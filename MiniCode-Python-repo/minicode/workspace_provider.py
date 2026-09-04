"""Workspace isolation boundary used by the exclusive Ultra V1 writer."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from minicode.path_safety import is_link_or_reparse, is_unsafe_stored_path


_IGNORED_PARTS = frozenset(
    {
        ".git",
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


class WorkspaceConflict(RuntimeError):
    pass


def _ignored(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part in _IGNORED_PARTS for part in relative.parts)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_snapshot(root: Path) -> dict[str, str]:
    root = Path(root).resolve()
    snapshot: dict[str, str] = {}
    if not root.is_dir():
        return snapshot
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        names[:] = [
            name
            for name in names
            if not _ignored(current / name, root)
            and not is_link_or_reparse(current / name)
        ]
        for name in files:
            path = current / name
            if (
                _ignored(path, root)
                or is_unsafe_stored_path(path, boundary=root, reject_hardlinks=True)
                or not path.is_file()
            ):
                continue
            key = str(path.relative_to(root)).replace("\\", "/")
            snapshot[key] = _file_hash(path)
    return snapshot


def workspace_link_aliases(root: Path) -> tuple[str, ...]:
    """Return symlink, junction/reparse, and hard-link aliases in a workspace."""

    root = Path(root).resolve()
    aliases: list[str] = []
    if not root.is_dir():
        return ()
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        kept: list[str] = []
        for name in names:
            path = current / name
            if _ignored(path, root):
                continue
            if is_link_or_reparse(path):
                aliases.append(str(path.relative_to(root)).replace("\\", "/"))
            else:
                kept.append(name)
        names[:] = kept
        for name in files:
            path = current / name
            if _ignored(path, root):
                continue
            if is_unsafe_stored_path(path, boundary=root, reject_hardlinks=True):
                aliases.append(str(path.relative_to(root)).replace("\\", "/"))
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

    def changed_resources(self) -> tuple[str, ...]:
        current = workspace_snapshot(self.workspace)
        keys = set(self.base_hashes) | set(current)
        return tuple(sorted(key for key in keys if self.base_hashes.get(key) != current.get(key)))


class WorkspaceProvider(Protocol):
    def create(self, source: Path, *, owner_token: str) -> WorkspaceHandle: ...
    def integrate(self, handle: WorkspaceHandle) -> tuple[str, ...]: ...
    def cleanup(self, handle: WorkspaceHandle) -> None: ...


class DirectWorkspaceProvider:
    """Standard fast-path provider; no copy and therefore no patch apply."""

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

    def create(self, source: Path, *, owner_token: str) -> WorkspaceHandle:
        source = Path(source).resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"workspace does not exist: {source}")
        source_aliases = workspace_link_aliases(source)
        if source_aliases:
            raise WorkspaceConflict(
                "temporary workspace does not follow link aliases: "
                + ", ".join(source_aliases[:20])
            )
        # Capture the user's baseline before creating any Runtime-owned path,
        # including when a configured temporary root sits under the workspace.
        base_hashes = workspace_snapshot(source)
        if self.temporary_root is not None:
            self.temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix="minicode-runtime-v2-",
                dir=str(self.temporary_root) if self.temporary_root else None,
            )
        ).resolve()
        workspace = temporary / "workspace"

        def ignore(directory: str, names: list[str]) -> set[str]:
            current = Path(directory)
            ignored: set[str] = set()
            for name in names:
                candidate = current / name
                if self.temporary_root is not None:
                    resolved_candidate = candidate.resolve()
                    if (
                        resolved_candidate == self.temporary_root
                        or self.temporary_root in resolved_candidate.parents
                        or resolved_candidate in self.temporary_root.parents
                    ):
                        ignored.add(name)
                        continue
                try:
                    relative = candidate.relative_to(source)
                except ValueError:
                    continue
                if any(part in _IGNORED_PARTS for part in relative.parts):
                    ignored.add(name)
            return ignored

        shutil.copytree(source, workspace, ignore=ignore, symlinks=True)
        (temporary / _OWNER_MARKER).write_text(owner_token, encoding="utf-8")
        return WorkspaceHandle(
            source=source,
            workspace=workspace,
            base_hashes=base_hashes,
            isolated=True,
            owner_token=owner_token,
        )

    def integrate(self, handle: WorkspaceHandle) -> tuple[str, ...]:
        if handle.closed:
            raise WorkspaceConflict("workspace handle is closed")
        if not handle.isolated:
            raise WorkspaceConflict("temporary provider requires an isolated handle")
        current_source = workspace_snapshot(handle.source)
        current_isolated = workspace_snapshot(handle.workspace)
        isolated_aliases = workspace_link_aliases(handle.workspace)
        if isolated_aliases:
            raise WorkspaceConflict(
                "refusing to integrate link aliases: "
                + ", ".join(isolated_aliases[:20])
            )
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
        container = handle.workspace.parent.resolve()
        marker = container / _OWNER_MARKER
        expected_prefix = "minicode-runtime-v2-"
        if not container.name.startswith(expected_prefix):
            raise WorkspaceConflict("refusing to remove an unowned workspace path")
        if not marker.is_file() or marker.read_text(encoding="utf-8") != handle.owner_token:
            raise WorkspaceConflict("refusing to remove workspace without matching owner marker")
        if self.temporary_root is not None:
            try:
                container.relative_to(self.temporary_root)
            except ValueError as error:
                raise WorkspaceConflict("temporary workspace escaped configured root") from error
        shutil.rmtree(container)
        handle.closed = True
