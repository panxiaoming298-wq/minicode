"""Durable, byte-preserving undo/restore for the latest completed Web turn."""
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from minicode.git_snapshot import WorktreeError, blob, member, put_blob, write_entry
from minicode.path_safety import is_unsafe_stored_path
from minicode.session_contract import _valid_session_id, atomic_write_json
from minicode.store_lock import store_lock
from minicode.workspace_files import is_internal_state_path


class TurnUndoStore:
    def __init__(self, session_id: str):
        from minicode import session
        if not _valid_session_id(session_id):
            raise WorktreeError("无效的会话标识。")
        self.root = session.SESSIONS_DIR / session_id / "turn-undo"
        self.path = self.root / "latest.json"
        self.blobs = self.root / "blobs"
        if is_unsafe_stored_path(self.path, boundary=session.SESSIONS_DIR, reject_hardlinks=True):
            raise WorktreeError("撤销快照路径不安全。")

    def _read(self):
        if not self.path.exists():
            return None
        if is_unsafe_stored_path(self.path, boundary=self.root, reject_hardlinks=True):
            raise WorktreeError("撤销记录路径不安全。")
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if (record["state"] not in {"applied", "undone", "unavailable"}
                    or not isinstance(record["files"], dict)
                    or type(record["revision"]) is not int or record["revision"] < 1
                    or not isinstance(record["turnId"], str) or not record["turnId"]
                    or not isinstance(record["workspace"], str) or not record["workspace"]
                    or not isinstance(record["reason"], str)):
                raise ValueError("invalid undo record")
            for name, pair in record["files"].items():
                if not isinstance(name, str) or not isinstance(pair, dict) or set(pair) != {"before", "after"}:
                    raise ValueError("invalid undo file")
                for entry in pair.values():
                    if entry is not None and (not isinstance(entry, dict)
                            or not isinstance(entry.get("hash"), str) or len(entry["hash"]) != 64
                            or any(c not in "0123456789abcdef" for c in entry["hash"])
                            or type(entry.get("size")) is not int or entry["size"] < 0
                            or type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o7777):
                        raise ValueError("invalid undo snapshot")
            # Never restore app state, including snapshots captured by older
            # versions. Project undo must not roll back another chat's memory.
            had_files = bool(record["files"])
            record["files"] = {name: pair for name, pair in record["files"].items()
                               if not is_internal_state_path(name)}
            if had_files and not record["files"] and not record.get("pending"):
                record.update(state="unavailable", reason="本轮没有需要撤销的项目文件修改。")
            return record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WorktreeError("撤销记录损坏，未修改项目文件。") from exc

    def capture(self, workspace: Path, turn_id: str, before: dict, after: dict) -> None:
        """Keep only this turn's changed files; never use Git HEAD or the index."""
        names = [name for name in sorted(set(before) | set(after))
                 if not is_internal_state_path(name)
                 and self._identity(before.get(name)) != self._identity(after.get(name))]
        reason = "" if names else "本轮没有需要撤销的文件修改。"
        if any(entry is not None and entry["data"] is None
               for name in names for entry in (before.get(name), after.get(name))):
            reason = "本轮含超出快照容量的文件，无法完整撤销。"
        with store_lock(self.root / ".lock"):
            previous = self._read()
            if previous and previous.get("pending"):
                raise WorktreeError("上次文件恢复被中断，快照已保留，需要先检查项目文件。")
            files = {}
            if not reason:
                for name in names:
                    member(workspace, name)
                    files[name] = {}
                    for side, entry in (("before", before.get(name)), ("after", after.get(name))):
                        files[name][side] = None if entry is None else {
                            "hash": put_blob(self.blobs, entry["data"]),
                            "size": entry["size"], "mode": entry["mode"],
                        }
            atomic_write_json(self.path, {"turnId": turn_id, "workspace": str(workspace.resolve()),
                                         "state": "unavailable" if reason else "applied",
                                         "revision": 1, "files": files, "reason": reason})
            # Superseded turns cannot be undone. Bound storage to the retained turn.
            retained = {entry["hash"] for pair in files.values() for entry in pair.values() if entry}
            if self.blobs.exists():
                for path in self.blobs.iterdir():
                    if len(path.name) == 64 and all(c in "0123456789abcdef" for c in path.name) and path.name not in retained:
                        try:
                            if not is_unsafe_stored_path(path, boundary=self.blobs, reject_hardlinks=True):
                                path.unlink()
                        except OSError:
                            pass

    @staticmethod
    def _identity(entry):
        return (entry["hash"], entry["mode"]) if entry else None

    @staticmethod
    def _matches(workspace, name, entry):
        path = member(workspace, name)
        try:
            before = path.stat()
        except FileNotFoundError:
            return entry is None
        if entry is None or not stat.S_ISREG(before.st_mode):
            return False
        if (before.st_size, stat.S_IMODE(before.st_mode)) != (entry["size"], entry["mode"]):
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        return ((before.st_size, before.st_mtime_ns, before.st_ino) ==
                (after.st_size, after.st_mtime_ns, after.st_ino)
                and stat.S_IMODE(after.st_mode) == entry["mode"] and digest.hexdigest() == entry["hash"])

    def status(self, workspace: Path, turn_id: str, *, eligible: bool = True, busy: bool = False) -> dict:
        with store_lock(self.root / ".lock"):
            record = self._read()
            reason = ""
            if not eligible:
                reason = "仅支持最近一轮已完成的修改。"
            elif busy:
                reason = "工作区有任务正在运行，请结束后再操作。"
            elif not record or record["turnId"] != turn_id or Path(record["workspace"]).resolve() != workspace.resolve():
                reason = "本轮未保存可撤销快照；新一轮完整执行结束后可用。"
            elif record.get("pending"):
                reason = "上次文件恢复被中断，快照已保留，需要检查项目文件。"
            elif record["state"] == "unavailable":
                reason = record["reason"]
            else:
                side = "after" if record["state"] == "applied" else "before"
                try:
                    conflicts = [name for name, pair in record["files"].items()
                                 if not self._matches(workspace, name, pair[side])]
                except OSError as exc:
                    raise WorktreeError("无法完整读取项目文件，未执行文件恢复。", code="unreadable") from exc
                if conflicts:
                    reason = "文件在本轮结束或撤销后又被修改，未覆盖后续编辑：" + "、".join(conflicts[:5])
            matching = record and record["turnId"] == turn_id
            state = record["state"] if matching else "unavailable"
            return {"turnId": turn_id, "state": state, "revision": record["revision"] if matching else 0,
                    "canUndo": not reason and state == "applied", "canRestore": not reason and state == "undone",
                    "fileCount": len(record["files"]) if matching else 0, "reason": reason}

    def apply(self, workspace: Path, turn_id: str, action: str, revision: int) -> dict:
        """Caller holds the session-store and workspace Writer leases."""
        if action not in {"undo", "restore"}:
            raise WorktreeError("无效的文件恢复操作。")
        with store_lock(self.root / ".lock"):
            current = self.status(workspace, turn_id)
            allowed = current["canUndo"] if action == "undo" else current["canRestore"]
            if not allowed or current["revision"] != revision:
                raise WorktreeError(current["reason"] or "撤销状态已改变，请刷新后重试。", code="stale")
            record = self._read()
            source, target = ("after", "before") if action == "undo" else ("before", "after")
            # Verify every backup before touching any file, including binary content.
            try:
                for pair in record["files"].values():
                    for entry in pair.values():
                        if entry:
                            blob(self.blobs, entry)
            except (OSError, ValueError, WorktreeError) as exc:
                raise WorktreeError("撤销快照缺失或损坏，未修改项目文件。", code="snapshot_unavailable") from exc
            atomic_write_json(self.path, {**record, "pending": {"action": action, "revision": revision}})
            written = []
            try:
                for name, pair in record["files"].items():
                    if not self._matches(workspace, name, pair[source]):
                        raise WorktreeError("文件在确认后又被修改，操作已停止。", code="stale", files=[name])
                    write_entry(workspace, name, pair[target], self.blobs)
                    written.append(name)
                if any(not self._matches(workspace, name, pair[target]) for name, pair in record["files"].items()):
                    raise WorktreeError("恢复期间文件又被修改，操作未完成。", code="stale")
                atomic_write_json(self.path, {**record, "state": "undone" if action == "undo" else "applied",
                                             "revision": revision + 1})
            except Exception as exc:
                rollback_errors = []
                for name in reversed(written):
                    try:
                        pair = record["files"][name]
                        if not self._matches(workspace, name, pair[target]):
                            raise WorktreeError("文件已被其他编辑修改。")
                        write_entry(workspace, name, pair[source], self.blobs)
                    except Exception:
                        rollback_errors.append(name)
                if not rollback_errors:
                    try:
                        atomic_write_json(self.path, record)
                    except OSError:
                        rollback_errors = written
                raise WorktreeError("文件恢复失败；" + ("部分文件需要检查，恢复快照已保留。" if rollback_errors
                                                        else "已恢复操作前状态，可以重试。"),
                                    code="restore_failed", files=rollback_errors) from exc
            return self.status(workspace, turn_id)
