"""Workspace isolation boundary used by the exclusive Ultra V1 writer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Protocol

from minicode.path_safety import is_link_or_reparse, is_unsafe_stored_path
from minicode.turn_telemetry import measured


_IGNORED_PARTS = frozenset(
    {
        ".git",
        ".mini-code",
        ".mini-code-memory",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        ".mini-code-memory-local",
        ".mini-code-memory-v2",
        ".temp",
    }
)
_OWNER_MARKER = ".minicode-runtime-workspace"

# How long a single file read inside a workspace snapshot tolerates a concurrent
# atomic replacement before the failure is treated as real. The window is the same
# order as the Runtime state store's replace tolerance: reading a workspace a live
# run is still writing is normal, and a sharing violation is not a content fact.
_FILE_READ_ATTEMPTS = 16
_FILE_READ_BACKOFF_SECONDS = 0.005
_FILE_READ_MAX_BACKOFF_SECONDS = 0.15
# Windows can spend most of a fresh copy's first scan waiting in file open.
# Bound concurrent reads; every observation still reads every current byte.
_SNAPSHOT_READ_WORKERS = 8 if os.name == "nt" else 1
# Windows metadata/open latency around 1 ms already dominates thousands of
# small files. Waiting for 15 ms/file kept realistic project scans serial.
_SNAPSHOT_PARALLEL_SECONDS_PER_FILE = 0.001
# Each assistant preparation is independently bounded, including in a batch.
ASYNC_PREPARATION_SECONDS = 120.0


class WorkspaceConflict(RuntimeError):
    pass


class WorkspacePreparationCanceled(WorkspaceConflict):
    """Preparation exited before publishing a runnable workspace."""


def _check_preparation(cancellation_event):
    if cancellation_event is not None and cancellation_event.is_set():
        raise WorkspacePreparationCanceled("workspace preparation canceled; no new task was registered")


def _npm_outputs(root: Path) -> bool:
    # The built-in frontend recipe produces these root-level paths. Source,
    # configuration, lockfiles, tests and all Data directories stay versioned.
    return (root / "package.json").is_file()


def _ignored(path: Path, root: Path, *, npm_outputs: bool | None = None) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    if any(part in _IGNORED_PARTS for part in relative.parts):
        return True
    if npm_outputs is None:
        npm_outputs = _npm_outputs(root)
    return bool(npm_outputs and relative.parts and (
        (relative.parts[0] in {"node_modules", "dist"} and (len(relative.parts) > 1 or path.is_dir()))
        or (len(relative.parts) == 1 and relative.name.endswith(".tsbuildinfo"))))


def _file_hash(path: Path, *, cancellation_event=None) -> str:
    """Hash one workspace file, tolerating a concurrent atomic replacement.

    A snapshot is a point-in-time observation of a workspace that a live run is
    still writing. Writing a file is ``temp + os.replace``, so between the moment
    this loop decides a path is a readable file and the moment it opens that path,
    the path can be swapped out from under it. On Windows that shows up as a
    sharing violation (``PermissionError``) while the replacement is mid-flight;
    POSIX typically shows nothing, which is why the failure looks platform
    specific.

    A filesystem race is not a property of the workspace, so it is retried for a
    bounded window and then raised with the attempt count -- the same discipline
    ``RuntimeStateStore`` uses for its atomic replace. A path that genuinely
    disappeared (the replacement removed it, or it was deleted) contributes
    nothing: the next snapshot simply will not contain it.
    """

    for attempt in range(_FILE_READ_ATTEMPTS):
        _check_preparation(cancellation_event)
        try:
            with path.open("rb") as handle:
                # Reuse a bounded readinto buffer instead of allocating a 1 MiB
                # bytes object for every read of every small dependency file.
                if cancellation_event is None:
                    return hashlib.file_digest(handle, "sha256").hexdigest()
                digest = hashlib.sha256()
                while chunk := handle.read(1024 * 1024):
                    _check_preparation(cancellation_event)
                    digest.update(chunk)
                return digest.hexdigest()
        except FileNotFoundError:
            return ""
        except PermissionError as error:
            if attempt == _FILE_READ_ATTEMPTS - 1:
                raise PermissionError(
                    f"{error} (reading {path} did not succeed after "
                    f"{_FILE_READ_ATTEMPTS} attempts; another handle kept it open)"
                ) from error
            time.sleep(min(_FILE_READ_MAX_BACKOFF_SECONDS,
                           _FILE_READ_BACKOFF_SECONDS * (2 ** attempt)))
    raise AssertionError("unreachable")


def snapshot_revision(snapshot: dict[str, str]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_metadata(path: Path):
    """Inspect a leaf without following it; ancestors are checked per directory."""
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise WorkspaceConflict(f"cannot inspect workspace file: {path}") from error
    if (not stat.S_ISREG(value.st_mode) or value.st_nlink > 1
            or getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        return None
    return value


def _snapshot_file(root: Path, path: Path, reject_aliases: bool, cancellation_event=None):
    _check_preparation(cancellation_event)
    before = _file_metadata(path)
    if before is None:
        if reject_aliases and (path.exists() or is_link_or_reparse(path)):
            raise WorkspaceConflict(f"workspace contains link aliases: {path.relative_to(root)}")
        return None
    digest = (_file_hash(path) if cancellation_event is None
              else _file_hash(path, cancellation_event=cancellation_event))
    after = _file_metadata(path)
    if after is None and reject_aliases and (path.exists() or is_link_or_reparse(path)):
        raise WorkspaceConflict(f"workspace contains link aliases: {path.relative_to(root)}")
    # Missing/replaced files must not be represented by an old or empty digest.
    if (digest and after is not None
            and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        return str(path.relative_to(root)).replace("\\", "/"), digest
    return None


@measured("workspace_snapshot")
def workspace_snapshot(root: Path, *, reject_aliases: bool = False,
                       cancellation_event=None, progress=None) -> dict[str, str]:
    """Read current bytes; optionally validate isolation in the same traversal.

    No metadata/digest cache is shared across observations. Owned candidates
    reject aliases rather than silently omitting them from the revision.
    """
    root = Path(root).resolve()
    _check_preparation(cancellation_event)
    snapshot: dict[str, str] = {}
    if not root.is_dir():
        return snapshot
    npm_outputs = _npm_outputs(root)
    # The executor is local to this observation and drains even after an error.
    # Pipeline bounded batches across directory boundaries. Waiting for every
    # small dependency directory separately left most readers idle on Windows.
    # Each directory is still checked before scheduling and after all reads.
    with ExitStack() as resources:
        readers = None
        parallel_reads = False
        pending = deque()
        pending_files = 0
        completed_files = 0
        directories = {}

        def observe(count):
            nonlocal completed_files
            completed_files += count
            _check_preparation(cancellation_event)
            if progress is not None:
                progress(completed_files)

        def drain_batch():
            nonlocal pending_files
            records, futures = pending.popleft()
            pending_files -= len(futures)
            for future in futures:
                item = future.result()
                if item is not None:
                    records[item[0]] = item[1]
            observe(len(futures))

        for directory, names, files in os.walk(root, topdown=True, followlinks=False):
            _check_preparation(cancellation_event)
            current = Path(directory)
            if is_unsafe_stored_path(current, boundary=root):
                if reject_aliases:
                    raise WorkspaceConflict(f"workspace contains link aliases: {current.relative_to(root)}")
                names[:] = []
                continue
            kept = []
            for name in names:
                path = current / name
                if _ignored(path, root, npm_outputs=npm_outputs):
                    continue
                if is_link_or_reparse(path):
                    if reject_aliases:
                        raise WorkspaceConflict(f"workspace contains link aliases: {path.relative_to(root)}")
                else:
                    kept.append(name)
            names[:] = kept
            paths = (current / name for name in files if not _ignored(current / name, root, npm_outputs=npm_outputs))
            directory_snapshot = {}
            directories[current] = directory_snapshot
            while batch := list(islice(paths, 32)):
                _check_preparation(cancellation_event)
                started = time.perf_counter()
                if parallel_reads:
                    if readers is None:
                        readers = resources.enter_context(ThreadPoolExecutor(
                            max_workers=_SNAPSHOT_READ_WORKERS, thread_name_prefix="workspace-read"))
                    pending.append((directory_snapshot, [readers.submit(
                        _snapshot_file, root, path, reject_aliases, cancellation_event) for path in batch]))
                    pending_files += len(batch)
                    while pending_files >= 128:
                        drain_batch()
                else:
                    results = (_snapshot_file(root, path, reject_aliases, cancellation_event) for path in batch)
                    directory_snapshot.update(item for item in results if item is not None)
                    observe(len(batch))
                # Warm small-file reads are faster serially. Switch only after
                # this observation encounters slow I/O, never from saved hashes
                # or mtime. The decision expires with this snapshot.
                if (_SNAPSHOT_READ_WORKERS > 1 and not parallel_reads and len(batch) >= 4
                        and time.perf_counter() - started > len(batch) * _SNAPSHOT_PARALLEL_SECONDS_PER_FILE):
                    parallel_reads = True
        while pending:
            drain_batch()
        for current, directory_snapshot in directories.items():
            if is_unsafe_stored_path(current, boundary=root):
                if reject_aliases:
                    raise WorkspaceConflict(f"workspace contains link aliases: {current.relative_to(root)}")
            else:
                snapshot.update(directory_snapshot)
    _check_preparation(cancellation_event)
    return snapshot


def workspace_link_aliases(root: Path) -> tuple[str, ...]:
    """Return symlink, junction/reparse, and hard-link aliases in a workspace."""

    root = Path(root).resolve()
    aliases: list[str] = []
    if not root.is_dir():
        return ()
    npm_outputs = _npm_outputs(root)
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        if is_unsafe_stored_path(current, boundary=root):
            aliases.append(str(current.relative_to(root)).replace("\\", "/"))
            names[:] = []
            continue
        kept: list[str] = []
        for name in names:
            path = current / name
            if _ignored(path, root, npm_outputs=npm_outputs):
                continue
            if is_link_or_reparse(path):
                aliases.append(str(path.relative_to(root)).replace("\\", "/"))
            else:
                kept.append(name)
        names[:] = kept
        for name in files:
            path = current / name
            if _ignored(path, root, npm_outputs=npm_outputs):
                continue
            if _file_metadata(path) is None and path.exists():
                aliases.append(str(path.relative_to(root)).replace("\\", "/"))
            elif is_link_or_reparse(path):
                # Broken links do not satisfy exists(), but remain aliases.
                aliases.append(str(path.relative_to(root)).replace("\\", "/"))
        if is_unsafe_stored_path(current, boundary=root):
            aliases.append(str(current.relative_to(root)).replace("\\", "/"))
            names[:] = []
    return tuple(sorted(set(aliases)))


@dataclass(slots=True)
class WorkspaceHandle:
    source: Path
    workspace: Path
    base_hashes: dict[str, str]
    isolated: bool
    owner_token: str
    integrated: bool = False
    closed: bool = False

    def changed_resources(self, current: dict[str, str] | None = None) -> tuple[str, ...]:
        if current is None:
            current = workspace_snapshot(self.workspace)
        keys = set(self.base_hashes) | set(current)
        return tuple(sorted(key for key in keys if self.base_hashes.get(key) != current.get(key)))


class WorkspaceProvider(Protocol):
    def create(self, source: Path, *, owner_token: str) -> WorkspaceHandle: ...
    def integrate(self, handle: WorkspaceHandle) -> tuple[str, ...]: ...
    def cleanup(self, handle: WorkspaceHandle) -> None: ...


class DirectWorkspaceProvider:
    """Execute in the selected directory; no copy and therefore no patch apply."""

    def create(self, source: Path, *, owner_token: str) -> WorkspaceHandle:
        resolved = Path(source).resolve()
        return WorkspaceHandle(
            source=resolved,
            workspace=resolved,
            base_hashes=workspace_snapshot(resolved),
            isolated=False,
            owner_token=owner_token,
        )

    def integrate(self, handle: WorkspaceHandle) -> tuple[str, ...]:
        if handle.closed:
            raise WorkspaceConflict("workspace handle is closed")
        handle.integrated = True
        return handle.changed_resources()

    def cleanup(self, handle: WorkspaceHandle) -> None:
        handle.closed = True


class TemporaryWorkspaceProvider:
    """Copy-on-run fallback that integrates only unchanged-base resources.

    It works for Git and non-Git workspaces, includes pre-existing dirty files,
    and never overwrites a source path whose hash changed after isolation.
    """

    def __init__(self, temporary_root: Path | None = None) -> None:
        self.temporary_root = Path(temporary_root).resolve() if temporary_root else None

    def validate_candidate_root(self, source: Path) -> None:
        """Runtime copies must not become part of the candidate's tracked tree."""
        source = Path(source).resolve()
        root = self.temporary_root
        if root is not None and (root == source or (source in root.parents and not _ignored(root, source))):
            raise WorkspaceConflict("candidate-first temporary root must be outside the workspace or inside a Runtime-ignored directory such as .temp")

    def reopen(self, *, source: Path, workspace: Path, owner_token: str,
               base_hashes: dict[str, str], expected_revision: str) -> WorkspaceHandle:
        """Reopen an owned checkpoint without copying or integrating files."""
        handle = WorkspaceHandle(Path(source).resolve(), Path(workspace), base_hashes, True, owner_token)
        current = self.owned_snapshot(handle)
        if snapshot_revision(current) != expected_revision:
            raise WorkspaceConflict("checkpoint candidate was modified or contains link aliases")
        handle.workspace = handle.workspace.resolve()
        handle.base_hashes = dict(base_hashes)
        return handle

    def owned_snapshot(self, handle: WorkspaceHandle) -> dict[str, str]:
        """Validate ownership and observe current content once, without caching."""
        self.validate_owner(handle)
        return workspace_snapshot(handle.workspace, reject_aliases=True)

    def validate_owner(self, handle: WorkspaceHandle) -> None:
        """Check the live owned directory without claiming fresh content evidence."""
        if handle.closed or not handle.isolated:
            raise WorkspaceConflict("owned snapshot requires a live isolated workspace")
        workspace, owner_token, base_hashes = handle.workspace, handle.owner_token, handle.base_hashes
        boundary = self.temporary_root or Path(tempfile.gettempdir()).resolve()
        if (
            workspace.name != "workspace"
            or workspace.parent.parent.resolve() != boundary
            or not workspace.parent.name.startswith("minicode-runtime-v2-")
            or is_unsafe_stored_path(workspace, boundary=boundary)
            or not workspace.is_dir()
        ):
            raise WorkspaceConflict("checkpoint workspace is outside its owned temporary root")
        marker = workspace.parent / _OWNER_MARKER
        if is_unsafe_stored_path(marker, boundary=boundary, reject_hardlinks=True) or not marker.is_file():
            raise WorkspaceConflict("checkpoint ownership marker is unavailable")
        if not owner_token or marker.read_text(encoding="utf-8") != owner_token:
            raise WorkspaceConflict("checkpoint workspace owner does not match")
        if not isinstance(base_hashes, dict) or any(
            not isinstance(key, str) or not key or Path(key).is_absolute() or ".." in Path(key).parts
            or not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for key, value in base_hashes.items()
        ):
            raise WorkspaceConflict("checkpoint contains an invalid workspace baseline")
    @measured("workspace_copy")
    def create(self, source: Path, *, owner_token: str,
               cancellation_event=None, progress=None) -> WorkspaceHandle:
        source = Path(source).resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"workspace does not exist: {source}")
        _check_preparation(cancellation_event)
        # Hash the actual bytes copied in the same pass. This removes a full
        # source read before every copy without trusting mtime or sharing files.
        # create_verified still freshly checks both trees after preparation.
        base_hashes: dict[str, str] = {}
        if self.temporary_root is not None:
            self.temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix="minicode-runtime-v2-",
                dir=str(self.temporary_root) if self.temporary_root else None,
            )
        ).resolve()
        workspace = temporary / "workspace"
        handle = WorkspaceHandle(source=source, workspace=workspace,
                                 base_hashes=base_hashes, isolated=True, owner_token=owner_token)
        # Own partial copies too, so an I/O failure cannot leave an unmarked
        # directory which neither cleanup nor recovery can safely identify.
        (temporary / _OWNER_MARKER).write_text(owner_token, encoding="utf-8")
        nested_temporary_root = self.temporary_root is not None and source in self.temporary_root.parents
        npm_outputs = _npm_outputs(source)

        def ignore(directory: str, names: list[str]) -> set[str]:
            _check_preparation(cancellation_event)
            current = Path(directory)
            ignored: set[str] = set()
            for name in names:
                candidate = current / name
                # A verifier may copy a candidate already under temporary_root.
                # Exclude the root only when it is *inside* the source, never
                # when it is an ancestor of the source (which excluded every file).
                if nested_temporary_root:
                    if (
                        candidate == self.temporary_root
                        or self.temporary_root in candidate.parents
                    ):
                        ignored.add(name)
                        continue
                # source/current are absolute, and aliases were rejected by the
                # baseline scan. No filesystem resolution is needed per leaf.
                if candidate == temporary:
                    ignored.add(name)
                    continue
                try:
                    relative = candidate.relative_to(source)
                except ValueError:
                    continue
                if _ignored(candidate, source, npm_outputs=npm_outputs):
                    ignored.add(name)
            return ignored

        try:
            if progress is not None:
                progress("复制独立工作区", 0)
            # Bound actual file I/O, not just directory traversal. Every file is
            # still copied independently; no hard links or shared dependencies.
            with ThreadPoolExecutor(max_workers=4 if os.name == "nt" else 1,
                                    thread_name_prefix="workspace-copy") as copies:
                pending = deque()
                copied = 0

                def drain_copy():
                    nonlocal copied
                    relative, digest = pending.popleft().result()
                    base_hashes[relative] = digest
                    copied += 1
                    _check_preparation(cancellation_event)
                    if progress is not None:
                        progress("复制独立工作区", copied)

                def copy_file(src, dst):
                    _check_preparation(cancellation_event)
                    if _file_metadata(Path(src)) is None:
                        raise WorkspaceConflict(f"workspace contains link aliases or changed file types: {src}")
                    pending.append(copies.submit(copy_and_hash, Path(src), Path(dst)))
                    if len(pending) >= 16:
                        drain_copy()

                def copy_and_hash(src, dst):
                    before = _file_metadata(src)
                    if before is None:
                        raise WorkspaceConflict(f"workspace contains link aliases or changed file types: {src}")
                    digest = hashlib.sha256()
                    with src.open("rb") as reader, dst.open("xb") as writer:
                        while chunk := reader.read(1024 * 1024):
                            _check_preparation(cancellation_event)
                            writer.write(chunk)
                            digest.update(chunk)
                    after = _file_metadata(src)
                    if after is None or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise WorkspaceConflict(f"source changed during copy: {src}")
                    shutil.copystat(src, dst, follow_symlinks=False)
                    return src.relative_to(source).as_posix(), digest.hexdigest()

                directories = []
                def walk_failure(error):
                    raise error

                for directory, names, files in os.walk(source, topdown=True, followlinks=False, onerror=walk_failure):
                    current = Path(directory)
                    if is_unsafe_stored_path(current, boundary=source):
                        raise WorkspaceConflict(f"source contains link aliases during copy: {current}")
                    excluded = ignore(directory, [*names, *files])
                    names[:] = [name for name in names if name not in excluded]
                    for name in names:
                        if is_link_or_reparse(current / name):
                            raise WorkspaceConflict(f"source contains link aliases during copy: {current / name}")
                    destination = workspace / current.relative_to(source)
                    destination.mkdir()
                    directories.append((current, destination))
                    for name in files:
                        if name not in excluded:
                            copy_file(current / name, destination / name)
                while pending:
                    drain_copy()
                # Apply directory permissions only after every file writer has
                # joined; read-only source directories must not race the copy.
                for current, destination in reversed(directories):
                    _check_preparation(cancellation_event)
                    shutil.copystat(current, destination, follow_symlinks=False)
            _check_preparation(cancellation_event)
        except BaseException as error:
            try:
                self.cleanup(handle)
            except Exception as cleanup_error:
                error.add_note(f"Partial workspace retained at {workspace}: {cleanup_error}")
            raise
        return handle

    def create_verified(self, source: Path, *, owner_token: str,
                        cancellation_event=None, progress=None) -> WorkspaceHandle:
        """Copy and bind a fixed input with three fresh byte observations.

        create() binds the baseline to the exact copied bytes. Reuse that
        observation, not a second adjacent scan, and still check both ends after
        copying. No digest cache, dependency exclusion or alias relaxation.
        """
        handle = self.create(source, owner_token=owner_token,
            cancellation_event=cancellation_event, progress=progress)
        try:
            for stage, root in (("校验工作区副本", handle.workspace), ("复核源文件", handle.source)):
                if progress is not None:
                    progress(stage, 0)
                current = workspace_snapshot(root, reject_aliases=True,
                    cancellation_event=cancellation_event,
                    progress=(lambda count: progress(stage, count)) if progress else None)
                if current != handle.base_hashes:
                    raise WorkspaceConflict("source or copy changed during workspace capture")
            return handle
        except BaseException as error:
            try:
                self.cleanup(handle)
            except Exception as cleanup_error:
                error.add_note(f"Partial workspace retained at {handle.workspace}: {cleanup_error}")
            raise

    def integrate(self, handle: WorkspaceHandle) -> tuple[str, ...]:
        if handle.closed:
            raise WorkspaceConflict("workspace handle is closed")
        if not handle.isolated:
            raise WorkspaceConflict("temporary provider requires an isolated handle")
        current_source = workspace_snapshot(handle.source)
        current_isolated = workspace_snapshot(handle.workspace, reject_aliases=True)
        changed = tuple(
            sorted(
                key
                for key in set(handle.base_hashes) | set(current_isolated)
                if handle.base_hashes.get(key) != current_isolated.get(key)
            )
        )
        conflicts = [
            key
            for key in changed
            if current_source.get(key) != handle.base_hashes.get(key)
        ]
        if conflicts:
            raise WorkspaceConflict(
                "base revision changed for: " + ", ".join(conflicts[:20])
            )

        for key in changed:
            source_path = handle.source / Path(key)
            isolated_path = handle.workspace / Path(key)
            if key not in current_source and source_path.exists():
                raise WorkspaceConflict(f"source path changed type for: {key}")
            if key not in current_isolated and (
                isolated_path.exists() or is_link_or_reparse(isolated_path)
            ):
                raise WorkspaceConflict(
                    f"refusing to treat a non-regular isolated path as deletion: {key}"
                )
            if key in current_isolated and (
                not isolated_path.is_file()
                or is_unsafe_stored_path(
                    isolated_path,
                    boundary=handle.workspace,
                    reject_hardlinks=True,
                )
            ):
                raise WorkspaceConflict(f"refusing to integrate non-regular file: {key}")

        backup_root = handle.workspace.parent / "integration-backup"
        staged: dict[str, Path] = {}
        existed: dict[str, bool] = {}
        created_directories: list[Path] = []
        applied: list[str] = []
        try:
            for key in changed:
                source_path = handle.source / Path(key)
                isolated_path = handle.workspace / Path(key)
                missing_parents: list[Path] = []
                parent = source_path.parent
                while parent != handle.source and not parent.exists():
                    missing_parents.append(parent)
                    parent = parent.parent
                for directory in reversed(missing_parents):
                    directory.mkdir()
                    created_directories.append(directory)
                existed[key] = source_path.is_file()
                if existed[key]:
                    backup_path = backup_root / Path(key)
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, backup_path)
                if key in current_isolated:
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{source_path.name}.minicode-",
                        suffix=".tmp",
                        dir=str(source_path.parent),
                    )
                    os.close(descriptor)
                    temporary = Path(temporary_name)
                    shutil.copy2(isolated_path, temporary)
                    staged[key] = temporary

            for key in changed:
                source_path = handle.source / Path(key)
                if key not in current_isolated:
                    if source_path.exists():
                        source_path.unlink()
                else:
                    os.replace(staged[key], source_path)
                applied.append(key)
        except OSError as error:
            rollback_errors: list[str] = []
            for key in reversed(applied):
                source_path = handle.source / Path(key)
                try:
                    if existed.get(key):
                        backup_path = backup_root / Path(key)
                        shutil.copy2(backup_path, source_path)
                    elif source_path.exists() and not is_link_or_reparse(source_path):
                        source_path.unlink()
                except OSError as rollback_error:
                    rollback_errors.append(f"{key}: {rollback_error}")
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            details = f"workspace integration failed and was rolled back: {error}"
            if rollback_errors:
                details += "; rollback errors: " + ", ".join(rollback_errors)
            raise WorkspaceConflict(details) from error
        finally:
            for temporary in staged.values():
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        handle.integrated = True
        return changed

    def cleanup(self, handle: WorkspaceHandle) -> None:
        if handle.closed:
            return
        if not handle.isolated:
            handle.closed = True
            return
        container = handle.workspace.parent
        boundary = self.temporary_root or Path(tempfile.gettempdir()).resolve()
        if container.parent.resolve() != boundary or is_unsafe_stored_path(container, boundary=boundary):
            raise WorkspaceConflict("temporary workspace escaped configured root or contains link aliases")
        container = container.resolve()
        marker = container / _OWNER_MARKER
        expected_prefix = "minicode-runtime-v2-"
        if not container.name.startswith(expected_prefix):
            raise WorkspaceConflict("refusing to remove an unowned workspace path")
        if (is_unsafe_stored_path(marker, boundary=boundary, reject_hardlinks=True)
                or not marker.is_file() or marker.read_text(encoding="utf-8") != handle.owner_token):
            raise WorkspaceConflict("refusing to remove workspace without matching owner marker")
        # Ownership and the resolved boundary are checked above. Use Windows'
        # extended namespace so generated caches deeper than MAX_PATH can be
        # removed without discarding an otherwise completed verification.
        if os.name == "nt":
            value = str(container)
            if not value.startswith("\\\\?\\"):
                container = Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
        shutil.rmtree(container)
        handle.closed = True
