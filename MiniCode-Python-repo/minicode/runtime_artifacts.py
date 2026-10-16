"""Small content-addressed Artifact/Evidence store for Runtime V2."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from minicode.config import MINI_CODE_DIR
from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_protocol import ArtifactRef, new_id

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class ArtifactIntegrityError(RuntimeError):
    pass


class RuntimeArtifactStore:
    """Persist complete outputs atomically and expose verified references."""

    @staticmethod
    def node_directory(node_id: str) -> str:
        return node_id if len(node_id) <= 32 else "node-" + hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def owns_reference(cls, ref: ArtifactRef, *, run_id: str, node_id: str) -> bool:
        key = ref.store_key.replace("\\", "/")
        return (ref.size > 0 and ".." not in key.split("/")
            and key.startswith(f"{run_id}/{cls.node_directory(node_id)}/"))

    def __init__(self, root: Path | None = None) -> None:
        self.root = (
            Path(root)
            if root is not None
            else MINI_CODE_DIR / "memory-v2" / "runtime-artifacts"
        )
        if os.name == "nt":
            # Run IDs and Builder IDs can push an otherwise valid configured
            # root past MAX_PATH. Keep relative store keys unchanged, but use
            # the extended namespace for all storage and alias checks.
            value = str(self.root.absolute())
            if not value.startswith("\\\\?\\"):
                value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
            self.root = Path(value)

    def put_text(
        self,
        text: str,
        *,
        run_id: str,
        node_id: str,
        media_type: str = "text/plain; charset=utf-8",
    ) -> ArtifactRef:
        return self.put_bytes(
            text.encode("utf-8"),
            run_id=run_id,
            node_id=node_id,
            media_type=media_type,
        )

    def put_bytes(
        self,
        data: bytes,
        *,
        run_id: str,
        node_id: str,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        self._validate_component(run_id)
        self._validate_component(node_id)
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = new_id("artifact")
        # Durable consultation IDs include a full digest. Do not repeat that
        # long logical ID in Windows paths: artifact IDs and content hashes
        # still identify each file, and authorization uses the ArtifactRef,
        # never this storage-only directory name. Old store_keys stay readable.
        node_directory = self.node_directory(node_id)
        directory = self.root / run_id / node_directory
        directory.mkdir(parents=True, exist_ok=True)
        self._ensure_safe_path(directory)
        content_path = directory / f"{artifact_id}.blob"
        metadata_path = directory / f"{artifact_id}.json"
        self._atomic_write(content_path, data)
        ref = ArtifactRef(
            artifact_id=artifact_id,
            content_hash=digest,
            size=len(data),
            media_type=media_type,
            store_key=str(content_path.relative_to(self.root)).replace("\\", "/"),
        )
        self._atomic_write(
            metadata_path,
            (json.dumps(ref.to_dict(), sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return ref

    def get_bytes(self, ref: ArtifactRef) -> bytes:
        path = self._resolve_key(ref.store_key)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ArtifactIntegrityError(f"artifact is unavailable: {ref.artifact_id}") from error
        actual = hashlib.sha256(data).hexdigest()
        if len(data) != ref.size or actual != ref.content_hash:
            raise ArtifactIntegrityError(f"artifact integrity check failed: {ref.artifact_id}")
        return data

    def get_text(self, ref: ArtifactRef) -> str:
        return self.get_bytes(ref).decode("utf-8")

    def owned_reference(self, artifact_id: str, *, run_id: str, node_id: str) -> ArtifactRef | None:
        """Resolve only an explicitly authorized producer's immutable metadata."""
        if not re.fullmatch(r"artifact-[a-f0-9]{32}", artifact_id):
            return None
        self._validate_component(run_id)
        self._validate_component(node_id)
        key = f"{run_id}/{self.node_directory(node_id)}/{artifact_id}"
        metadata = self._resolve_key(key + ".json")
        if not metadata.is_file():
            return None
        self._ensure_safe_path(metadata, reject_hardlinks=True)
        try:
            ref = ArtifactRef(**json.loads(metadata.read_text(encoding="utf-8")))
        except (TypeError, ValueError) as error:
            raise ArtifactIntegrityError("artifact metadata is invalid") from error
        if ref.artifact_id != artifact_id or ref.store_key != key + ".blob":
            raise ArtifactIntegrityError("artifact metadata does not match the authorized producer")
        self.get_bytes(ref)
        return ref

    def read_range(self, ref: ArtifactRef, *, start: int, end: int) -> bytes:
        """Return a verified half-open byte range without changing cursor units."""
        if start < 0 or end < start:
            raise ValueError("artifact range must satisfy 0 <= start <= end")
        return self.get_bytes(ref)[start:end]

    def exists(self, ref: ArtifactRef) -> bool:
        try:
            self.get_bytes(ref)
        except (ArtifactIntegrityError, UnicodeError):
            return False
        return True

    def orphaned_content(self) -> tuple[Path, ...]:
        if not self.root.exists():
            return ()
        orphans: list[Path] = []
        for current, directory_names, file_names in os.walk(self.root, followlinks=False):
            current_path = Path(current)
            directory_names[:] = [
                name
                for name in directory_names
                if not is_unsafe_stored_path(
                    current_path / name,
                    boundary=self.root,
                )
            ]
            for name in file_names:
                if not name.endswith(".blob"):
                    continue
                content = current_path / name
                if is_unsafe_stored_path(
                    content,
                    boundary=self.root,
                    reject_hardlinks=True,
                ):
                    continue
                metadata = content.with_suffix(".json")
                if (
                    is_unsafe_stored_path(
                        metadata,
                        boundary=self.root,
                        reject_hardlinks=True,
                    )
                    or not metadata.is_file()
                ):
                    orphans.append(content)
        return tuple(sorted(orphans))

    def _resolve_key(self, key: str) -> Path:
        candidate = self.root / key
        self._ensure_safe_path(candidate, reject_hardlinks=True)
        return candidate

    def _ensure_safe_path(self, path: Path, *, reject_hardlinks: bool = False) -> None:
        if is_unsafe_stored_path(
            path,
            boundary=self.root,
            reject_hardlinks=reject_hardlinks,
        ):
            raise ArtifactIntegrityError("artifact path escapes or aliases the store root")

    @staticmethod
    def _validate_component(value: str) -> None:
        if not value or not _SAFE_ID.fullmatch(value):
            raise ValueError("artifact run/node identifiers must be path-safe")

    def _atomic_write(self, path: Path, data: bytes) -> None:
        self._ensure_safe_path(path, reject_hardlinks=True)
        # The random name already guarantees exclusive staging. Repeating the
        # target filename can exceed Windows MAX_PATH for an otherwise valid
        # artifact path (especially with nested run / consultation IDs).
        temporary = path.with_name(f".{new_id('tmp')}")
        self._ensure_safe_path(temporary, reject_hardlinks=True)
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
