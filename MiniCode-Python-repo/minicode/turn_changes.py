"""Bounded working-file comparisons against the start of a Web turn.

The Git index/HEAD is never the baseline. Existing staged, unstaged and
untracked content belongs to the user. Candidate directories are deliberately
excluded: only changes that reach the selected workspace are presented here.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import stat
import threading
import time
from pathlib import Path

from minicode.git_snapshot import WorktreeError, git, member
from minicode.workspace_files import INTERNAL_STATE_DIRS, is_internal_state_path

_EXCLUDED = {".git", ".mini-code", ".temp", ".venv", "venv", "node_modules", "__pycache__",
             ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_MAX_FILES = 20_000
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
_MAX_SCAN_BYTES = 128 * 1024 * 1024
_MAX_DIFF_CHARS = 40_000
_MAX_TOTAL_DIFF_CHARS = 400_000


def project_file_changes(value: dict | None) -> dict | None:
    """Read-time projection also protects old persisted cards and diff objects."""
    if value is None:
        return None
    files = [row for row in value["files"] if not is_internal_state_path(row["path"])]
    if len(files) == len(value["files"]):
        return value
    return {**value, "files": files, "additions": sum(row["additions"] or 0 for row in files),
            "deletions": sum(row["deletions"] or 0 for row in files),
            "unmeasuredFiles": sum(row["additions"] is None for row in files),
            "notice": value.get("notice", "") if files or value.get("partial") else ""}


def change_summary(value: dict | None) -> dict | None:
    value = project_file_changes(value)
    if value is None:
        return None
    return {**value, "files": [{k: v for k, v in row.items() if k != "diff"}
                                for row in value["files"]]}


def project_change_event(event: dict) -> dict:
    if event.get("kind") == "file_changes" and isinstance(event.get("fileChanges"), dict):
        return {**event, "fileChanges": change_summary(event["fileChanges"])}
    return event


def _names(root: Path) -> set[str]:
    # --full-name would escape a workspace that is a repository subdirectory.
    try:
        git(root, "rev-parse", "--is-inside-work-tree", timeout=5)
    except WorktreeError:
        names = set()
        def unreadable(error):
            raise error
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
            dirs[:] = [name for name in dirs if name not in _EXCLUDED | INTERNAL_STATE_DIRS
                       and not is_internal_state_path(name)]
            for name in dirs:
                member(root, (Path(directory) / name).relative_to(root).as_posix())
            names.update((Path(directory) / name).relative_to(root).as_posix() for name in files)
            if len(names) > _MAX_FILES:
                raise ValueError("工作区文件过多，未建立完整的本轮基线。")
    else:
        names = {os.fsdecode(name) for name in git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", ".", timeout=10).split(b"\0") if name}
    names = {name for name in names if not set(Path(name).parts) & _EXCLUDED and not is_internal_state_path(name)}
    if len(names) > _MAX_FILES:
        raise ValueError("工作区文件过多，未建立完整的本轮基线。")
    return names


def _scan(root: Path, extra: set[str]) -> dict:
    result = {}
    remaining = _MAX_SNAPSHOT_BYTES
    scan_remaining = _MAX_SCAN_BYTES
    for name in sorted(_names(root) | extra):
        if is_internal_state_path(name):
            continue
        path = member(root, name)
        try:
            before = path.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("工作区含无法读取的特殊文件，改动统计不完整。")
        # Metadata-only entries are explicitly unmeasured, never +/- 0 lines.
        data = None
        digest = f"metadata:{before.st_size}:{before.st_mtime_ns}"
        if before.st_size <= _MAX_FILE_BYTES:
            if before.st_size > scan_remaining:
                raise ValueError("工作区超出本轮快照读取容量。")
            with path.open("rb") as stream:
                data = stream.read(_MAX_FILE_BYTES + 1)
            if len(data) != before.st_size:
                raise ValueError("文件正在变化，稍后重试改动统计。")
            scan_remaining -= len(data)
            digest = hashlib.sha256(data).hexdigest()
            # Fingerprints must not depend on which other files consumed the
            # text-retention budget, otherwise a new file fabricates edits.
            if len(data) <= remaining:
                remaining -= len(data)
            else:
                data = None
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("文件正在变化，稍后重试改动统计。")
        result[name] = {"hash": digest, "data": data, "size": before.st_size, "mode": stat.S_IMODE(before.st_mode)}
    return result


def _file_change(name: str, before: dict | None, after: dict | None, budget: int) -> dict:
    row = {"path": name, "status": "A" if before is None else "D" if after is None else "M",
           "beforeSize": before["size"] if before else 0, "afterSize": after["size"] if after else 0,
           "additions": None, "deletions": None, "binary": False, "truncated": False, "diff": "", "notice": ""}
    old = before["data"] if before else b""
    new = after["data"] if after else b""
    if old is None or new is None:
        row["notice"] = "文件较大或超出快照容量，未统计行数。"
        return row
    try:
        if b"\0" in old or b"\0" in new:
            raise UnicodeError()
        old_lines, new_lines = old.decode("utf-8").splitlines(keepends=True), new.decode("utf-8").splitlines(keepends=True)
    except UnicodeError:
        row.update(binary=True, notice="二进制或非 UTF-8 文件，仅显示大小变化。")
        return row
    if len(old_lines) + len(new_lines) > 20_000:
        row["notice"] = "文件行数过多，未生成文本差异。"
        return row
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    row["additions"] = sum(j2 - j1 for tag, _, _, j1, j2 in matcher.get_opcodes() if tag in {"insert", "replace"})
    row["deletions"] = sum(i2 - i1 for tag, i1, i2, _, _ in matcher.get_opcodes() if tag in {"delete", "replace"})
    lines = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{name}" if before else "/dev/null", tofile=f"b/{name}" if after else "/dev/null")
    # Preserve the EOF marker instead of joining two +/- records on one line.
    diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
    limit = min(_MAX_DIFF_CHARS, max(0, budget))
    row.update(diff=diff[:limit], truncated=len(diff) > limit)
    if old == new:
        row["notice"] = "空文件或文件权限发生变化，无文本行差异。"
    return row


class TurnChangeTracker:
    """Best-effort UI telemetry; a read failure never changes the task outcome."""

    def __init__(self, root: Path, turn_id: str, publish, *, session_id: str | None = None):
        self.root, self.turn_id, self.publish = root, turn_id, publish
        self.session_id = session_id
        self.lock = threading.Lock()
        self.timer: threading.Timer | None = None
        self.last_scan = 0.0
        self.value = {"turnId": turn_id, "revision": 0, "files": [], "additions": 0, "deletions": 0,
                      "unmeasuredFiles": 0, "partial": False, "complete": False, "notice": ""}
        self.baseline = None
        try:
            self.baseline = _scan(root, set())
        except (OSError, ValueError, WorktreeError):
            self.value.update(partial=True, notice="未能建立本轮文件基线，无法提供准确的改动统计。")
        self.publish(self.value)

    def refresh(self, *, complete: bool = False) -> dict:
        with self.lock:
            if self.value["complete"]:
                return self.value
            delay = 1.5 - (time.monotonic() - self.last_scan)
            if not complete and delay > 0:
                # Coalesce bursts, but do not lose the final edit before a long
                # model call. It must become visible without another tool event.
                if self.timer is None:
                    self.timer = threading.Timer(delay, self._refresh_later)
                    self.timer.daemon = True
                    self.timer.start()
                return self.value
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            self.last_scan = time.monotonic()
            value = {**self.value, "complete": complete}
            if self.baseline is not None:
                try:
                    current = _scan(self.root, set(self.baseline))
                    files, budget = [], _MAX_TOTAL_DIFF_CHARS
                    for name in sorted(set(current) | set(self.baseline)):
                        before, after = self.baseline.get(name), current.get(name)
                        if before and after and (before["hash"], before["mode"]) == (after["hash"], after["mode"]):
                            continue
                        row = _file_change(name, before, after, budget)
                        budget -= len(row["diff"])
                        files.append(row)
                    value.update(files=files, additions=sum(row["additions"] or 0 for row in files),
                                 deletions=sum(row["deletions"] or 0 for row in files),
                                 unmeasuredFiles=sum(row["additions"] is None for row in files), partial=False, notice="")
                    if complete and self.session_id:
                        from minicode.turn_undo import TurnUndoStore
                        try:
                            TurnUndoStore(self.session_id).capture(self.root, self.turn_id, self.baseline, current)
                        except (OSError, ValueError, WorktreeError):
                            value["notice"] = "改动统计已保存，但未能保存完整的撤销快照。"
                except (OSError, ValueError, WorktreeError):
                    value.update(partial=True, notice="未能完整读取工作区，以下是最近一次成功记录的改动。")
            if value != self.value:
                value["revision"] += 1
                self.value = value
                self.publish(value)
            return self.value

    def _refresh_later(self) -> None:
        with self.lock:
            self.timer = None
        self.refresh()
