"""Durable session worktrees with reviewed, recoverable file integration."""

from __future__ import annotations

import difflib
import json
import os
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

from minicode.git_snapshot import (WorktreeError, blob, fingerprint, git, ignored_files,
                                   member, repository, snapshot, write_entry)
from minicode.path_safety import is_unsafe_stored_path
from minicode.session_contract import atomic_write_json
from minicode.store_lock import store_lock
from minicode.workspace_lock import WorkspaceWriteLease, normalize_workspace_path


class GitWorktreeService:
    def __init__(self, root: Path | None = None):
        if root is None:
            from minicode import session
            root = session.SESSIONS_DIR.parent / "worktrees"
        self.root = Path(root).resolve()
        self.blobs = self.root / "blobs"

    def _directory(self, worktree_id: str) -> Path:
        if len(worktree_id) != 32 or any(c not in "0123456789abcdef" for c in worktree_id):
            raise WorktreeError("无效的工作树标识。")
        path = self.root / worktree_id
        if is_unsafe_stored_path(path, boundary=self.root):
            raise WorktreeError("工作树存储路径不安全。")
        return path

    def _read(self, path: Path) -> dict:
        if is_unsafe_stored_path(path, boundary=self.root, reject_hardlinks=True):
            raise WorktreeError("工作树记录路径不安全。")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WorktreeError("工作树记录不存在或已损坏。", code="missing") from exc

    def _save(self, record: dict):
        atomic_write_json(self._directory(record["worktreeId"]) / "record.json", record)

    def get(self, worktree_id: str, *, require_ready: bool = False) -> dict:
        directory = self._directory(worktree_id)
        record = self._read(directory / "record.json")
        if record.get("worktreeId") != worktree_id or Path(record.get("checkout", "")) != directory / "checkout":
            raise WorktreeError("工作树归属校验失败。")
        checkout = directory / "checkout"
        if is_unsafe_stored_path(checkout, boundary=directory):
            raise WorktreeError("工作树目录被链接替换。")
        if Path(record.get("workspace", "")) != checkout / record["offset"]:
            raise WorktreeError("工作树执行目录归属校验失败。")
        if record["status"] == "ready" and not Path(record["workspace"]).is_dir():
            record = {**record, "status": "missing"}
        if require_ready:
            if record["status"] != "ready":
                raise WorktreeError("工作树尚未就绪或目录已缺失。", code=record["status"])
            _, common, _ = repository(checkout)
            if str(common) != record["commonDir"]:
                raise WorktreeError("工作树已经不属于原仓库。")
        return record

    @staticmethod
    def public(record: dict) -> dict:
        return {key: record.get(key) for key in
                ("worktreeId", "status", "workspace", "projectPath", "createdAt", "includedFiles", "error")}

    @contextmanager
    def _leases(self, paths: list[Path], operation_id: str):
        with ExitStack() as stack:
            normalized = {normalize_workspace_path(p) for p in paths}
            for path in sorted(p for p in normalized if not any(str(a) in normalized for a in Path(p).parents)):
                stack.enter_context(WorkspaceWriteLease(path, run_id=operation_id))
            yield

    def _git_lock(self, common: Path):
        return store_lock(self.root / "locks" / f"git-{fingerprint(str(common))}.lock")

    def create(self, source: Path, project: Path, *, worktree_id: str | None = None,
               include_ignored: list[str] | None = None) -> dict:
        worktree_id = worktree_id or uuid.uuid4().hex
        directory = self._directory(worktree_id)
        source, project = source.resolve(), project.resolve()
        repo, common, head = repository(source)
        target_repo, target_common, _ = repository(project)
        if common != target_common:
            raise WorktreeError("源目录与目标项目不属于同一仓库。")
        if self.root == repo or repo in self.root.parents:
            raise WorktreeError("工作树存储目录必须位于源仓库之外。")
        offset = source.relative_to(repo).as_posix()
        if project.relative_to(target_repo).as_posix() != offset:
            raise WorktreeError("工作树项目子目录与原项目不一致。")
        checkout = directory / "checkout"
        with store_lock(directory / ".lock"), self._leases([repo, target_repo], worktree_id), self._git_lock(common):
            if (directory / "record.json").exists() or checkout.exists():
                raise WorktreeError("工作树标识已存在。")
            included = ignored_files(repo, include_ignored or [])
            base = snapshot(repo, self.blobs, extra=included)
            target_base = snapshot(target_repo, self.blobs, extra=list(base))
            record = {"worktreeId": worktree_id, "status": "preparing", "checkout": str(checkout),
                      "workspace": str(checkout / offset), "projectPath": str(project),
                      "targetRepo": str(target_repo), "sourceRepo": str(repo), "commonDir": str(common),
                      "baseHead": head, "offset": offset, "baseline": base, "targetBaseline": target_base,
                      "includedFiles": included, "createdAt": time.time(), "generation": 0}
            self._save(record)
            try:
                git(repo, "worktree", "add", "--detach", str(checkout), head)
                initial = snapshot(checkout, self.blobs)
                for name in sorted(set(initial) | set(base)):
                    if initial.get(name) != base.get(name):
                        write_entry(checkout, name, base.get(name), self.blobs)
                if (snapshot(repo, self.blobs, extra=included) != base
                        or repository(repo)[2] != head or snapshot(target_repo, self.blobs, extra=list(base)) != target_base):
                    raise WorktreeError("创建期间原目录发生变化，请重试。", code="stale")
                if not (checkout / offset).is_dir():
                    raise WorktreeError("快照中不存在所选项目目录。")
                record["status"] = "ready"
                self._save(record)
                return record
            except Exception as exc:
                record.update(status="failed", error=str(exc))
                self._save(record)
                if checkout.exists():
                    try:
                        if repository(checkout)[1] != common:
                            raise WorktreeError("初始化目录归属改变，已保留目录。")
                        git(repo, "worktree", "remove", "--force", str(checkout))
                    except WorktreeError:
                        record["error"] += "；初始化目录已保留，可稍后显式清理。"
                        self._save(record)
                raise

    @staticmethod
    def _in_scope(record: dict, name: str) -> bool:
        offset = record["offset"]
        return offset == "." or name.startswith(offset + "/")

    def _current(self, record: dict) -> tuple[dict, dict]:
        checkout = Path(record["checkout"])
        target = Path(record["targetRepo"])
        if str(repository(target)[1]) != record["commonDir"]:
            raise WorktreeError("目标项目的 Git 仓库身份已改变。")
        current = snapshot(checkout, self.blobs, extra=list(record["baseline"]))
        return current, snapshot(target, self.blobs, extra=list(set(current) | set(record["baseline"])))

    def _comparison(self, record: dict) -> dict:
        current, target = self._current(record)
        base = record["baseline"]
        names = sorted(name for name in set(base) | set(current) if base.get(name) != current.get(name))
        conflicts = [name for name in names if target.get(name) not in (base.get(name), current.get(name))]
        outside = [name for name in names if not self._in_scope(record, name)]
        files = []
        for name in names:
            before = blob(self.blobs, base[name]) if name in base else b""
            after = blob(self.blobs, current[name]) if name in current else b""
            try:
                before_text, after_text = before.decode("utf-8"), after.decode("utf-8")
                binary = b"\0" in before or b"\0" in after
            except UnicodeError:
                binary = True
                before_text = after_text = ""
            diff = "" if binary else "".join(difflib.unified_diff(
                before_text.splitlines(keepends=True), after_text.splitlines(keepends=True),
                fromfile=f"a/{name}", tofile=f"b/{name}"))
            files.append({"path": name, "status": "A" if name not in base else "D" if name not in current else "M",
                          "binary": binary, "beforeSize": len(before), "afterSize": len(after),
                          "diff": diff[:500_000], "truncated": len(diff) > 500_000,
                          "conflict": name in conflicts, "outsideProject": name in outside})
        return {"files": files, "conflicts": conflicts, "outsideProject": outside,
                "current": current, "target": target, "generation": record["generation"],
                "sourceHead": repository(Path(record["checkout"]))[2],
                "targetHead": repository(Path(record["targetRepo"]))[2]}

    def changes(self, worktree_id: str) -> dict:
        with store_lock(self._directory(worktree_id) / ".lock"):
            record = self.get(worktree_id, require_ready=True)
            value = self._comparison(record)
            return {"worktree": self.public(record), **{key: value[key] for key in ("files", "conflicts", "outsideProject")}}

    def preview(self, worktree_id: str) -> dict:
        with store_lock(self._directory(worktree_id) / ".lock"):
            record = self.get(worktree_id, require_ready=True)
            with self._leases([Path(record["checkout"]), Path(record["targetRepo"])], worktree_id):
                return self._preview_locked(record)

    def _preview_locked(self, record: dict) -> dict:
            worktree_id = record["worktreeId"]
            self._recover(record)
            record = self.get(worktree_id, require_ready=True)
            value = self._comparison(record)
            preview_id = uuid.uuid4().hex
            value.update(previewId=preview_id, createdAt=time.time(), worktreeId=worktree_id)
            atomic_write_json(self._directory(worktree_id) / "previews" / f"{preview_id}.json", value)
            return {key: value[key] for key in ("previewId", "worktreeId", "files", "conflicts", "outsideProject")}

    def apply(self, worktree_id: str, preview_id: str) -> dict:
        if len(preview_id) != 32 or any(c not in "0123456789abcdef" for c in preview_id):
            raise WorktreeError("无效的预览标识。")
        directory = self._directory(worktree_id)
        with store_lock(directory / ".lock"):
            record = self.get(worktree_id, require_ready=True)
            with self._leases([Path(record["checkout"]), Path(record["targetRepo"])], preview_id):
                self._recover(record)
                record = self.get(worktree_id, require_ready=True)
                preview = self._read(directory / "previews" / f"{preview_id}.json")
                if preview.get("applied"):
                    return {"applied": True, "files": preview["appliedFiles"]}
                current = self._comparison(record)
                if any(current[key] != preview[key] for key in ("current", "target", "generation", "sourceHead", "targetHead")):
                    raise WorktreeError("预览已过期，请重新查看差异。", code="stale")
                if current["outsideProject"] or current["conflicts"]:
                    raise WorktreeError("存在冲突或项目范围外的修改，未应用任何文件。", code="conflict",
                                        files=current["conflicts"] + current["outsideProject"])
                changed = [f["path"] for f in current["files"]]
                journal = {"state": "applying", "previewId": preview_id, "beforeRecord": record,
                           "before": {n: current["target"].get(n) for n in changed},
                           "after": {n: current["current"].get(n) for n in changed}}
                atomic_write_json(directory / "integration.json", journal)
                try:
                    for name in changed:
                        if current["target"].get(name) != current["current"].get(name):
                            write_entry(Path(record["targetRepo"]), name, current["current"].get(name), self.blobs)
                    record.update(baseline=current["current"], targetBaseline=current["target"],
                                  generation=record["generation"] + 1)
                    for name in changed:
                        if name in current["current"]:
                            record["targetBaseline"][name] = current["current"][name]
                        else:
                            record["targetBaseline"].pop(name, None)
                    self._save(record)
                    preview.update(applied=True, appliedFiles=changed)
                    atomic_write_json(directory / "previews" / f"{preview_id}.json", preview)
                    journal["state"] = "completed"
                    atomic_write_json(directory / "integration.json", journal)
                except Exception:
                    self._recover(record)
                    raise
                return {"applied": True, "files": changed}

    def _recover(self, record: dict):
        path = self._directory(record["worktreeId"]) / "integration.json"
        if not path.exists():
            return
        journal = self._read(path)
        if journal["state"] != "applying":
            return
        preview_path = path.parent / "previews" / f"{journal['previewId']}.json"
        preview = self._read(preview_path)
        if preview.get("applied"):
            journal["state"] = "completed"
            atomic_write_json(path, journal)
            return
        target = Path(record["targetRepo"])
        current = snapshot(target, self.blobs, extra=list(journal["before"]))
        conflicts = [name for name, before in journal["before"].items()
                     if current.get(name) not in (before, journal["after"].get(name))]
        if conflicts:
            raise WorktreeError("中断恢复发现新的外部修改，已保留备份，请处理后重试。", code="recovery_conflict", files=conflicts)
        for name, before in journal["before"].items():
            if current.get(name) != before:
                write_entry(target, name, before, self.blobs)
        self._save(journal["beforeRecord"])
        journal["state"] = "rolled_back"
        atomic_write_json(path, journal)

    def cleanup(self, worktree_id: str, *, discard: bool = False) -> dict:
        directory = self._directory(worktree_id)
        with store_lock(directory / ".lock"):
            record = self.get(worktree_id)
            if record["status"] == "removed":
                return record
            with self._leases([Path(record["checkout"]), Path(record["targetRepo"])], worktree_id), self._git_lock(Path(record["commonDir"])):
                # A lost checkout must not prevent restoring a partial apply to
                # the original directory before its ownership record is closed.
                self._recover(record)
                if record["status"] in {"ready", "missing"} and Path(record["checkout"]).is_dir():
                    if self._comparison(self.get(worktree_id))["files"] and not discard:
                        raise WorktreeError("工作树还有未应用改动，请选择保留目录或明确丢弃。", code="unapplied")
                checkout = Path(record["checkout"])
                if checkout.exists():
                    self.get(worktree_id, require_ready=record["status"] == "ready")
                    if str(repository(checkout)[1]) != record["commonDir"]:
                        raise WorktreeError("清理目录归属改变，已保留目录。")
                    git(Path(record["targetRepo"]), "worktree", "remove", "--force", str(checkout))
                else:
                    registrations = git(Path(record["targetRepo"]), "worktree", "list", "--porcelain", "-z").decode("utf-8").split("\0")
                    if any(entry.startswith("worktree ") and Path(entry[9:]).resolve() == checkout.resolve() for entry in registrations):
                        git(Path(record["targetRepo"]), "worktree", "remove", "--force", str(checkout))
                record["status"] = "removed"
                self._save(record)
                return record
