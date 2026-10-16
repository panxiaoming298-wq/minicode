"""Byte-preserving Git working-directory snapshots and content-addressed blobs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath

from minicode.path_safety import is_link_or_reparse, is_unsafe_stored_path


class WorktreeError(RuntimeError):
    def __init__(self, message: str, *, code: str = "worktree_error", files: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.files = files or []


def git(root: Path, *args: str, timeout: int = 60) -> bytes:
    try:
        result = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(root), *args],
                                # Snapshot/worktree operations are non-interactive;
                                # do not inherit the Web Worker's live inbox pipe.
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"Git 操作未完成：{exc}") from exc
    if result.returncode:
        raise WorktreeError(result.stderr.decode("utf-8", "replace").strip() or "Git 操作失败。")
    return result.stdout


def repository(root: Path) -> tuple[Path, Path, str]:
    repo = Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()
    head = git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
    return repo, common, head


def member(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or any(p in {"..", ".git"} for p in relative.parts)
            or "\\" in name or ":" in name):
        raise WorktreeError("文件路径不在工作区内。", files=[name])
    path = root.joinpath(*relative.parts)
    if is_unsafe_stored_path(path, boundary=root, reject_hardlinks=True):
        raise WorktreeError("工作树不处理链接或越界文件。", files=[name])
    return path


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def put_blob(blobs: Path, content: bytes) -> str:
    import tempfile
    key = hashlib.sha256(content).hexdigest()
    path = blobs / key
    blobs.mkdir(parents=True, exist_ok=True)
    if is_unsafe_stored_path(path, boundary=blobs, reject_hardlinks=True):
        raise WorktreeError("快照存储路径不安全。")
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != key:
            raise WorktreeError("快照数据损坏。")
        return key
    descriptor, temporary = tempfile.mkstemp(prefix=".blob-", dir=blobs)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return key


def blob(blobs: Path, entry: dict) -> bytes:
    key = entry["hash"]
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise WorktreeError("无效的快照标识。")
    path = blobs / key
    if is_unsafe_stored_path(path, boundary=blobs, reject_hardlinks=True):
        raise WorktreeError("快照路径不安全。")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != key:
        raise WorktreeError("快照校验失败。")
    return content


_CACHE_PARTS = {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
                ".ruff_cache", "dist", "build", "coverage", ".mini-code", ".mini-code-memory", ".mini-code-memory-v2"}


def _runtime_file(name: str) -> bool:
    parts = PurePosixPath(name).parts
    module_test_data = any(parts[index:index + 2] == ("Data", "Test") for index in range(len(parts) - 1))
    return module_test_data or bool(set(parts).intersection(_CACHE_PARTS)) or name.lower().endswith(
        (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm"))


def snapshot(repo: Path, blobs: Path, *, extra: list[str] | None = None) -> dict[str, dict]:
    tracked = git(repo, "ls-files", "-z", "--cached").decode("utf-8").split("\0")
    untracked = git(repo, "ls-files", "-z", "--others", "--exclude-standard").decode("utf-8").split("\0")
    names = set(filter(None, tracked)) | {
        n for n in untracked if n and not _runtime_file(n)
    } | set(extra or [])
    result = {}
    for name in sorted(names):
        path = member(repo, name)
        if not path.exists():
            continue
        if not path.is_file() or is_link_or_reparse(path):
            raise WorktreeError("首版仅支持普通文件，无法快照子模块或特殊文件。", files=[name])
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise WorktreeError("创建快照期间文件发生变化，请重试。", code="stale", files=[name])
        result[name] = {"hash": put_blob(blobs, content), "size": len(content),
                        "mode": stat.S_IMODE(after.st_mode)}
    return result


def ignored_files(repo: Path, names: list[str]) -> list[str]:
    included = set()
    for name in names:
        path = member(repo, name)
        if not path.exists():
            raise WorktreeError("白名单文件不存在。", files=[name])
        candidates = [path]
        if path.is_dir():
            candidates = []
            for current, directories, files in os.walk(path, followlinks=False):
                for entry in [*directories, *files]:
                    if is_link_or_reparse(Path(current) / entry):
                        raise WorktreeError("白名单不支持链接。", files=[name])
                if _runtime_file(Path(current).relative_to(repo).as_posix()):
                    raise WorktreeError("白名单不能复制依赖、缓存或运行时数据库。", files=[name])
                candidates.extend(Path(current) / entry for entry in files)
        for candidate in candidates:
            relative = candidate.relative_to(repo).as_posix()
            if _runtime_file(relative):
                raise WorktreeError("白名单不能复制依赖、缓存或运行时数据库。", files=[relative])
            try:
                git(repo, "check-ignore", "--no-index", "--", relative)
            except WorktreeError:
                raise WorktreeError("白名单仅用于 Git 忽略的文件。", files=[relative]) from None
            included.add(relative)
    return sorted(included)


def write_entry(root: Path, name: str, entry: dict | None, blobs: Path) -> None:
    import tempfile
    path = member(root, name)
    if entry is None:
        if path.exists():
            if not path.is_file():
                raise WorktreeError("目标路径类型发生变化。", files=[name])
            path.unlink()
        return
    content = blob(blobs, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".minicode-apply-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, entry["mode"])
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
