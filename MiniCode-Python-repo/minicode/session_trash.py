"""Durable recycle-bin markers, separate from autosaved conversation content."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicode.path_safety import is_unsafe_stored_path
from minicode.session_contract import _valid_session_id, atomic_write_json


class SessionTrashError(RuntimeError):
    """The recycle-bin state cannot safely be read or changed."""


@dataclass(frozen=True)
class TrashedSession:
    metadata: dict[str, Any]
    archived_at: float
    deletion_pending: bool = False


class SessionTrash:
    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir
        self.root = sessions_dir / "trash"

    def _path(self, session_id: str) -> Path:
        if not _valid_session_id(session_id):
            raise SessionTrashError("无效的会话标识。")
        path = self.root / f"{session_id}.json"
        if is_unsafe_stored_path(path, boundary=self.sessions_dir, reject_hardlinks=True):
            raise SessionTrashError("回收站路径不安全，操作已停止。")
        return path

    def get(self, session_id: str) -> TrashedSession | None:
        path = self._path(session_id)
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            metadata = data["metadata"]
            archived_at = float(data["archived_at"])
            pending = data.get("deletion_pending", False)
            if (
                not isinstance(metadata, dict)
                or metadata.get("session_id") != session_id
                or not math.isfinite(archived_at)
                or archived_at <= 0
                or not isinstance(pending, bool)
            ):
                raise ValueError("invalid recycle-bin record")
            return TrashedSession(metadata, archived_at, pending)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # A broken marker must never make an archived session active again.
            raise SessionTrashError("无法读取回收站记录，请检查本机存储后重试。") from exc

    def entries(self) -> list[TrashedSession]:
        self._path("check")
        try:
            paths = list(self.root.glob("*.json"))
        except OSError as exc:
            raise SessionTrashError("无法读取回收站，请检查本机存储后重试。") from exc
        entries = [self.get(path.stem) for path in paths]
        return sorted(
            (entry for entry in entries if entry is not None),
            key=lambda entry: entry.archived_at,
            reverse=True,
        )

    def _write(self, entry: TrashedSession) -> None:
        path = self._path(entry.metadata["session_id"])
        try:
            atomic_write_json(path, {
                "metadata": entry.metadata,
                "archived_at": entry.archived_at,
                "deletion_pending": entry.deletion_pending,
            })
        except OSError as exc:
            raise SessionTrashError("无法保存回收站状态，请检查本机存储后重试。") from exc

    def archive(self, metadata: dict[str, Any]) -> TrashedSession:
        existing = self.get(metadata["session_id"])
        if existing is not None:
            return existing
        entry = TrashedSession(metadata, time.time())
        self._write(entry)
        return entry

    def begin_delete(self, session_id: str) -> None:
        entry = self.get(session_id)
        if entry is None:
            raise SessionTrashError("请先将对话移入回收站，再确认永久删除。")
        self._write(TrashedSession(entry.metadata, entry.archived_at, True))

    def forget(self, session_id: str) -> None:
        try:
            self._path(session_id).unlink(missing_ok=True)
        except OSError as exc:
            raise SessionTrashError("无法更新回收站状态，请重试。") from exc
