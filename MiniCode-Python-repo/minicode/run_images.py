"""Immutable, session-owned pictures produced by tools, separate from model input."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import stat
import time
import warnings
from functools import lru_cache
from pathlib import Path

from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_artifacts import ArtifactIntegrityError, RuntimeArtifactStore
from minicode.store_lock import serialized_sessions

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_DISPLAY_RECORD_BYTES = 16 * 1024  # Identity/caption plus bounded capture provenance.
MAX_CLEARED_RECORD_BYTES = MAX_DISPLAY_RECORD_BYTES + 128  # Reserve the tombstone without truncating old records.
MAX_TOOL_IMAGES = 24
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class ImageClearedError(ArtifactIntegrityError):
    """A durable clearing record, distinct from an unreadable or missing file."""


def image_storage_inventory(root: Path) -> tuple[list[Path], list[Path]]:
    """Validate the complete owned tree before clearing pixels or a session."""
    store = RuntimeArtifactStore(root)
    root = store.root
    store._ensure_safe_path(root)
    if not root.exists():
        return [], []
    files, directories = [], []

    def visit(directory: Path, relative: str) -> None:
        store._ensure_safe_path(directory)
        if not directory.is_dir():
            raise ArtifactIntegrityError("图片存储目录不可用。")
        directories.append(directory)
        for path in directory.iterdir():
            store._ensure_safe_path(path, reject_hardlinks=True)
            child = f"{relative}/{path.name}".lstrip("/")
            if child in ("outputs", "outputs/images"):
                visit(path, child)
            elif relative == "outputs/images" and path.is_file() and re.fullmatch(
                r"(?:artifact-[a-f0-9]{32}\.(?:blob|json|display\.json)|\.tmp-[a-f0-9]{32})", path.name
            ):
                files.append(path)
            else:
                raise ArtifactIntegrityError("图片目录包含无法确认归属的文件，未执行清理。")

    visit(root, "")
    return sorted(files), list(reversed(directories))


@lru_cache(maxsize=64)
def _display_records(root: str, session_id: str, directory_version: int) -> tuple[dict, ...]:
    """Directory mtime invalidates the cache on each atomic image publication."""
    store = RuntimeArtifactStore(Path(root))
    records = []
    for path in (store.root / "outputs" / "images").glob("artifact-*.display.json"):
        try:
            store._ensure_safe_path(path, reject_hardlinks=True)
            record_size = path.stat().st_size
            if record_size > MAX_CLEARED_RECORD_BYTES:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("sessionId") != session_id
                or (record_size > MAX_DISPLAY_RECORD_BYTES and value.get("availability") != "cleared")
                or not re.fullmatch(r"artifact-[a-f0-9]{32}", str(value.get("id", "")))
                or value.get("mediaType") not in IMAGE_TYPES.values()
                or any(not isinstance(value.get(key), str) for key in ("name", "caption"))
                or any(type(value.get(key)) is not int or value[key] <= 0
                       for key in ("size", "width", "height", "createdAt"))):
                continue
            records.append(value)
        except (OSError, ValueError, ArtifactIntegrityError):
            continue
    return tuple(sorted(records, key=lambda item: (item["createdAt"], item["id"])))


def inspect_image(data: bytes) -> dict:
    """Decode complete bounded raster images; never serve active SVG/HTML as a picture."""
    from PIL import Image, UnidentifiedImageError
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 16 MB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                if picture.format not in IMAGE_TYPES or getattr(picture, "n_frames", 1) != 1:
                    raise ValueError("仅支持 PNG、JPEG、WebP 和静态 GIF 图片。")
                if max(picture.size) > 16384 or picture.width * picture.height > 32_000_000:
                    raise ValueError("图片尺寸过大，请缩小后展示。")
                meta = {"mediaType": IMAGE_TYPES[picture.format], "width": picture.width, "height": picture.height}
                picture.load()
            # PNG verification also checks the terminal chunk and its checksum.
            with Image.open(io.BytesIO(data)) as picture:
                picture.verify()
            return meta
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise ValueError("图片尚未写完或内容无法解码。") from error


def read_image_file(path: Path) -> bytes:
    """Read a regular non-aliased file, checking identity across the read."""
    path = path.absolute()
    if is_unsafe_stored_path(path, boundary=Path(path.anchor), reject_hardlinks=True):
        raise ValueError("图片路径包含链接或特殊文件。")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_IMAGE_BYTES:
        raise ValueError("图片必须是最多 16 MB 的普通文件。")
    with path.open("rb") as source:
        import os
        opened = os.fstat(source.fileno())
        data = source.read(MAX_IMAGE_BYTES + 1)
        after_read = os.fstat(source.fileno())
    after = path.stat()
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if (len(data) > MAX_IMAGE_BYTES or len(data) != before.st_size
        or len({signature(value) for value in (before, opened, after_read, after)}) != 1
        or is_unsafe_stored_path(path, boundary=Path(path.anchor), reject_hardlinks=True)):
        raise ValueError("图片仍在写入，请在保存完成后重试。")
    return data


class RunImageStore:
    def __init__(self, session_id: str):
        from minicode import session as sessions
        if not sessions._valid_session_id(session_id):
            raise ValueError("无效的图片会话。")
        self.session_id = session_id
        self.root = sessions.SESSIONS_DIR / session_id / "tool-images"
        self.store = RuntimeArtifactStore(self.root)

    @serialized_sessions
    def save(self, data: bytes, *, name: str, caption: str = "", identity: dict | None = None,
             captured_at: int | None = None, capture: dict | None = None) -> dict:
        dimensions = inspect_image(data)
        ref = self.store.put_bytes(data, run_id="outputs", node_id="images", media_type=dimensions["mediaType"])
        clean_name = re.split(r"[/\\]", name)[-1]
        clean_name = re.sub(r"[\x00-\x1f\x7f]", "", clean_name)[:180] or "screenshot.png"
        public = {"id": ref.artifact_id, "sessionId": self.session_id, "name": clean_name,
                  "caption": str(caption)[:500], "size": ref.size, **dimensions,
                  "createdAt": captured_at if captured_at is not None else round(time.time() * 1000)}
        if capture is not None:
            from minicode.browser_capture import capture_metadata
            if metadata := capture_metadata(capture, data):
                public["capture"] = metadata
        for key in ("runId", "webRunId", "turnId", "nodeId", "attemptId", "toolCallId", "toolName", "sourceImageId"):
            value = (identity or {}).get(key)
            if isinstance(value, str) and value:
                public[key] = value[:200]
        source_time = (identity or {}).get("sourceCreatedAt")
        if type(source_time) is int and source_time > 0:
            public["sourceCreatedAt"] = source_time
        encoded = json.dumps(public, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_DISPLAY_RECORD_BYTES:
            raise ValueError("图片来源记录过大，无法保存为可读取的历史记录。")
        self.store._atomic_write(self.store.root / "outputs" / "images" / f"{ref.artifact_id}.display.json", encoded)
        return public

    def _metadata(self, image_id: str) -> dict:
        if not re.fullmatch(r"artifact-[a-f0-9]{32}", image_id):
            raise FileNotFoundError("图片不存在。")
        path = self.store.root / "outputs" / "images" / f"{image_id}.display.json"
        self.store._ensure_safe_path(path, reject_hardlinks=True)
        record_size = path.stat().st_size
        if record_size > MAX_CLEARED_RECORD_BYTES:
            raise ArtifactIntegrityError("图片来源记录超过读取上限。")
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("id") != image_id or metadata.get("sessionId") != self.session_id:
            raise ArtifactIntegrityError("图片记录与所属对话不匹配。")
        if record_size > MAX_DISPLAY_RECORD_BYTES and metadata.get("availability") != "cleared":
            raise ArtifactIntegrityError("图片来源记录超过读取上限。")
        return metadata

    def get(self, image_id: str) -> tuple[dict, bytes]:
        metadata = self._metadata(image_id)
        if metadata.get("availability") == "cleared":
            raise ImageClearedError("图片已清理。")
        blob = self.store.root / "outputs" / "images" / f"{image_id}.blob"
        self.store._ensure_safe_path(blob, reject_hardlinks=True)
        if not blob.is_file():
            raise FileNotFoundError("图片文件缺失。")
        ref = self.store.owned_reference(image_id, run_id="outputs", node_id="images")
        if ref is None:
            raise FileNotFoundError("图片记录缺失。")
        if (ref.media_type not in IMAGE_TYPES.values() or ref.size > MAX_IMAGE_BYTES
            or metadata.get("mediaType") != ref.media_type or metadata.get("size") != ref.size):
            raise ArtifactIntegrityError("图片记录与内容不匹配。")
        return metadata, self.store.get_bytes(ref)

    @serialized_sessions
    def availability(self, image_id: str) -> dict:
        try:
            self.get(image_id)
            status = "available"
        except ImageClearedError:
            metadata = self._metadata(image_id)
            return {"id": image_id, "status": "cleared", "clearedAt": metadata.get("clearedAt")}
        except FileNotFoundError:
            status = "missing"
        except (ArtifactIntegrityError, ValueError, TypeError) as error:
            # Permission/I/O failures can be temporary; never label them deleted.
            if isinstance(error.__cause__, OSError) and not isinstance(error.__cause__, FileNotFoundError):
                raise error.__cause__
            status = "corrupt"
        return {"id": image_id, "status": status}

    @serialized_sessions
    def storage_summary(self) -> dict:
        files, _ = image_storage_inventory(self.root)
        blobs = [path for path in files if path.suffix == ".blob"]
        revision = hashlib.sha256(json.dumps([(path.name, path.stat().st_size, path.stat().st_mtime_ns)
                                             for path in files]).encode("utf-8")).hexdigest()
        return {"sessionId": self.session_id, "storedCount": len(blobs),
                "storedBytes": sum(path.stat().st_size for path in blobs), "revision": revision}

    @serialized_sessions
    def clear(self, *, expected_revision: str | None = None) -> dict:
        """Clear only this session's pixels; retain history and durable tombstones."""
        files, _ = image_storage_inventory(self.root)
        if expected_revision is not None and self.storage_summary()["revision"] != expected_revision:
            raise ValueError("图片用量已变化，请核对更新后的数量再确认。")
        blobs = [path for path in files if path.suffix == ".blob"]
        released_bytes = sum(path.stat().st_size for path in blobs)
        cleared_at = round(time.time() * 1000)
        # Commit every readable display record first. An interrupted deletion
        # stays visibly cleared and a retry still removes remaining payloads.
        for path in files:
            if not path.name.endswith(".display.json"):
                continue
            try:
                metadata = self._metadata(path.name.removesuffix(".display.json"))
            except (ValueError, TypeError, ArtifactIntegrityError):
                continue
            if metadata.get("availability") != "cleared":
                metadata.update(availability="cleared", clearedAt=cleared_at)
                self.store._atomic_write(path, json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
        for path in blobs:
            self.store._ensure_safe_path(path, reject_hardlinks=True)
            path.unlink(missing_ok=True)
        return {"clearedCount": len(blobs), "releasedBytes": released_bytes}

    def records(self, *, turn_id: str = "", web_run_id: str = "") -> list[dict]:
        """Recover published pictures even when the tool or worker never returns."""
        directory = self.store.root / "outputs" / "images"
        try:
            self.store._ensure_safe_path(directory)
            version = directory.stat().st_mtime_ns
            records = _display_records(str(self.store.root), self.session_id, version)
        except (OSError, ValueError, ArtifactIntegrityError):
            # A damaged image directory must not prevent opening the conversation.
            return []
        return [dict(value) for value in records if (turn_id or web_run_id)
                and (not turn_id or value.get("turnId") == turn_id)
                and (not web_run_id or value.get("webRunId") == web_run_id)]


def publish_image(context, data: bytes, *, name: str, caption: str = "", tool_name: str = "",
                  source_image: dict | None = None, capture: dict | None = None) -> dict:
    session_id = getattr(context.session, "session_id", None)
    if not session_id:
        raise ValueError("图片展示需要一个已保存的会话。")
    runtime = context._runtime or {}
    record = RunImageStore(session_id).save(data, name=name, caption=caption,
        capture=source_image.get("capture") if source_image else capture, identity={
        "runId": context.run_id or runtime.get("runtimeRunId", ""),
        "webRunId": runtime.get("_webRunId", ""),
        "turnId": runtime.get("_webTurnId", ""), "nodeId": context.node_id or "",
        "attemptId": context.attempt_id or "", "toolCallId": context.tool_call_id or "",
        "toolName": tool_name,
        **({"sourceImageId": source_image["id"],
            "sourceCreatedAt": source_image.get("sourceCreatedAt", source_image["createdAt"])} if source_image else {}),
    })
    # Persist first; a disconnected UI must never discard an already captured picture.
    if context.image_observer is not None:
        try:
            context.image_observer([record])
        except Exception:
            logging.getLogger(__name__).warning("Image event delivery failed; saved image remains available", exc_info=True)
    return record


def image_messages(records: list[dict]) -> list[dict]:
    return [{"role": "tool_image", "images": [record], "toolName": record.get("toolName", ""),
             "toolUseId": record.get("toolCallId", ""), "executionId": record.get("runId", ""),
             "nodeId": record.get("nodeId", ""), "attemptId": record.get("attemptId", "")}
            for record in records]


def publish_project_check_images(records, context, artifact_store=None) -> tuple[list[dict], list[str]]:
    """Expose only this run's native browser artifacts through the saved-image reader."""
    from minicode.runtime_protocol import ArtifactRef
    if not getattr(context.session, "session_id", None):
        return [], []
    store = artifact_store or RuntimeArtifactStore()
    images, errors = [], []
    for record in records:
        if record.verifier != "npm-build-and-browser" and not record.verifier.startswith("browser-scenario-"):
            continue
        try:
            report = json.loads(record.details)
            captures = {item["name"]: item["capture"] for item in (report.get("browser") or {}).get("images", [])}
            for item in report.get("artifacts", []):
                if not re.fullmatch(r"(desktop|mobile)(?:-[A-Za-z0-9_-]{1,64})?\.png", str(item["name"])):
                    continue
                ref = ArtifactRef(**item["ref"])
                owned = store.owned_reference(ref.artifact_id, run_id=context.run_id, node_id="npm-build-and-browser")
                if owned != ref or ref.media_type != "image/png":
                    raise ValueError("Browser image does not belong to this run's native check")
                images.append(publish_image(context, store.get_bytes(ref), name=item["name"],
                    tool_name="run_acceptance_checks", capture=captures.get(item["name"])))
        except (OSError, ValueError, KeyError, TypeError, ArtifactIntegrityError) as error:
            errors.append(str(error)[:500])
    return images, errors


def fork_images(source_id: str, child_id: str, turn_id: str) -> list[dict]:
    """Forked conversations own their pixels even after the parent is deleted."""
    source, child = RunImageStore(source_id), RunImageStore(child_id)
    copies = []
    for record in source.records(turn_id=turn_id):
        try:
            metadata, data = source.get(record["id"])
            copies.append(child.save(data, name=metadata["name"], caption=metadata["caption"],
                                     identity=metadata, captured_at=metadata["createdAt"], capture=metadata.get("capture")))
        except (OSError, ValueError, ArtifactIntegrityError):
            # Keep the original unavailable reference visible with its retry UI.
            copies.append(record)
    return copies
