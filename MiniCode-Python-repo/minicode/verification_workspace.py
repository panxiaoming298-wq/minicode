"""A batch-local verification copy, restored between settled commands."""
from __future__ import annotations

import os
import logging
from pathlib import Path
import shutil
import stat

from minicode.path_safety import is_link_or_reparse, is_unsafe_stored_path
from minicode.workspace_provider import (
    TemporaryWorkspaceProvider, WorkspaceConflict, _file_metadata, _ignored,
    workspace_snapshot,
)


def _filesystem_root(path):
    """Use the same owned absolute root with Windows long-path support."""
    path = Path(path)
    if os.name == "nt":
        value = str(path)
        if not value.startswith("\\\\?\\"):
            path = Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
    return path


class VerificationWorkspaceSession:
    """Reuse only within a serial batch; every command still observes fresh bytes.

    There is no process-global cache. Windows foreground jobs establish that
    descendants have exited before reuse. Platforms without that ownership
    receipt keep the independent-copy path.
    """

    def __init__(self, context):
        self.context = context
        self.provider = None
        self.handle = None
        self.reusable = False
        self.cleanup_warnings = []

    def is_set(self):
        self.context.check_active()
        return False

    def acquire(self, temporary_root, owner_token):
        self.context.check_active()
        root = Path(temporary_root).resolve() if temporary_root else None
        if self.handle is not None and (not self.reusable or self.provider.temporary_root != root):
            self.close()
        if self.handle is not None:
            self.reusable = False
            try:
                current = workspace_snapshot(self.context.workspace, reject_aliases=True, cancellation_event=self)
                if current != self.handle.base_hashes:
                    self.close()
                else:
                    self._restore()
            except BaseException:
                self.close()
                raise
        if self.handle is None:
            self.provider = TemporaryWorkspaceProvider(temporary_root=root)
            self.provider.validate_candidate_root(self.context.workspace)
            self.handle = self.provider.create(self.context.workspace, owner_token=owner_token,
                                               cancellation_event=self)
        return self.handle

    def settled(self):
        # A process group alone cannot account for detached children after the
        # leader exits. Do not reuse its copy without a kernel-owned job receipt.
        self.reusable = os.name == "nt"

    def _inventory(self, root, *, source):
        files, directories = {}, {}
        npm_outputs = (root / "package.json").is_file()

        def failed(error):
            raise error

        for directory, names, leaves in os.walk(root, topdown=True, followlinks=False, onerror=failed):
            self.context.check_active()
            current = Path(directory)
            if is_unsafe_stored_path(current, boundary=root) or is_link_or_reparse(current):
                raise WorkspaceConflict("verification workspace contains a directory alias")
            directories[current.relative_to(root)] = current.stat()
            if source:
                names[:] = [name for name in names if not _ignored(current / name, root, npm_outputs=npm_outputs)]
                leaves = [name for name in leaves if not _ignored(current / name, root, npm_outputs=npm_outputs)]
            for name in names:
                if is_link_or_reparse(current / name):
                    raise WorkspaceConflict("verification workspace contains a directory alias")
            for name in leaves:
                path = current / name
                metadata = _file_metadata(path)
                if metadata is None:
                    raise WorkspaceConflict("verification workspace contains a file alias or changed type")
                files[path.relative_to(root)] = metadata
        return files, directories

    def _restore(self):
        handle = self.handle
        self.provider.validate_owner(handle)
        source_root, target_root = _filesystem_root(handle.source), _filesystem_root(handle.workspace)
        source_files, source_dirs = self._inventory(source_root, source=True)
        target_files, target_dirs = self._inventory(target_root, source=False)
        hashes = workspace_snapshot(target_root, reject_aliases=True, cancellation_event=self)
        # Checks may change permissions, file/directory types, empty directories,
        # and ignored caches. Restore all of those, not just tracked file bytes.
        for relative, metadata in target_dirs.items():
            self.context.check_active()
            mode = stat.S_IMODE(metadata.st_mode)
            if mode & (stat.S_IWUSR | stat.S_IXUSR) != stat.S_IWUSR | stat.S_IXUSR:
                (target_root / relative).chmod(mode | stat.S_IWUSR | stat.S_IXUSR)
        for relative, metadata in target_files.items():
            self.context.check_active()
            target = target_root / relative
            if relative not in source_files or hashes.get(relative.as_posix()) != handle.base_hashes.get(relative.as_posix()):
                target.chmod(stat.S_IMODE(metadata.st_mode) | stat.S_IWUSR)
                target.unlink()
        for relative in sorted(target_dirs, key=lambda path: len(path.parts), reverse=True):
            self.context.check_active()
            if relative not in source_dirs:
                (target_root / relative).rmdir()
        for relative in source_dirs:
            self.context.check_active()
            (target_root / relative).mkdir(exist_ok=True)
        for relative in source_files:
            self.context.check_active()
            source, target = source_root / relative, target_root / relative
            preserved = (relative in target_files and
                         hashes.get(relative.as_posix()) == handle.base_hashes.get(relative.as_posix()))
            if not preserved:
                if _file_metadata(source) is None:
                    raise WorkspaceConflict("source changed type during verification restore")
                with source.open("rb") as reader, target.open("xb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        self.context.check_active()
                        writer.write(chunk)
            previous = target_files.get(relative)
            wanted = source_files[relative]
            if not preserved or (stat.S_IMODE(previous.st_mode), previous.st_mtime_ns) != (
                    stat.S_IMODE(wanted.st_mode), wanted.st_mtime_ns):
                shutil.copystat(source, target, follow_symlinks=False)
        for relative in reversed(source_dirs):
            self.context.check_active()
            shutil.copystat(source_root / relative, target_root / relative, follow_symlinks=False)

    def close(self):
        if self.handle is not None:
            handle = self.handle
            self.handle = None
            self.reusable = False
            try:
                self.provider.cleanup(handle)
            except (OSError, WorkspaceConflict) as error:
                warning = f"verification workspace cleanup deferred at {handle.workspace}: {error}"
                self.cleanup_warnings.append(warning)
                logging.getLogger(__name__).warning(warning)
