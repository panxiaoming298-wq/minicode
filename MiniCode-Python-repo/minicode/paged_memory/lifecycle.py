"""Retention, real deletion, orphan GC, integrity scans and local restore drills."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from contextlib import nullcontext
from dataclasses import replace
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .artifacts import (
    ArtifactIntegrityError,
    ArtifactStoreUnavailable,
    FileArtifactStore,
)
from .config import PagedMemoryConfig
from .models import (
    ArchiveStatus,
    ArtifactRecord,
    ArtifactStatus,
    DependencyType,
    MemoryType,
    RetentionRule,
    TaskRetentionPolicy,
    content_hash_bytes,
    content_hash_text,
    new_id,
    now_epoch,
    stable_json,
)
from .sqlite_store import SqliteMemoryStore


class RetentionDenied(RuntimeError):
    pass


class BackupVerificationError(RuntimeError):
    pass


def _io_path(path: Path) -> Path:
    """Use the Windows extended namespace for deep backup/restore payloads."""
    value = str(path.resolve())
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return Path(value)


class RetentionPolicyEngine:
    """Deterministic policy matching; legal hold always wins over expiry."""

    def __init__(self, rules: Sequence[RetentionRule] = ()) -> None:
        self.rules = list(rules)

    def match(
        self,
        *,
        tenant_id: str = "",
        event_type: str = "",
        memory_type: str = "",
        artifact_type: str = "",
        sensitivity_level: str = "",
        task_status: str = "",
    ) -> RetentionRule | None:
        for rule in self.rules:
            if rule.tenant_ids and tenant_id not in rule.tenant_ids:
                continue
            if rule.event_types and event_type not in rule.event_types:
                continue
            if rule.memory_types and memory_type not in rule.memory_types:
                continue
            if rule.artifact_types and artifact_type not in rule.artifact_types:
                continue
            if (
                rule.sensitivity_levels
                and sensitivity_level not in rule.sensitivity_levels
            ):
                continue
            if rule.task_statuses and task_status not in rule.task_statuses:
                continue
            return rule
        return None

    @staticmethod
    def expired(
        *,
        created_at: float,
        now: float,
        rule: RetentionRule | None,
        task_policy: TaskRetentionPolicy | None,
    ) -> bool:
        if (rule and rule.legal_hold) or (task_policy and task_policy.legal_hold):
            return False
        deadlines: list[float] = []
        if rule and rule.retention_seconds is not None:
            deadlines.append(created_at + rule.retention_seconds)
        if task_policy and task_policy.retention_until is not None:
            deadlines.append(task_policy.retention_until)
        return bool(deadlines) and now >= min(deadlines)


class LifecycleManager:
    def __init__(
        self,
        *,
        authority: Any,
        artifact_store: Any,
        spool_store: Any | None = None,
        config: PagedMemoryConfig | None = None,
    ) -> None:
        self.authority = authority
        self.artifact_store = artifact_store
        self.spool_store = spool_store
        self.config = config or PagedMemoryConfig()
        self.retention_policy = RetentionPolicyEngine(self.config.retention_rules)

    def set_task_retention(
        self,
        *,
        tenant_id: str,
        task_id: str,
        retention_until: float | None,
        legal_hold: bool,
        set_by: str,
        deletion_mode: str = "HARD_DELETE",
    ) -> TaskRetentionPolicy:
        if deletion_mode not in {"HARD_DELETE", "TOMBSTONE"}:
            raise ValueError("deletion_mode must be HARD_DELETE or TOMBSTONE")
        policy = TaskRetentionPolicy(
            tenant_id=tenant_id,
            task_id=task_id,
            retention_until=retention_until,
            legal_hold=legal_hold,
            deletion_mode=deletion_mode,
            set_by=set_by,
        )
        return self.authority.set_retention_policy(policy)

    def delete_task(
        self,
        *,
        tenant_id: str,
        task_id: str,
        authorized: bool,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Propagate an authorized real deletion to all known storage layers."""
        started = now_epoch()
        if not authorized or not actor.strip() or not reason.strip():
            raise RetentionDenied(
                "real deletion requires authorization, actor and reason"
            )
        policy = self.authority.get_retention_policy(
            tenant_id=tenant_id, task_id=task_id
        )
        if policy and policy.legal_hold:
            raise RetentionDenied("task is under legal hold")

        begin_delete = getattr(self.authority, "begin_task_delete", None)
        previous_task_status = (
            begin_delete(tenant_id=tenant_id, task_id=task_id)
            if callable(begin_delete)
            else "ACTIVE"
        )
        if previous_task_status is None:
            status_reader = getattr(self.authority, "task_status", None)
            current_status = (
                status_reader(tenant_id=tenant_id, task_id=task_id)
                if callable(status_reader)
                else None
            )
            if current_status != "DELETING":
                raise RetentionDenied("task is missing or already being deleted")
            # A prior distributed delete made partial progress.  Keep the
            # fence in place and resume idempotent propagation.
            previous_task_status = "DELETING"

        destructive_progress = False
        try:
            artifacts = self.authority.all_artifacts(
                tenant_id=tenant_id, task_id=task_id
            )
            memories = self.authority.all_memories(tenant_id=tenant_id, task_id=task_id)
            errors: list[str] = []
            invalidator = getattr(self.authority, "working_set_invalidate", None)
            if callable(invalidator) and memories:
                invalidator(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    memory_ids=[memory.id for memory in memories],
                )
                destructive_progress = True

            deleted_artifacts = 0
            for artifact in artifacts:
                try:
                    store = (
                        self.spool_store
                        if artifact.status == ArtifactStatus.PENDING_UPLOAD
                        else self.artifact_store
                    )
                    if store is None:
                        raise ArtifactStoreUnavailable("artifact store unavailable")
                    store.delete(artifact)
                    deleted_artifacts += 1
                    destructive_progress = True
                    source_spool_uri = str(
                        artifact.metadata.get("source_spool_uri", "")
                    )
                    if source_spool_uri and source_spool_uri != artifact.uri:
                        if self.spool_store is None:
                            raise ArtifactStoreUnavailable(
                                "promoted artifact spool is unavailable for deletion"
                            )
                        stale_spool_record = ArtifactRecord(
                            **{
                                **artifact.__dict__,
                                "uri": source_spool_uri,
                                "status": ArtifactStatus.PENDING_UPLOAD,
                            }
                        )
                        self.spool_store.delete(stale_spool_record)
                        deleted_artifacts += 1
                        destructive_progress = True
                except Exception as exc:  # noqa: BLE001 - collect cross-store failures.
                    errors.append(f"artifact {artifact.id}: {type(exc).__name__}")
            if errors:
                # Do not erase the registry while opaque payloads may still exist;
                # this keeps retries scoped and auditable without preserving a copy.
                raise RetentionDenied(
                    "distributed delete incomplete: " + "; ".join(errors)
                )

            request_id = new_id("delete")
            actor_hash = content_hash_text(actor)
            reason_hash = content_hash_text(reason)
            counts = self.authority.hard_delete_task(
                tenant_id=tenant_id,
                task_id=task_id,
                request_id=request_id,
                actor_hash=actor_hash,
                reason_hash=reason_hash,
            )
        except Exception:
            restore_status = getattr(self.authority, "restore_task_status", None)
            if (
                callable(restore_status)
                and previous_task_status != "DELETING"
                and not destructive_progress
            ):
                restore_status(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    status=previous_task_status,
                )
            raise
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "task_id_hash": content_hash_text(task_id),
            "actor_hash": actor_hash,
            "reason_hash": reason_hash,
            "execution_status": "COMPLETED",
            "artifact_objects": deleted_artifacts,
            "database_rows": counts,
            "completed_at": now_epoch(),
            "hard_delete_propagation_lag_ms": (now_epoch() - started) * 1_000,
        }

    def reference_integrity_scan(
        self,
        *,
        tenant_id: str,
        task_id: str,
        verify_payload_hashes: bool = True,
    ) -> dict[str, Any]:
        artifacts = self.authority.all_artifacts(tenant_id=tenant_id, task_id=task_id)
        artifact_by_id = {artifact.id: artifact for artifact in artifacts}
        artifact_by_uri = {artifact.uri: artifact for artifact in artifacts}
        memories = self.authority.all_memories(tenant_id=tenant_id, task_id=task_id)
        memory_by_id = {memory.id: memory for memory in memories}
        events = self.authority.recent_events(
            tenant_id=tenant_id, task_id=task_id, limit=1_000_000
        )
        event_by_id = {event.id: event for event in events}
        decisions = self.authority.active_decisions(
            tenant_id=tenant_id, task_id=task_id
        )
        decision_ids = {decision.id for decision in decisions}
        decision_keys = {decision.decision_key for decision in decisions}
        dependency_reader = getattr(self.authority, "all_dependencies", None)
        dependencies = (
            dependency_reader(tenant_id=tenant_id, task_id=task_id)
            if callable(dependency_reader)
            else []
        )
        checkpoint_reader = getattr(self.authority, "all_checkpoints", None)
        checkpoints = (
            checkpoint_reader(tenant_id=tenant_id, task_id=task_id)
            if callable(checkpoint_reader)
            else []
        )
        checkpoint_by_id = {checkpoint.id: checkpoint for checkpoint in checkpoints}
        archive_reader = getattr(self.authority, "list_archive_manifests", None)
        archive_manifests = (
            archive_reader(
                tenant_id=tenant_id,
                task_id=task_id,
                statuses=(
                    ArchiveStatus.ACTIVE.value,
                    ArchiveStatus.VERIFIED.value,
                    ArchiveStatus.RESTORED.value,
                ),
            )
            if callable(archive_reader)
            else []
        )
        missing_registry: list[str] = []
        missing_objects: list[str] = []
        hash_mismatches: list[str] = []
        invalid_sources: list[str] = []
        invalid_dependencies: list[str] = []
        event_hash_mismatches: list[str] = []
        memory_hash_mismatches: list[str] = []
        checkpoint_hash_mismatches: list[str] = []
        invalid_archives: list[str] = []
        artifact_payloads: dict[str, bytes] = {}

        for event in events:
            if event.artifact_uri and event.artifact_uri not in artifact_by_uri:
                missing_registry.append(f"event:{event.id}:{event.artifact_uri}")
            if event.artifact_uri:
                artifact = artifact_by_uri.get(event.artifact_uri)
                expected_hash = artifact.content_hash if artifact is not None else ""
            else:
                expected_hash = str(event.content_json.get("content_hash", ""))
                if not expected_hash:
                    text = event.content_text
                    # Empty tool output is a valid source; older structured
                    # events can instead carry the digest of their JSON body.
                    if not text and event.content_hash != content_hash_text(""):
                        text = stable_json(event.content_json)
                    expected_hash = content_hash_text(text)
            if event.content_hash != expected_hash:
                event_hash_mismatches.append(event.id)
        for memory in memories:
            is_artifact_chunk = memory.memory_type == MemoryType.ARTIFACT_CHUNK
            is_event_chunk = "event_chunk" in memory.facets and not is_artifact_chunk
            if memory.source_event_id:
                source = event_by_id.get(memory.source_event_id)
                if source is None or (
                    not is_artifact_chunk and not is_event_chunk
                    and source.content_hash != memory.content_hash
                ):
                    invalid_sources.append(
                        f"memory:{memory.id}:event:{memory.source_event_id}"
                    )
                elif is_event_chunk:
                    source_range = memory.source_range
                    data = source.content_text.encode("utf-8")
                    if (source.artifact_uri or memory.artifact_id
                        or source_range.unit != "BYTE" or source_range.start < 0
                        or source_range.end <= source_range.start or source_range.end > len(data)):
                        invalid_sources.append(f"memory:{memory.id}:event-range:{source.id}")
                    elif verify_payload_hashes and content_hash_bytes(
                        data[source_range.start:source_range.end]
                    ) != memory.content_hash:
                        memory_hash_mismatches.append(memory.id)
            elif is_event_chunk:
                invalid_sources.append(f"memory:{memory.id}:event:missing-id")
            if memory.artifact_id:
                artifact = artifact_by_id.get(memory.artifact_id)
                if artifact is None:
                    missing_registry.append(
                        f"memory:{memory.id}:artifact:{memory.artifact_id}"
                    )
                elif is_artifact_chunk:
                    source_range = memory.source_range
                    if (
                        source_range.unit != "BYTE"
                        or source_range.start < 0
                        or source_range.end <= source_range.start
                        or source_range.end > artifact.size_bytes
                    ):
                        invalid_sources.append(
                            f"memory:{memory.id}:artifact-range:{memory.artifact_id}"
                        )
                elif artifact.content_hash != memory.content_hash:
                    invalid_sources.append(
                        f"memory:{memory.id}:artifact-hash:{memory.artifact_id}"
                    )
            elif is_artifact_chunk:
                invalid_sources.append(f"memory:{memory.id}:artifact:missing-id")

        for decision in decisions:
            source = event_by_id.get(decision.source_event_id)
            if source is None or source.content_text != decision.decision_text:
                invalid_sources.append(
                    f"decision:{decision.id}:event:{decision.source_event_id}"
                )

        for checkpoint in checkpoints:
            if (
                content_hash_text(stable_json(checkpoint.snapshot))
                != checkpoint.content_hash
            ):
                checkpoint_hash_mismatches.append(checkpoint.id)
            checkpoint_state = checkpoint.snapshot.get("task_state", {})
            if isinstance(checkpoint_state, Mapping):
                for artifact_id in checkpoint_state.get("active_artifacts", ()):
                    if str(artifact_id) not in artifact_by_id:
                        missing_registry.append(
                            f"checkpoint:{checkpoint.id}:artifact:{artifact_id}"
                        )

        step_getter = getattr(self.authority, "get_step", None)
        for dependency in dependencies:
            owner = (
                step_getter(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    step_id=dependency.step_id,
                )
                if callable(step_getter)
                else None
            )
            target_exists = owner is not None
            if dependency.dependency_type == DependencyType.EVENT:
                target_exists = (
                    target_exists and dependency.dependency_id in event_by_id
                )
            elif dependency.dependency_type == DependencyType.DECISION:
                target_exists = target_exists and dependency.dependency_id in {
                    *decision_ids,
                    *decision_keys,
                }
            elif dependency.dependency_type in {
                DependencyType.MEMORY,
                DependencyType.CONSTRAINT,
            }:
                target_exists = (
                    target_exists and dependency.dependency_id in memory_by_id
                )
            elif dependency.dependency_type == DependencyType.STEP:
                target_exists = (
                    target_exists
                    and callable(step_getter)
                    and bool(
                        step_getter(
                            tenant_id=tenant_id,
                            task_id=task_id,
                            step_id=dependency.dependency_id,
                        )
                    )
                )
            elif dependency.dependency_type == DependencyType.ARTIFACT:
                target_exists = (
                    target_exists and dependency.dependency_id in artifact_by_id
                )
            elif dependency.dependency_type == DependencyType.CHECKPOINT:
                target_exists = (
                    target_exists and dependency.dependency_id in checkpoint_by_id
                )
            if not target_exists:
                invalid_dependencies.append(
                    f"step:{dependency.step_id}:{dependency.dependency_type.value}:"
                    f"{dependency.dependency_id}"
                )

        if verify_payload_hashes:
            for artifact in artifacts:
                if artifact.status == ArtifactStatus.TOMBSTONED:
                    continue
                store = (
                    self.spool_store
                    if artifact.status == ArtifactStatus.PENDING_UPLOAD
                    else self.artifact_store
                )
                if store is None:
                    missing_objects.append(artifact.id)
                    continue
                try:
                    data = store.get_bytes(artifact)
                    artifact_payloads[artifact.id] = data
                    if content_hash_bytes(data) != artifact.content_hash:
                        hash_mismatches.append(artifact.id)
                except ArtifactIntegrityError:
                    hash_mismatches.append(artifact.id)
                except Exception:  # noqa: BLE001 - report unavailable objects uniformly.
                    missing_objects.append(artifact.id)
            for memory in memories:
                if memory.memory_type != MemoryType.ARTIFACT_CHUNK:
                    continue
                data = artifact_payloads.get(memory.artifact_id)
                if data is None:
                    continue
                source_range = memory.source_range
                if (
                    source_range.unit != "BYTE"
                    or source_range.start < 0
                    or source_range.end <= source_range.start
                    or source_range.end > len(data)
                ):
                    continue
                if (
                    content_hash_bytes(data[source_range.start : source_range.end])
                    != memory.content_hash
                ):
                    memory_hash_mismatches.append(memory.id)
            if archive_manifests:
                # Imported lazily to keep lifecycle/archive module initialization acyclic.
                from .archive import EventArchiveManager

                archive_manager = EventArchiveManager(
                    authority=self.authority,
                    artifact_store=self.artifact_store,
                )
                for manifest in archive_manifests:
                    try:
                        archive_manager.verify_archive(
                            tenant_id=tenant_id,
                            task_id=task_id,
                            archive_id=manifest.id,
                        )
                    except Exception:  # noqa: BLE001 - report by opaque archive ID only.
                        invalid_archives.append(manifest.id)

        state = self.authority.get_state(tenant_id=tenant_id, task_id=task_id)
        if state is not None:
            for artifact_id in state.active_artifacts:
                if artifact_id not in artifact_by_id:
                    missing_registry.append(
                        f"state:{state.version}:artifact:{artifact_id}"
                    )
            for constraint in state.active_constraints:
                memory_id = (
                    str(constraint.get("memory_id", ""))
                    if isinstance(constraint, Mapping)
                    else ""
                )
                if not memory_id or memory_id not in memory_by_id:
                    invalid_sources.append(
                        f"state:{state.version}:constraint:{memory_id or 'missing-id'}"
                    )
            for key, decision in state.active_decisions.items():
                decision_id = (
                    str(decision.get("decision_id") or decision.get("id") or "")
                    if isinstance(decision, Mapping)
                    else ""
                )
                if decision_id not in decision_ids:
                    invalid_sources.append(
                        f"state:{state.version}:decision:{key}:{decision_id or 'missing-id'}"
                    )
        valid = not (
            missing_registry
            or missing_objects
            or hash_mismatches
            or memory_hash_mismatches
            or invalid_sources
            or invalid_dependencies
            or event_hash_mismatches
            or checkpoint_hash_mismatches
            or invalid_archives
        )
        return {
            "valid": valid,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "events": len(events),
            "memories": len(memories),
            "artifacts": len(artifacts),
            "dependencies": len(dependencies),
            "checkpoints": len(checkpoints),
            "archives": len(archive_manifests),
            "missing_registry_references": missing_registry,
            "missing_artifact_objects": missing_objects,
            "hash_mismatches": hash_mismatches,
            "memory_hash_mismatches": memory_hash_mismatches,
            "invalid_source_pointers": invalid_sources,
            "invalid_dependencies": invalid_dependencies,
            "event_hash_mismatches": event_hash_mismatches,
            "checkpoint_hash_mismatches": checkpoint_hash_mismatches,
            "invalid_archives": invalid_archives,
        }

    @staticmethod
    def _uri_in_tenant_scope(uri: str, tenant_id: str) -> bool:
        if uri.startswith(("file:", "spool:")):
            relative = uri.split(":", 1)[1]
        else:
            return False
        parts = PurePosixPath(relative).parts
        return bool(parts and parts[0] == tenant_id and ".." not in parts)

    def orphan_candidates(
        self,
        *,
        tenant_id: str,
        now: float | None = None,
        grace_hours: int | None = None,
    ) -> list[dict[str, Any]]:
        current = now_epoch() if now is None else now
        grace = 3600 * (
            self.config.artifact_gc_grace_hours if grace_hours is None else grace_hours
        )
        referenced = {
            artifact.uri
            for artifact in self.authority.all_artifacts(tenant_id=tenant_id)
        }
        reference_checker = getattr(self.authority, "artifact_reference_exists", None)
        candidates: list[dict[str, Any]] = []
        for uri, modified_at in self.artifact_store.list_objects():
            if not self._uri_in_tenant_scope(uri, tenant_id):
                continue
            relative = uri.split(":", 1)[-1]
            artifact_id = PurePosixPath(relative).stem
            if uri in referenced or (
                callable(reference_checker)
                and reference_checker(
                    tenant_id=tenant_id,
                    artifact_id=artifact_id,
                    uri=uri,
                )
            ):
                continue
            age_seconds = max(0.0, current - modified_at)
            if age_seconds < grace:
                continue
            candidates.append(
                {"uri": uri, "modified_at": modified_at, "age_seconds": age_seconds}
            )
        return candidates

    def gc_orphans(
        self,
        *,
        tenant_id: str,
        dry_run: bool = True,
        now: float | None = None,
        grace_hours: int | None = None,
    ) -> dict[str, Any]:
        candidates = self.orphan_candidates(
            tenant_id=tenant_id, now=now, grace_hours=grace_hours
        )
        deleted: list[str] = []
        errors: list[str] = []
        if not dry_run:
            for candidate in candidates:
                uri = candidate["uri"]
                placeholder = ArtifactRecord(
                    tenant_id=tenant_id,
                    task_id="gc",
                    run_id="gc",
                    step_id="gc",
                    artifact_type="ORPHAN",
                    uri=uri,
                    content_hash="sha256:" + "0" * 64,
                    media_type="application/octet-stream",
                    size_bytes=0,
                )
                try:
                    self.artifact_store.delete(placeholder)
                    deleted.append(uri)
                except Exception as exc:  # noqa: BLE001 - report each GC failure.
                    errors.append(f"{uri}: {type(exc).__name__}")
        return {
            "dry_run": dry_run,
            "tenant_id": tenant_id,
            "candidate_count": len(candidates),
            "artifact_orphan_candidate_count": len(candidates),
            "candidates": candidates,
            "deleted": deleted,
            "errors": errors,
        }

    def create_local_backup(self, *, destination: str | Path | None = None) -> Path:
        from minicode.store_lock import store_lock
        sessions = Path(self.config.data_root) / 'sessions' if self.config.data_root else None
        guard = store_lock(sessions / '.store.lock') if sessions and sessions.exists() else nullcontext()
        with guard:
            return self._create_local_backup(destination=destination)

    def _create_local_backup(self, *, destination: str | Path | None = None) -> Path:
        """Create a verified local authority+artifact bundle.

        Includes the SQLite database, local payloads and session metadata.
        """
        if not isinstance(self.authority, SqliteMemoryStore):
            raise NotImplementedError(
                "backup requires the SQLite authority"
            )
        if not isinstance(self.artifact_store, FileArtifactStore):
            raise NotImplementedError(
                "backup requires a local file artifact store"
            )
        backup_root = Path(destination or self.config.backup_root).resolve()
        for protected in (self.config.artifact_root, self.config.local_spool_root):
            if backup_root.is_relative_to(Path(protected).resolve()):
                raise ValueError('backup destination must be outside artifact/spool storage')
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        encrypted = bool(self.config.backup_encryption_key)
        suffix = ".tar.gz.enc" if encrypted else ".tar.gz"
        target = backup_root / f"minicode-memory-{stamp}-{new_id('backup')[-12:]}{suffix}"
        temporary = target.with_name(f".{target.name}.tmp{suffix}")

        with tempfile.TemporaryDirectory(prefix=".staging-", dir=_io_path(backup_root)) as temporary_dir:
            staging = Path(temporary_dir)
            database_path = 'database/state.db' if self.config.data_root else 'state.db'
            self.authority.backup_to(staging / database_path)
            artifact_target = staging / "artifacts"
            if self.artifact_store.root.exists():
                shutil.copytree(self.artifact_store.root, artifact_target)
            if (
                self.spool_store is not None
                and isinstance(self.spool_store, FileArtifactStore)
                and self.spool_store.root.exists()
            ):
                shutil.copytree(self.spool_store.root, staging / "spool")
            sessions = Path(self.config.data_root) / 'sessions' if self.config.data_root else None
            if sessions and sessions.exists():
                if backup_root.is_relative_to(sessions.resolve()):
                    raise ValueError('backup destination must be outside session storage')
                shutil.copytree(sessions, staging / 'sessions', ignore=shutil.ignore_patterns('.store.lock'))
            files = self._hash_tree(staging)
            manifest = {
                "schema_version": 2,
                "database_path": database_path,
                "created_at": now_epoch(),
                "authority": "sqlite",
                "artifact_store": "file",
                "encrypted": encrypted,
                "rpo_hours": self.config.backup_rpo_hours,
                "rto_hours": self.config.backup_rto_hours,
                "files": files,
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            payload = self._tar_directory(staging)
            if encrypted:
                payload = self._encrypt_backup(
                    payload, self.config.backup_encryption_key
                )
            try:
                with temporary.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                self.verify_local_backup(temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return target

    def verify_local_backup(self, backup: str | Path) -> dict[str, Any]:
        path = Path(backup).resolve()
        payload = path.read_bytes()
        if path.name.endswith(".enc"):
            payload = self._decrypt_backup(payload, self.config.backup_encryption_key)
        scratch = Path(self.config.backup_root)
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=_io_path(scratch)) as temporary_dir:
            staging = Path(temporary_dir)
            self._extract_tar(payload, staging)
            manifest_path = staging / "manifest.json"
            if not manifest_path.is_file():
                raise BackupVerificationError("backup manifest is missing")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected: Mapping[str, str] = manifest.get("files", {})
            actual = self._hash_tree(staging, exclude=("manifest.json",))
            if dict(expected) != actual:
                raise BackupVerificationError("backup file manifest/hash mismatch")
            database_path = manifest.get('database_path', 'state.db')
            if database_path not in {'state.db', 'database/state.db'}:
                raise BackupVerificationError('unsupported backup database path')
            database = staging / database_path
            connection = sqlite3.connect(database)
            try:
                check = connection.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise BackupVerificationError(
                        "restored SQLite integrity_check failed"
                    )
                rows = connection.execute(
                    "SELECT uri,content_hash,size_bytes,status FROM agent_artifact"
                ).fetchall()
            finally:
                connection.close()
            missing: list[str] = []
            mismatched: list[str] = []
            for uri, digest, size_bytes, status in rows:
                if status != ArtifactStatus.ACTIVE.value or not str(uri).startswith(
                    "file:"
                ):
                    continue
                artifact_path = (
                    staging
                    / "artifacts"
                    / Path(*PurePosixPath(str(uri).removeprefix("file:")).parts)
                )
                if not artifact_path.is_file():
                    missing.append(str(uri))
                    continue
                data = artifact_path.read_bytes()
                if len(data) != size_bytes or content_hash_bytes(data) != digest:
                    mismatched.append(str(uri))
            if missing or mismatched:
                raise BackupVerificationError(
                    f"backup artifact verification failed: missing={missing}, mismatched={mismatched}"
                )
            return {
                "valid": True,
                "backup": str(path),
                "created_at": manifest.get("created_at"),
                "file_count": len(expected),
                "artifact_count": len(rows),
                "encrypted": bool(manifest.get("encrypted")),
                "last_successful_backup_age_seconds": max(
                    0.0, now_epoch() - float(manifest.get("created_at") or 0.0)
                ),
            }

    def restore_local_backup(
        self, backup: str | Path, *, target_directory: str | Path
    ) -> Path:
        """Restore into a new empty directory; never overwrite a live workspace."""
        self.verify_local_backup(backup)
        target = Path(target_directory).resolve()
        if target.exists() and any(target.iterdir()):
            raise FileExistsError("restore target must be absent or empty")
        target.mkdir(parents=True, exist_ok=True)
        path = Path(backup).resolve()
        payload = path.read_bytes()
        if path.name.endswith(".enc"):
            payload = self._decrypt_backup(payload, self.config.backup_encryption_key)
        self._extract_tar(payload, _io_path(target))
        marker_path = target / 'sessions' / '.history-backend.json'
        if marker_path.is_file():
            marker = json.loads(marker_path.read_text(encoding='utf-8'))
            if marker.get('backend') == 'sqlite':
                from minicode.history_storage import _digest, sqlite_artifact_identity
                from minicode.session_contract import atomic_write_json
                manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
                database = target / manifest.get('database_path', 'state.db')
                marker['databaseId'] = _digest({'path': os.path.normcase(os.path.abspath(database))})
                marker['artifactStoreId'] = sqlite_artifact_identity(replace(
                    self.config, artifact_store_type='file', artifact_root=str(target / 'artifacts')))
                atomic_write_json(marker_path, marker)
        return target

    def restore_drill(self, backup: str | Path) -> dict[str, Any]:
        started = now_epoch()
        scratch = Path(self.config.backup_root)
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".restore-drill-", dir=_io_path(scratch)
        ) as temporary_dir:
            restored = self.restore_local_backup(backup, target_directory=temporary_dir)
            manifest = json.loads((restored / 'manifest.json').read_text(encoding='utf-8'))
            database = restored / manifest.get('database_path', 'state.db')
            connection = sqlite3.connect(database)
            try:
                events = connection.execute(
                    "SELECT COUNT(*) FROM agent_event"
                ).fetchone()[0]
                states = connection.execute(
                    "SELECT COUNT(*) FROM agent_task_state"
                ).fetchone()[0]
                artifacts = connection.execute(
                    "SELECT COUNT(*) FROM agent_artifact"
                ).fetchone()[0]
                archive_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='memory_archive_manifest'"
                ).fetchone()
                archives = (
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_archive_manifest"
                    ).fetchone()[0]
                    if archive_table
                    else 0
                )
            finally:
                connection.close()
        return {
            "valid": True,
            "restore_drill_success": True,
            "events": events,
            "states": states,
            "artifacts": artifacts,
            "archives": archives,
            "elapsed_seconds": now_epoch() - started,
            "rto_target_hours": self.config.backup_rto_hours,
        }

    @staticmethod
    def _hash_tree(root: Path, *, exclude: Sequence[str] = ()) -> dict[str, str]:
        excluded = set(exclude)
        values: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            values[relative] = content_hash_bytes(path.read_bytes())
        return values

    @staticmethod
    def _tar_directory(root: Path) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path in sorted(root.rglob("*")):
                archive.add(
                    path, arcname=path.relative_to(root).as_posix(), recursive=False
                )
        return buffer.getvalue()

    @staticmethod
    def _extract_tar(payload: bytes, target: Path) -> None:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            root = target.resolve()
            for member in archive.getmembers():
                member_name = PurePosixPath(member.name)
                if member_name.is_absolute() or ".." in member_name.parts:
                    raise BackupVerificationError("backup contains an unsafe path")
                member_path = (root / Path(*member_name.parts)).resolve()
                try:
                    member_path.relative_to(root)
                except ValueError as exc:
                    raise BackupVerificationError(
                        "backup contains an unsafe path"
                    ) from exc
                if member.issym() or member.islnk():
                    raise BackupVerificationError("backup contains a link")
                if not (member.isfile() or member.isdir()):
                    raise BackupVerificationError(
                        "backup contains an unsupported special file"
                    )
            archive.extractall(root)

    @staticmethod
    def _decode_key(encoded_key: str) -> bytes:
        if not encoded_key:
            raise BackupVerificationError("backup encryption key is required")
        try:
            key = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
        except Exception as exc:
            raise BackupVerificationError("invalid backup encryption key") from exc
        if len(key) not in {16, 24, 32}:
            raise BackupVerificationError(
                "backup key must decode to 16, 24, or 32 bytes"
            )
        return key

    @classmethod
    def _encrypt_backup(cls, payload: bytes, encoded_key: str) -> bytes:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise BackupVerificationError(
                "encrypted backups require cryptography"
            ) from exc
        nonce = os.urandom(12)
        return (
            b"MCBK1"
            + nonce
            + AESGCM(cls._decode_key(encoded_key)).encrypt(nonce, payload, None)
        )

    @classmethod
    def _decrypt_backup(cls, payload: bytes, encoded_key: str) -> bytes:
        if not payload.startswith(b"MCBK1") or len(payload) < 18:
            raise BackupVerificationError("invalid encrypted backup header")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise BackupVerificationError(
                "encrypted backups require cryptography"
            ) from exc
        nonce = payload[5:17]
        try:
            return AESGCM(cls._decode_key(encoded_key)).decrypt(
                nonce, payload[17:], None
            )
        except Exception as exc:
            raise BackupVerificationError(
                "backup authentication/decryption failed"
            ) from exc


__all__ = [
    "BackupVerificationError",
    "LifecycleManager",
    "RetentionDenied",
    "RetentionPolicyEngine",
]
