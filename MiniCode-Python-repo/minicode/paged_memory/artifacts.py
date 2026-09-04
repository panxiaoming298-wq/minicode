"""Artifact adapters, range reads and encrypted outage spool."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from .config import PagedMemoryConfig
from .models import (
    ArtifactRecord,
    ArtifactStatus,
    content_hash_bytes,
    new_id,
)

_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactStoreUnavailable(ArtifactStoreError):
    pass


class ArtifactIntegrityError(ArtifactStoreError):
    pass


def _component(value: str) -> str:
    if not value or not _SAFE.fullmatch(value):
        raise ValueError(f"unsafe artifact path component: {value!r}")
    if len(value) <= 64:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{value[:32]}-{digest}"


def _select_bytes(
    data: bytes,
    *,
    start: int,
    end: int,
    unit: str,
    query: str = "",
    log_level: str = "",
) -> bytes:
    if start < 0 or end < start:
        raise ValueError("artifact range must satisfy 0 <= start <= end")
    unit = unit.upper()
    if unit == "BYTE":
        return data[start:end]
    text = data.decode("utf-8")
    if query:
        lowered = query.casefold()
        lines = text.splitlines()
        selected: list[str] = []
        for index, line in enumerate(lines):
            if lowered in line.casefold():
                selected.extend(lines[max(0, index - 2) : min(len(lines), index + 3)])
        text = "\n".join(dict.fromkeys(selected))
    if log_level:
        marker = log_level.upper()
        text = "\n".join(line for line in text.splitlines() if marker in line.upper())
    if unit == "CHAR":
        return text[start:end].encode("utf-8")
    if unit == "LINE":
        return "\n".join(text.splitlines()[start:end]).encode("utf-8")
    if unit == "PAGE":
        pages = text.split("\f")
        return "\f".join(pages[start:end]).encode("utf-8")
    raise ValueError("artifact range unit must be BYTE, CHAR, LINE, or PAGE")


class FileArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self,
        data: bytes,
        *,
        artifact_id: str = "",
        tenant_id: str,
        task_id: str,
        run_id: str,
        step_id: str,
        media_type: str,
        artifact_type: str,
    ) -> ArtifactRecord:
        artifact_id = _component(artifact_id) if artifact_id else new_id("artifact")
        # run_id/step_id remain authoritative columns on ArtifactRecord.  The
        # object key only needs tenant/task scope plus a collision-resistant ID;
        # this avoids Windows MAX_PATH failures on deeply nested workspaces.
        relative = Path(
            _component(tenant_id), _component(task_id), f"{artifact_id}.blob"
        )
        target = self._resolve(relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        # The destination is deterministic for idempotent retries, while each
        # writer needs a private staging file so concurrent retries cannot
        # remove or replace one another's temporary data.
        upload_nonce = new_id("upload")[-12:]
        temporary = target.with_name(f".{artifact_id[-12:]}.{upload_nonce}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            try:
                target.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return ArtifactRecord(
            id=artifact_id,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            artifact_type=artifact_type,
            uri="file:" + relative.as_posix(),
            content_hash=content_hash_bytes(data),
            media_type=media_type,
            size_bytes=len(data),
            metadata={"backend": "file"},
            status=ArtifactStatus.ACTIVE,
        )

    def get_bytes(self, artifact: ArtifactRecord) -> bytes:
        if not artifact.uri.startswith("file:"):
            raise ArtifactIntegrityError("artifact URI is not owned by the file store")
        target = self._resolve(artifact.uri.removeprefix("file:"))
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise ArtifactStoreUnavailable(
                f"artifact unavailable: {artifact.id}"
            ) from exc
        if (
            len(data) != artifact.size_bytes
            or content_hash_bytes(data) != artifact.content_hash
        ):
            raise ArtifactIntegrityError(
                f"artifact integrity check failed: {artifact.id}"
            )
        return data

    def read_range(
        self,
        artifact: ArtifactRecord,
        *,
        start: int,
        end: int,
        unit: str = "BYTE",
        query: str = "",
        log_level: str = "",
    ) -> bytes:
        return _select_bytes(
            self.get_bytes(artifact),
            start=start,
            end=end,
            unit=unit,
            query=query,
            log_level=log_level,
        )

    def delete(self, artifact: ArtifactRecord) -> None:
        target = self._resolve(artifact.uri.removeprefix("file:"))
        try:
            target.unlink()
        except FileNotFoundError:
            return

    def list_objects(self) -> Iterable[tuple[str, float]]:
        if not self.root.exists():
            return ()
        values: list[tuple[str, float]] = []
        for path in self.root.rglob("*.blob"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self.root)
            except (OSError, ValueError):
                continue
            values.append(
                (
                    "file:" + resolved.relative_to(self.root).as_posix(),
                    path.stat().st_mtime,
                )
            )
        return tuple(values)

    def health_check(self) -> bool:
        try:
            return self.root.is_dir() and os.access(self.root, os.R_OK | os.W_OK)
        except OSError:
            return False

    def _resolve(self, relative: str) -> Path:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise ArtifactIntegrityError("artifact path escapes store root")
        target = (self.root / Path(*pure.parts)).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactIntegrityError("artifact path escapes store root") from exc
        current = target
        while current != self.root:
            if current.exists() and current.is_symlink():
                raise ArtifactIntegrityError("artifact path contains a symbolic link")
            current = current.parent
        return target


class S3ArtifactStore:
    """S3/MinIO adapter using boto3 with content-hash verification."""

    def __init__(self, config: PagedMemoryConfig) -> None:
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:
            raise RuntimeError(
                "S3/MinIO artifacts require the optional 'memory' dependencies"
            ) from exc
        scheme = "https" if config.artifact_secure else "http"
        endpoint = config.artifact_endpoint
        if endpoint and "://" not in endpoint:
            endpoint = f"{scheme}://{endpoint}"
        self.bucket = config.artifact_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            aws_access_key_id=config.artifact_access_key or None,
            aws_secret_access_key=config.artifact_secret_key or None,
            region_name=config.artifact_region,
            config=BotoConfig(
                connect_timeout=10, read_timeout=30, retries={"max_attempts": 3}
            ),
        )

    def put_bytes(
        self,
        data: bytes,
        *,
        artifact_id: str = "",
        tenant_id: str,
        task_id: str,
        run_id: str,
        step_id: str,
        media_type: str,
        artifact_type: str,
    ) -> ArtifactRecord:
        artifact_id = _component(artifact_id) if artifact_id else new_id("artifact")
        key = "/".join(
            [_component(tenant_id), _component(task_id), f"{artifact_id}.blob"]
        )
        digest = content_hash_bytes(data)
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=media_type,
                Metadata={"sha256": digest.removeprefix("sha256:")},
            )
        except Exception as exc:
            raise ArtifactStoreUnavailable("S3/MinIO artifact upload failed") from exc
        return ArtifactRecord(
            id=artifact_id,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            artifact_type=artifact_type,
            uri=f"s3://{self.bucket}/{key}",
            content_hash=digest,
            media_type=media_type,
            size_bytes=len(data),
            metadata={"backend": "s3", "bucket": self.bucket, "key": key},
            status=ArtifactStatus.ACTIVE,
        )

    def get_bytes(self, artifact: ArtifactRecord) -> bytes:
        bucket, key = self._location(artifact)
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
            data = response["Body"].read()
        except Exception as exc:
            raise ArtifactStoreUnavailable(
                f"artifact unavailable: {artifact.id}"
            ) from exc
        if (
            len(data) != artifact.size_bytes
            or content_hash_bytes(data) != artifact.content_hash
        ):
            raise ArtifactIntegrityError(
                f"artifact integrity check failed: {artifact.id}"
            )
        return data

    def read_range(
        self,
        artifact: ArtifactRecord,
        *,
        start: int,
        end: int,
        unit: str = "BYTE",
        query: str = "",
        log_level: str = "",
    ) -> bytes:
        # Only a whole-object digest is registered today.  A remote Range
        # response cannot be authenticated independently, so verify the full
        # authoritative object before selecting a page.  Per-chunk digests can
        # later restore true remote range I/O without weakening integrity.
        return _select_bytes(
            self.get_bytes(artifact),
            start=start,
            end=end,
            unit=unit,
            query=query,
            log_level=log_level,
        )

    def delete(self, artifact: ArtifactRecord) -> None:
        bucket, key = self._location(artifact)
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:
            raise ArtifactStoreUnavailable(
                f"artifact deletion failed: {artifact.id}"
            ) from exc

    def list_objects(self) -> Iterable[tuple[str, float]]:
        values: list[tuple[str, float]] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket):
                for item in page.get("Contents", []):
                    stamp = item.get("LastModified")
                    values.append(
                        (
                            f"s3://{self.bucket}/{item['Key']}",
                            stamp.timestamp() if stamp else 0.0,
                        )
                    )
        except Exception as exc:
            raise ArtifactStoreUnavailable("S3/MinIO listing failed") from exc
        return tuple(values)

    def health_check(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return True
        except Exception:  # noqa: BLE001 - SDKs expose backend-specific failures.
            return False

    @staticmethod
    def _location(artifact: ArtifactRecord) -> tuple[str, str]:
        if not artifact.uri.startswith("s3://"):
            raise ArtifactIntegrityError("artifact URI is not owned by S3")
        path = artifact.uri.removeprefix("s3://")
        bucket, separator, key = path.partition("/")
        if not separator or not bucket or not key or ".." in PurePosixPath(key).parts:
            raise ArtifactIntegrityError("invalid S3 artifact URI")
        return bucket, key


class EncryptedSpoolArtifactStore(FileArtifactStore):
    """Bounded AES-GCM spool used only when the remote artifact store is down."""

    _MAGIC = b"MCSP1"

    def __init__(
        self,
        root: str | Path,
        *,
        encoded_key: str,
        max_bytes: int,
        min_free_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        super().__init__(root)
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise RuntimeError(
                "encrypted outage spool requires the optional 'memory' dependencies"
            ) from exc
        try:
            key = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
        except Exception as exc:
            raise ValueError("MEMORY_LOCAL_SPOOL_KEY must be URL-safe base64") from exc
        if len(key) not in {16, 24, 32}:
            raise ValueError(
                "MEMORY_LOCAL_SPOOL_KEY must decode to 16, 24, or 32 bytes"
            )
        self._aesgcm = AESGCM(key)
        self.max_bytes = max_bytes
        self.min_free_bytes = max(0, min_free_bytes)

    def put_bytes(
        self,
        data: bytes,
        *,
        artifact_id: str = "",
        tenant_id: str,
        task_id: str,
        run_id: str,
        step_id: str,
        media_type: str,
        artifact_type: str,
    ) -> ArtifactRecord:
        current_size = sum(
            path.stat().st_size for path in self.root.rglob("*.blob") if path.is_file()
        )
        if current_size + len(data) + 64 > self.max_bytes:
            raise ArtifactStoreUnavailable("encrypted artifact spool capacity exceeded")
        required = len(data) + 64 + self.min_free_bytes
        if shutil.disk_usage(self.root).free < required:
            raise ArtifactStoreUnavailable(
                "encrypted artifact spool disk-space reserve would be violated"
            )
        nonce = os.urandom(12)
        encrypted = self._MAGIC + nonce + self._aesgcm.encrypt(nonce, data, None)
        record = super().put_bytes(
            encrypted,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            media_type=media_type,
            artifact_type=artifact_type,
        )
        record.content_hash = content_hash_bytes(data)
        record.size_bytes = len(data)
        record.status = ArtifactStatus.PENDING_UPLOAD
        record.uri = record.uri.replace("file:", "spool:", 1)
        record.metadata.update(
            {"backend": "encrypted-spool", "encrypted_size": len(encrypted)}
        )
        return record

    def get_bytes(self, artifact: ArtifactRecord) -> bytes:
        stored = ArtifactRecord(
            **{
                **artifact.__dict__,
                "uri": artifact.uri.replace("spool:", "file:", 1),
                "size_bytes": int(artifact.metadata["encrypted_size"]),
                "content_hash": "",
            }
        )
        target = self._resolve(stored.uri.removeprefix("file:"))
        encrypted = target.read_bytes()
        if not encrypted.startswith(self._MAGIC):
            raise ArtifactIntegrityError("invalid encrypted spool header")
        nonce = encrypted[len(self._MAGIC) : len(self._MAGIC) + 12]
        try:
            data = self._aesgcm.decrypt(nonce, encrypted[len(self._MAGIC) + 12 :], None)
        except Exception as exc:
            raise ArtifactIntegrityError(
                "encrypted spool authentication failed"
            ) from exc
        if (
            len(data) != artifact.size_bytes
            or content_hash_bytes(data) != artifact.content_hash
        ):
            raise ArtifactIntegrityError("encrypted spool content hash mismatch")
        return data

    def delete(self, artifact: ArtifactRecord) -> None:
        target = self._resolve(artifact.uri.removeprefix("spool:"))
        try:
            target.unlink()
        except FileNotFoundError:
            pass

    def list_objects(self) -> Iterable[tuple[str, float]]:
        return tuple(
            (uri.replace("file:", "spool:", 1), modified_at)
            for uri, modified_at in super().list_objects()
        )


def build_artifact_store(config: PagedMemoryConfig):
    if config.artifact_store_type == "file":
        return FileArtifactStore(config.artifact_root)
    return S3ArtifactStore(config)


def build_encrypted_spool(config: PagedMemoryConfig):
    if not config.local_spool_enabled or not config.local_spool_key:
        return None
    return EncryptedSpoolArtifactStore(
        config.local_spool_root,
        encoded_key=config.local_spool_key,
        max_bytes=config.local_spool_max_bytes,
        min_free_bytes=config.local_spool_min_free_bytes,
    )
