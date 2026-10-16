"""Lossless recovery patches for unsubmitted isolated work, never acceptance.

Store after-images plus base hashes (including deletions), before cleanup. An
explicit restore creates a new isolated workspace, not an integration or retry.
Oversized/unsafe/unavailable archives raise; the caller must keep the workspace.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import threading

from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_artifacts import ArtifactIntegrityError, RuntimeArtifactStore
from minicode.runtime_protocol import AgentInvocationSpec, ArtifactRef, ToolEffect
from minicode.session_contract import atomic_write_json
from minicode.workspace_provider import (
    TemporaryWorkspaceProvider, WorkspaceConflict, WorkspaceHandle,
    snapshot_revision, workspace_link_aliases, workspace_snapshot,
)

RECOVERY_MEDIA_TYPE = "application/vnd.minicode.unsubmitted-candidate+json"
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_FILES = 512


class CandidateCrashJournal:
    """Register owned work before tools run; never grants replay or acceptance.

    The OS run lease must be acquired before consuming these descriptors.
    An incomplete tool receipt or any external effect fences continuation.
    """

    @staticmethod
    def directory(artifacts: RuntimeArtifactStore, run_id: str) -> Path:
        artifacts._validate_component(run_id)
        path = artifacts.root / ".candidate-journals" / run_id
        if is_unsafe_stored_path(path, boundary=artifacts.root):
            raise WorkspaceConflict("unsafe crash journal path")
        return path

    def __init__(self, artifacts: RuntimeArtifactStore, invocation: AgentInvocationSpec,
                 handle: WorkspaceHandle):
        artifacts._validate_component(invocation.attempt_id)
        self.path = self.directory(artifacts, invocation.run_id) / (invocation.attempt_id + ".json")
        if is_unsafe_stored_path(self.path, boundary=artifacts.root, reject_hardlinks=True):
            raise WorkspaceConflict("unsafe crash journal")
        self.lock = threading.Lock()
        self.value = {"schema": "candidate-crash-journal-v1", "run_id": invocation.run_id,
            "node_id": invocation.node_id, "attempt_id": invocation.attempt_id,
            "plan_version": invocation.plan_version,
            "contract_revision": invocation.context_manifest.contract_revision,
            "source": str(handle.source), "workspace": str(handle.workspace),
            "isolated": handle.isolated, "owner": handle.owner_token,
            "base_hashes": dict(handle.base_hashes), "pending_tools": {}, "external_effects": []}
        # A repeat attempt must not erase an unresolved earlier tool receipt.
        if self.path.exists():
            raise WorkspaceConflict("crash journal already belongs to an activation")
        atomic_write_json(self.path, self.value)

    def started(self, name: str, effect: ToolEffect) -> None:
        if effect is ToolEffect.READ_ONLY:
            return
        with self.lock:
            pending = self.value["pending_tools"]
            pending[name] = pending.get(name, 0) + 1
            if effect in {ToolEffect.EXTERNAL_SIDE_EFFECT, ToolEffect.PRIVILEGED}:
                if name not in self.value["external_effects"]:
                    self.value["external_effects"].append(name)
            atomic_write_json(self.path, self.value)

    def finished(self, name: str) -> None:
        with self.lock:
            pending = self.value["pending_tools"]
            if name not in pending:
                return
            pending[name] -= 1
            if pending[name] == 0:
                del pending[name]
            atomic_write_json(self.path, self.value)

    def archived(self, ref: ArtifactRef | None) -> None:
        """The cleanup gate: persist the archive identity before removing files."""
        with self.lock:
            self.value.update(archive_complete=True, archive=ref.to_dict() if ref else None)
            atomic_write_json(self.path, self.value)


def _relative_file(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
        or PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise WorkspaceConflict("invalid recovery resource path")
    return value


class CandidateRecoveryStore:
    def __init__(self, artifacts: RuntimeArtifactStore) -> None:
        self.artifacts = artifacts

    def restore_submitted(self, ref: ArtifactRef, *, source: Path, provider: TemporaryWorkspaceProvider,
                          owner_token: str) -> WorkspaceHandle:
        """Explicit fresh attempt from retained bytes; never replay old authority."""
        if ref.media_type != "application/vnd.minicode.primary-repair-checkpoint+json":
            raise ArtifactIntegrityError("not a submitted primary checkpoint")
        payload = json.loads(self.artifacts.get_text(ref))
        descriptor = payload.get("workspace") or {}
        if (payload.get("schema") != "primary-repair-checkpoint-v1" or payload.get("node_id") != "primary"
                or PurePosixPath(ref.store_key).parent != PurePosixPath(payload.get("run_id", "")) / "primary"
                or descriptor.get("isolated") is not True or Path(descriptor.get("source", "")).resolve() != source.resolve()):
            raise WorkspaceConflict("submitted checkpoint ownership mismatch")
        original = provider.reopen(source=source, workspace=Path(descriptor["path"]),
            owner_token=descriptor["owner"], base_hashes=descriptor["base_hashes"], expected_revision=payload["candidate_revision"])
        if workspace_snapshot(source) != original.base_hashes:
            raise WorkspaceConflict("source no longer matches the submitted candidate baseline")
        handle = provider.create(original.workspace, owner_token=owner_token)
        try:
            if (snapshot_revision(handle.base_hashes) != payload["candidate_revision"]
                    or snapshot_revision(workspace_snapshot(handle.workspace)) != payload["candidate_revision"]
                    or snapshot_revision(workspace_snapshot(original.workspace)) != payload["candidate_revision"]
                    or workspace_snapshot(source) != original.base_hashes):
                raise WorkspaceConflict("candidate or source changed during explicit continuation")
            handle.source = source.resolve()
            handle.base_hashes = dict(original.base_hashes)
            return handle
        except BaseException:
            provider.cleanup(handle)
            raise

    def archive(self, handle: WorkspaceHandle, invocation: AgentInvocationSpec,
                provider: TemporaryWorkspaceProvider, *, retain_empty: bool = False) -> ArtifactRef | None:
        if not handle.isolated or handle.closed or handle.integrated:
            raise WorkspaceConflict("only an open unintegrated isolated workspace can be archived")
        if self.artifacts.root.resolve().is_relative_to(handle.workspace.resolve()):
            raise WorkspaceConflict("recovery artifacts must be outside the candidate workspace")
        current = workspace_snapshot(handle.workspace)
        revision = snapshot_revision(current)
        # Validate ownership, baseline paths, aliases, and unchanged snapshot.
        provider.reopen(source=handle.source, workspace=handle.workspace,
            owner_token=handle.owner_token, base_hashes=handle.base_hashes, expected_revision=revision)
        changed = handle.changed_resources(current)
        if not changed and not retain_empty:
            return None
        if len(changed) > _MAX_FILES:
            raise WorkspaceConflict("candidate exceeds the recovery file limit; retain workspace")
        patches = []
        total = 0
        for key in changed:
            _relative_file(key)
            path = handle.workspace / key
            ref = None
            if key in current:
                size = path.stat().st_size
                total += size
                if size > _MAX_FILE_BYTES or total > _MAX_TOTAL_BYTES:
                    raise WorkspaceConflict("candidate exceeds the recovery byte limit; retain workspace")
                if is_unsafe_stored_path(path, boundary=handle.workspace, reject_hardlinks=True):
                    raise WorkspaceConflict("recovery resource is not a regular owned file")
                with path.open("rb") as stream:
                    data = stream.read(_MAX_FILE_BYTES + 1)
                if len(data) != size or hashlib.sha256(data).hexdigest() != current[key]:
                    raise WorkspaceConflict("candidate changed during recovery capture")
                ref = self.artifacts.put_bytes(data, run_id=invocation.run_id, node_id=invocation.node_id)
                self.artifacts.get_bytes(ref)  # Verify the stored after-image before cleanup.
            patches.append({"path": key, "base_sha256": handle.base_hashes.get(key),
                            "candidate_sha256": current.get(key), "content": ref.to_dict() if ref else None})
        if workspace_link_aliases(handle.workspace) or workspace_snapshot(handle.workspace) != current:
            raise WorkspaceConflict("candidate changed during recovery capture")
        payload = {"schema": "unsubmitted-candidate-v1", "submitted": False, "verified": False, "integrated": False,
            "run_id": invocation.run_id, "node_id": invocation.node_id, "attempt_id": invocation.attempt_id,
            "plan_version": invocation.plan_version, "contract_revision": invocation.context_manifest.contract_revision,
            "source": str(handle.source), "workspace": str(handle.workspace),
            "owner_sha256": hashlib.sha256(handle.owner_token.encode("utf-8")).hexdigest(),
            "base_revision": snapshot_revision(handle.base_hashes), "base_hashes": handle.base_hashes,
            "candidate_revision": revision, "patches": patches,
            "notice": "Recovery data only. Explicit isolated restore, conflict checks, normal validation and submission are still required."}
        ref = self.artifacts.put_text(json.dumps(payload, ensure_ascii=False, allow_nan=False),
            run_id=invocation.run_id, node_id=invocation.node_id, media_type=RECOVERY_MEDIA_TYPE)
        self.artifacts.get_bytes(ref)
        return ref

    def verify_retained(self, ref: ArtifactRef, *, record, provider: TemporaryWorkspaceProvider) -> dict:
        """Verify a retained after-image for permanent retirement, without integration."""
        if ref.media_type != RECOVERY_MEDIA_TYPE:
            raise ArtifactIntegrityError("not an unsubmitted candidate archive")
        payload = json.loads(self.artifacts.get_text(ref))
        if (payload.get("schema") != "unsubmitted-candidate-v1"
            or any(payload.get(key) != record[key] for key in ("run_id", "node_id", "attempt_id"))
            or any(payload.get(key) is not False for key in ("submitted", "verified", "integrated"))):
            raise ArtifactIntegrityError("retained candidate identity or authority is invalid")
        from minicode.workspace_provider import _OWNER_MARKER
        workspace = Path(payload["workspace"])
        boundary = provider.temporary_root
        if boundary is None or workspace.parent.parent.resolve() != boundary or is_unsafe_stored_path(workspace, boundary=boundary):
            raise WorkspaceConflict("retained candidate is outside the configured owned root")
        marker = workspace.parent / _OWNER_MARKER
        if is_unsafe_stored_path(marker, boundary=boundary, reject_hardlinks=True):
            raise WorkspaceConflict("unsafe candidate ownership marker")
        token = marker.read_text(encoding="utf-8")
        if hashlib.sha256(token.encode("utf-8")).hexdigest() != payload.get("owner_sha256"):
            raise WorkspaceConflict("retained candidate owner changed")
        handle = provider.reopen(source=Path(payload["source"]), workspace=workspace, owner_token=token,
            base_hashes=payload["base_hashes"], expected_revision=payload["candidate_revision"])
        expected = dict(handle.base_hashes)
        for patch in payload["patches"]:
            key = _relative_file(patch["path"])
            if patch.get("content") is None:
                expected.pop(key, None)
            else:
                content = ArtifactRef(**patch["content"])
                if hashlib.sha256(self.artifacts.get_bytes(content)).hexdigest() != patch["candidate_sha256"]:
                    raise ArtifactIntegrityError("retained after-image hash mismatch")
                expected[key] = patch["candidate_sha256"]
        if workspace_snapshot(handle.workspace) != expected or snapshot_revision(expected) != payload["candidate_revision"]:
            raise WorkspaceConflict("retained candidate differs from its durable after-image")
        return {"disposition": "retired_isolated_candidate", "attempt_id": record["attempt_id"],
            "candidate_revision": payload["candidate_revision"], "archive_sha256": ref.content_hash,
            "workspace": str(handle.workspace), "integrated": False}

    def restore_isolated(self, ref: ArtifactRef, *, source: Path, provider: TemporaryWorkspaceProvider,
                         owner_token: str) -> WorkspaceHandle:
        """Operator-invoked restore only. Never apply to source or mark success."""
        if ref.media_type != RECOVERY_MEDIA_TYPE:
            raise ArtifactIntegrityError("not an unsubmitted candidate archive")
        payload = json.loads(self.artifacts.get_text(ref))
        if (not isinstance(payload, dict) or payload.get("schema") != "unsubmitted-candidate-v1"
            or any(payload.get(key) is not False for key in ("submitted", "verified", "integrated"))):
            raise ArtifactIntegrityError("recovery manifest has invalid authority")
        base = payload.get("base_hashes")
        if (not isinstance(base, dict) or snapshot_revision(base) != payload.get("base_revision")
            or workspace_link_aliases(source) or workspace_snapshot(source) != base):
            raise WorkspaceConflict("source no longer matches the recovery baseline")
        for path in base:
            _relative_file(path)
        patches = payload.get("patches")
        if not isinstance(patches, list) or not 1 <= len(patches) <= _MAX_FILES:
            raise ArtifactIntegrityError("invalid recovery patch list")
        contents = {}
        expected = dict(base)
        total = 0
        for patch in patches:
            key = _relative_file(patch["path"])
            if key in contents or patch.get("base_sha256") != base.get(key):
                raise ArtifactIntegrityError("recovery patch baseline or identity mismatch")
            data = None
            if patch.get("content") is not None:
                content_ref = ArtifactRef(**patch["content"])
                total += content_ref.size
                if (content_ref.size > _MAX_FILE_BYTES or total > _MAX_TOTAL_BYTES
                    or PurePosixPath(content_ref.store_key).parent != PurePosixPath(ref.store_key).parent):
                    raise ArtifactIntegrityError("recovery content exceeds bounds or belongs to another Worker")
                data = self.artifacts.get_bytes(content_ref)
                if hashlib.sha256(data).hexdigest() != patch.get("candidate_sha256"):
                    raise ArtifactIntegrityError("recovery after-image hash mismatch")
                expected[key] = patch["candidate_sha256"]
            elif patch.get("candidate_sha256") is not None or key not in base:
                raise ArtifactIntegrityError("invalid recovery deletion")
            else:
                expected.pop(key)
            contents[key] = data
        if snapshot_revision(expected) != payload.get("candidate_revision"):
            raise ArtifactIntegrityError("recovery candidate revision mismatch")
        provider.validate_candidate_root(source)
        handle = provider.create(source, owner_token=owner_token)
        try:
            if handle.base_hashes != base or workspace_snapshot(handle.workspace) != base:
                raise WorkspaceConflict("source changed while creating recovery workspace")
            for key, data in contents.items():
                path = handle.workspace / key
                if is_unsafe_stored_path(path, boundary=handle.workspace, reject_hardlinks=True):
                    raise WorkspaceConflict("recovery destination escapes or aliases the candidate")
                if data is None:
                    path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            if workspace_snapshot(handle.workspace) != expected:
                raise WorkspaceConflict("restored workspace does not match the recovery candidate")
            return handle
        except Exception:
            # Only the new owned restore is cleaned; the archive stays intact.
            provider.cleanup(handle)
            raise
