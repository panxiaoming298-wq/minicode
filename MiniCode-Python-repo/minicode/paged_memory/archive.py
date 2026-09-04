"""Immutable event archives backed by the configured Artifact Store.

The archive is a lossless cold copy, not a summary and not a vector index.  It
uses canonical JSONL inside deterministic gzip, while a relational manifest
keeps scope, range, hashes and restore status queryable.  Source events remain
in the authority store unless a separately authorized retention workflow is
introduced; creating an archive never deletes data.
"""

from __future__ import annotations

import gzip
import io
import json
import struct
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

from .artifacts import ArtifactIntegrityError as ArtifactPayloadIntegrityError
from .artifacts import ArtifactStoreUnavailable
from .lifecycle import RetentionPolicyEngine
from .models import (
    AgentEvent,
    ArchiveManifest,
    ArchiveStatus,
    ArtifactStatus,
    EventType,
    RetentionRule,
    content_hash_bytes,
    now_epoch,
    stable_json,
)

ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_FORMAT = "minicode.agent-event-archive"
_CONTROL_EVENT_TYPES = (
    EventType.ARCHIVE_CREATED.value,
    EventType.ARCHIVE_RESTORED.value,
)


class ArchiveError(RuntimeError):
    pass


class ArchiveIntegrityError(ArchiveError):
    pass


class ArchiveLimitExceeded(ArchiveError):
    pass


class ArchiveRestoreConflict(ArchiveError):
    pass


def _event_document(event: AgentEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "tenant_id": event.tenant_id,
        "task_id": event.task_id,
        "run_id": event.run_id,
        "step_id": event.step_id,
        "event_type": event.event_type.value,
        "content_text": event.content_text,
        "content_json": event.content_json,
        "artifact_uri": event.artifact_uri,
        "content_hash": event.content_hash,
        "token_count": event.token_count,
        "caused_by_event_id": event.caused_by_event_id,
        "correlation_id": event.correlation_id,
        "idempotency_key": event.idempotency_key,
        "created_at": event.created_at,
        "schema_version": event.schema_version,
    }


def _deterministic_gzip(data: bytes) -> bytes:
    """Return a byte-for-byte stable gzip stream (mtime=0, OS=unknown)."""
    compressor = zlib.compressobj(
        level=9,
        method=zlib.DEFLATED,
        wbits=-zlib.MAX_WBITS,
        memLevel=9,
        strategy=zlib.Z_DEFAULT_STRATEGY,
    )
    body = compressor.compress(data) + compressor.flush()
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"
    trailer = struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)
    return header + body + trailer


def _stable_id(prefix: str, digest: str) -> str:
    return f"{prefix}_{digest.removeprefix('sha256:')[:32]}"


class EventArchiveManager:
    """Create, verify, inspect and restore immutable AgentEvent segments."""

    def __init__(
        self,
        *,
        authority: Any,
        artifact_store: Any,
        retention_rules: Sequence[RetentionRule] = (),
        max_uncompressed_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        self.authority = authority
        self.artifact_store = artifact_store
        self.retention_policy = RetentionPolicyEngine(retention_rules)
        self.max_uncompressed_bytes = max(1, max_uncompressed_bytes)

    def archive_range(
        self,
        *,
        tenant_id: str,
        task_id: str,
        after: float | None = None,
        before: float | None = None,
        event_types: Sequence[str] = (),
        max_events: int = 100_000,
        backup_class: str = "STANDARD",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        if after is not None and before is not None and after >= before:
            raise ValueError("archive range must satisfy after < before")
        events = self.authority.events_in_range(
            tenant_id=tenant_id,
            task_id=task_id,
            after=after,
            before=before,
            limit=max_events + 1,
            event_types=event_types,
            exclude_event_types=_CONTROL_EVENT_TYPES,
        )
        if len(events) > max_events:
            raise ArchiveLimitExceeded(
                f"archive selection exceeds max_events={max_events}; split the range"
            )
        return self._archive_events(
            events,
            tenant_id=tenant_id,
            task_id=task_id,
            rule_name="manual",
            backup_class=backup_class,
            selection={"kind": "manual", "after": after, "before": before},
            dry_run=dry_run,
        )

    def archive_due(
        self,
        *,
        tenant_id: str,
        task_id: str,
        now: float | None = None,
        task_status: str = "",
        max_events: int = 100_000,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Execute configured ``archive_after_seconds`` rules for one task."""
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        current_time = now_epoch() if now is None else now
        events = self.authority.events_in_range(
            tenant_id=tenant_id,
            task_id=task_id,
            limit=max_events + 1,
            exclude_event_types=_CONTROL_EVENT_TYPES,
        )
        if len(events) > max_events:
            raise ArchiveLimitExceeded(
                f"task exceeds max_events={max_events}; archive explicit ranges first"
            )

        archived_ids: set[str] = set()
        for manifest in self.authority.list_archive_manifests(
            tenant_id=tenant_id,
            task_id=task_id,
            statuses=(
                ArchiveStatus.ACTIVE.value,
                ArchiveStatus.VERIFIED.value,
                ArchiveStatus.RESTORED.value,
            ),
        ):
            if manifest.metadata.get("selection", {}).get("kind") != "policy":
                continue
            for document in self._load_documents(
                manifest, update_verification=not dry_run
            ):
                archived_ids.add(str(document["id"]))

        grouped: dict[tuple[str, str], list[AgentEvent]] = {}
        for event in events:
            if event.id in archived_ids:
                continue
            sensitivity = str(event.content_json.get("sensitivity_level", ""))
            rule = self.retention_policy.match(
                tenant_id=tenant_id,
                event_type=event.event_type.value,
                sensitivity_level=sensitivity,
                task_status=task_status,
            )
            if (
                rule is None
                or rule.archive_after_seconds is None
                or current_time < event.created_at + rule.archive_after_seconds
            ):
                continue
            grouped.setdefault((rule.name, rule.backup_class), []).append(event)

        operations: list[dict[str, Any]] = []
        for (rule_name, backup_class), selected in grouped.items():
            operations.append(
                self._archive_events(
                    selected,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    rule_name=rule_name,
                    backup_class=backup_class,
                    selection={
                        "kind": "policy",
                        "rule_name": rule_name,
                    },
                    dry_run=dry_run,
                )
            )
        return {
            "dry_run": dry_run,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "candidate_count": sum(len(value) for value in grouped.values()),
            "archive_count": len(operations),
            "operations": operations,
            "source_events_deleted": 0,
        }

    def verify_archive(
        self, *, tenant_id: str, task_id: str, archive_id: str
    ) -> dict[str, Any]:
        manifest = self._manifest(
            tenant_id=tenant_id, task_id=task_id, archive_id=archive_id
        )
        documents = self._load_documents(manifest, update_verification=True)
        return {
            "valid": True,
            "archive_id": manifest.id,
            "artifact_id": manifest.artifact_id,
            "tenant_id": manifest.tenant_id,
            "task_id": manifest.task_id,
            "event_count": len(documents),
            "content_hash": manifest.content_hash,
            "object_hash": manifest.object_hash,
            "status": manifest.status.value,
            "source_events_deleted": 0,
        }

    def read_archive_events(
        self,
        *,
        tenant_id: str,
        task_id: str,
        archive_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if offset < 0 or limit < 1:
            raise ValueError("archive read requires offset >= 0 and limit >= 1")
        manifest = self._manifest(
            tenant_id=tenant_id, task_id=task_id, archive_id=archive_id
        )
        documents = self._load_documents(manifest, update_verification=True)
        return documents[offset : offset + limit]

    def restore_archive(
        self,
        *,
        tenant_id: str,
        task_id: str,
        archive_id: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        manifest = self._manifest(
            tenant_id=tenant_id, task_id=task_id, archive_id=archive_id
        )
        documents = self._load_documents(manifest, update_verification=True)
        missing = [
            document
            for document in documents
            if self.authority.get_event(
                tenant_id=tenant_id,
                task_id=task_id,
                event_id=str(document["id"]),
            )
            is None
        ]
        if not dry_run:
            for document in missing:
                restored = self._event_from_document(document)
                stored, _state = self.authority.persist_event(restored)
                if stored.id != restored.id or stored.content_hash != restored.content_hash:
                    raise ArchiveRestoreConflict(
                        f"event identity conflict while restoring {restored.id}"
                    )
            completed_at = now_epoch()
            manifest.status = ArchiveStatus.RESTORED
            manifest.restored_at = completed_at
            manifest.updated_at = completed_at
            self.authority.put_archive_manifest(manifest)
            restore_event = AgentEvent(
                id=_stable_id("event_restore", manifest.content_hash),
                tenant_id=tenant_id,
                task_id=task_id,
                run_id="archive-restore",
                event_type=EventType.ARCHIVE_RESTORED,
                content_json={
                    "archive_id": archive_id,
                    "artifact_id": manifest.artifact_id,
                    "restored_event_count": len(missing),
                    "archive_content_hash": manifest.content_hash,
                },
                idempotency_key=f"archive-restore:{archive_id}",
            )
            self.authority.persist_event(restore_event)
        return {
            "dry_run": dry_run,
            "archive_id": archive_id,
            "event_count": len(documents),
            "missing_event_count": len(missing),
            "restored_event_count": 0 if dry_run else len(missing),
            "all_events_present": not missing or not dry_run,
            "derived_projection_rebuild_required": bool(missing and not dry_run),
        }

    def _archive_events(
        self,
        events: Sequence[AgentEvent],
        *,
        tenant_id: str,
        task_id: str,
        rule_name: str,
        backup_class: str,
        selection: Mapping[str, Any],
        dry_run: bool,
    ) -> dict[str, Any]:
        ordered = sorted(events, key=lambda value: (value.created_at, value.id))
        if any(
            event.tenant_id != tenant_id or event.task_id != task_id for event in ordered
        ):
            raise ValueError("archive events must share one tenant/task scope")
        if not ordered:
            return {
                "dry_run": dry_run,
                "tenant_id": tenant_id,
                "task_id": task_id,
                "event_count": 0,
                "created": False,
                "source_events_deleted": 0,
            }

        raw = self._encode_jsonl(
            ordered, tenant_id=tenant_id, task_id=task_id, selection=selection
        )
        if len(raw) > self.max_uncompressed_bytes:
            raise ArchiveLimitExceeded(
                "archive exceeds configured uncompressed byte safety limit"
            )
        compressed = _deterministic_gzip(raw)
        content_hash = content_hash_bytes(raw)
        object_hash = content_hash_bytes(compressed)
        archive_id = _stable_id("archive", content_hash)
        artifact_id = _stable_id("artifact_archive", content_hash)
        base = {
            "dry_run": dry_run,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "archive_id": archive_id,
            "artifact_id": artifact_id,
            "event_count": len(ordered),
            "content_hash": content_hash,
            "object_hash": object_hash,
            "uncompressed_size_bytes": len(raw),
            "compressed_size_bytes": len(compressed),
            "first_event_id": ordered[0].id,
            "last_event_id": ordered[-1].id,
            "rule_name": rule_name,
            "backup_class": backup_class,
            "source_events_deleted": 0,
        }
        if dry_run:
            return {**base, "created": False, "existing": False}

        existing = self.authority.get_archive_manifest(
            tenant_id=tenant_id, task_id=task_id, archive_id=archive_id
        )
        if existing is not None:
            if (
                existing.content_hash != content_hash
                or existing.object_hash != object_hash
                or existing.event_count != len(ordered)
            ):
                raise ArchiveIntegrityError("deterministic archive identity collision")
            self._load_documents(existing, update_verification=True)
            return {
                **base,
                "created": False,
                "existing": True,
                "uri": existing.uri,
                "status": existing.status.value,
            }

        artifact = self.artifact_store.put_bytes(
            compressed,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id="archive",
            step_id="archive",
            media_type="application/gzip",
            artifact_type="EVENT_ARCHIVE",
        )
        if artifact.status is not ArtifactStatus.ACTIVE:
            raise ArchiveError("archive objects must reach the primary Artifact Store")
        if artifact.content_hash != object_hash or artifact.size_bytes != len(compressed):
            raise ArchiveIntegrityError("Artifact Store changed archive bytes")

        creation_event = AgentEvent(
            id=_stable_id("event_archive", content_hash),
            tenant_id=tenant_id,
            task_id=task_id,
            run_id="archive",
            step_id="archive",
            event_type=EventType.ARCHIVE_CREATED,
            artifact_uri=artifact.uri,
            content_hash=artifact.content_hash,
            content_json={
                "archive_id": archive_id,
                "artifact_id": artifact.id,
                "event_count": len(ordered),
                "archive_content_hash": content_hash,
                "object_hash": object_hash,
                "codec": "gzip",
                "source_events_deleted": 0,
            },
            idempotency_key=f"archive-create:{archive_id}",
        )
        artifact.source_event_id = creation_event.id
        artifact.metadata.update(
            {
                "archive_id": archive_id,
                "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
                "archive_content_hash": content_hash,
                "event_count": len(ordered),
                "backup_class": backup_class,
                "source_events_retained": True,
            }
        )
        self.authority.persist_event(creation_event, artifact=artifact)
        timestamp = now_epoch()
        manifest = ArchiveManifest(
            id=archive_id,
            tenant_id=tenant_id,
            task_id=task_id,
            artifact_id=artifact.id,
            uri=artifact.uri,
            content_hash=content_hash,
            object_hash=object_hash,
            event_count=len(ordered),
            uncompressed_size_bytes=len(raw),
            compressed_size_bytes=len(compressed),
            first_event_id=ordered[0].id,
            last_event_id=ordered[-1].id,
            first_created_at=ordered[0].created_at,
            last_created_at=ordered[-1].created_at,
            rule_name=rule_name,
            backup_class=backup_class,
            metadata={
                "format": ARCHIVE_FORMAT,
                "selection": dict(selection),
                "source_events_retained": True,
            },
            created_at=timestamp,
            updated_at=timestamp,
            schema_version=ARCHIVE_SCHEMA_VERSION,
        )
        self.authority.put_archive_manifest(manifest)
        self._load_documents(manifest, update_verification=True)
        return {
            **base,
            "created": True,
            "existing": False,
            "uri": manifest.uri,
            "status": manifest.status.value,
        }

    @staticmethod
    def _encode_jsonl(
        events: Sequence[AgentEvent],
        *,
        tenant_id: str,
        task_id: str,
        selection: Mapping[str, Any],
    ) -> bytes:
        header = {
            "record_type": "archive_header",
            "format": ARCHIVE_FORMAT,
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "event_count": len(events),
            "first_event_id": events[0].id,
            "last_event_id": events[-1].id,
            "first_created_at": events[0].created_at,
            "last_created_at": events[-1].created_at,
            "selection": dict(selection),
        }
        lines = [stable_json(header)]
        lines.extend(
            stable_json({"record_type": "agent_event", "event": _event_document(event)})
            for event in events
        )
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _manifest(
        self, *, tenant_id: str, task_id: str, archive_id: str
    ) -> ArchiveManifest:
        manifest = self.authority.get_archive_manifest(
            tenant_id=tenant_id, task_id=task_id, archive_id=archive_id
        )
        if manifest is None or manifest.status is ArchiveStatus.TOMBSTONED:
            raise ArchiveError(f"archive not found: {archive_id}")
        return manifest

    def _load_documents(
        self, manifest: ArchiveManifest, *, update_verification: bool
    ) -> list[dict[str, Any]]:
        artifact = self.authority.get_artifact(
            tenant_id=manifest.tenant_id,
            task_id=manifest.task_id,
            artifact_id=manifest.artifact_id,
        )
        if artifact is None or artifact.status is not ArtifactStatus.ACTIVE:
            raise ArchiveIntegrityError("archive Artifact Registry record is unavailable")
        if (
            artifact.uri != manifest.uri
            or artifact.content_hash != manifest.object_hash
            or artifact.size_bytes != manifest.compressed_size_bytes
            or artifact.artifact_type != "EVENT_ARCHIVE"
        ):
            raise ArchiveIntegrityError("archive manifest and Artifact Registry disagree")
        try:
            compressed = self.artifact_store.get_bytes(artifact)
        except (ArtifactPayloadIntegrityError, ArtifactStoreUnavailable) as exc:
            raise ArchiveIntegrityError("archive object is unavailable or corrupt") from exc
        if (
            len(compressed) != manifest.compressed_size_bytes
            or content_hash_bytes(compressed) != manifest.object_hash
        ):
            raise ArchiveIntegrityError("archive object hash mismatch")
        if manifest.uncompressed_size_bytes > self.max_uncompressed_bytes:
            raise ArchiveLimitExceeded("archive declares an unsafe uncompressed size")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
                raw = stream.read(self.max_uncompressed_bytes + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ArchiveIntegrityError("archive gzip stream is invalid") from exc
        if len(raw) > self.max_uncompressed_bytes:
            raise ArchiveLimitExceeded("archive expands beyond the safety limit")
        if (
            len(raw) != manifest.uncompressed_size_bytes
            or content_hash_bytes(raw) != manifest.content_hash
        ):
            raise ArchiveIntegrityError("archive canonical payload hash mismatch")

        try:
            lines = raw.decode("utf-8").splitlines()
            header = json.loads(lines[0])
            records = [json.loads(line) for line in lines[1:]]
        except (UnicodeDecodeError, json.JSONDecodeError, IndexError) as exc:
            raise ArchiveIntegrityError("archive JSONL is invalid") from exc
        if (
            header.get("record_type") != "archive_header"
            or header.get("format") != ARCHIVE_FORMAT
            or header.get("archive_schema_version") != ARCHIVE_SCHEMA_VERSION
            or header.get("tenant_id") != manifest.tenant_id
            or header.get("task_id") != manifest.task_id
            or header.get("event_count") != manifest.event_count
            or len(records) != manifest.event_count
        ):
            raise ArchiveIntegrityError("archive header does not match its manifest")

        documents: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records:
            if record.get("record_type") != "agent_event":
                raise ArchiveIntegrityError("archive contains an unknown record type")
            document = record.get("event")
            if not isinstance(document, dict):
                raise ArchiveIntegrityError("archive event record is not an object")
            event_id = str(document.get("id", ""))
            if (
                not event_id
                or event_id in seen
                or document.get("tenant_id") != manifest.tenant_id
                or document.get("task_id") != manifest.task_id
            ):
                raise ArchiveIntegrityError("archive event identity or scope is invalid")
            self._event_from_document(document)
            seen.add(event_id)
            documents.append(document)
        if (
            not documents
            or documents[0]["id"] != manifest.first_event_id
            or documents[-1]["id"] != manifest.last_event_id
            or float(documents[0]["created_at"]) != manifest.first_created_at
            or float(documents[-1]["created_at"]) != manifest.last_created_at
        ):
            raise ArchiveIntegrityError("archive event boundaries do not match manifest")

        if update_verification:
            verified_at = now_epoch()
            if manifest.status is ArchiveStatus.ACTIVE:
                manifest.status = ArchiveStatus.VERIFIED
            manifest.last_verified_at = verified_at
            manifest.updated_at = verified_at
            self.authority.put_archive_manifest(manifest)
        return documents

    @staticmethod
    def _event_from_document(document: Mapping[str, Any]) -> AgentEvent:
        required = {
            "id",
            "tenant_id",
            "task_id",
            "run_id",
            "step_id",
            "event_type",
            "content_text",
            "content_json",
            "artifact_uri",
            "content_hash",
            "token_count",
            "caused_by_event_id",
            "correlation_id",
            "idempotency_key",
            "created_at",
            "schema_version",
        }
        if not required.issubset(document):
            raise ArchiveIntegrityError("archive event is missing required fields")
        content_json = document["content_json"]
        if not isinstance(content_json, dict):
            raise ArchiveIntegrityError("archive event content_json must be an object")
        try:
            return AgentEvent(
                id=str(document["id"]),
                tenant_id=str(document["tenant_id"]),
                task_id=str(document["task_id"]),
                run_id=str(document["run_id"]),
                step_id=str(document["step_id"]),
                event_type=EventType(str(document["event_type"])),
                content_text=str(document["content_text"]),
                content_json=dict(content_json),
                artifact_uri=str(document["artifact_uri"]),
                content_hash=str(document["content_hash"]),
                token_count=int(document["token_count"]),
                caused_by_event_id=str(document["caused_by_event_id"]),
                correlation_id=str(document["correlation_id"]),
                idempotency_key=str(document["idempotency_key"]),
                created_at=float(document["created_at"]),
                schema_version=int(document["schema_version"]),
            )
        except (TypeError, ValueError) as exc:
            raise ArchiveIntegrityError("archive event field types are invalid") from exc


__all__ = [
    "ARCHIVE_FORMAT",
    "ARCHIVE_SCHEMA_VERSION",
    "ArchiveError",
    "ArchiveIntegrityError",
    "ArchiveLimitExceeded",
    "ArchiveRestoreConflict",
    "EventArchiveManager",
]
