"""Filesystem alias checks for persisted Runtime and session data.

``Path.is_symlink()`` is not sufficient on Windows: NTFS junctions are
reparse points but Python 3.11 reports them as ordinary directories.  Stored
state must also reject multiply-linked regular files because a hard link can
alias content outside the logical store without appearing as a symlink.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePath, PureWindowsPath


_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x0400,
)


def _resolved_comparison_path(path: PurePath) -> PurePath:
    """Compare resolved Windows paths in the same extended namespace.

    During concurrent creation, realpath can retain ``\\\\?\\`` for one
    result but not its parent. Adding that prefix to normal resolved paths
    avoids a false containment failure. Never strip it: trailing dots/spaces
    have distinct meanings in the extended namespace. Device paths and volume
    identifiers remain distinct; this is not a filesystem alias exemption.
    """
    if isinstance(path, PureWindowsPath) and path.is_absolute():
        drive = path.drive
        if len(drive) == 2 and drive[0].isalpha() and drive[1] == ":":
            return PureWindowsPath("\\\\?\\" + str(path))
        if drive.startswith("\\\\") and not drive.startswith(("\\\\?\\", "\\\\.\\")):
            return PureWindowsPath("\\\\?\\UNC\\" + str(path)[2:])
    return path


def is_link_or_reparse(path: Path) -> bool:
    """Return whether an existing path is a symlink or another reparse point.

    Errors other than a missing path fail closed.  This is intentionally based
    on ``lstat`` so the check never has to follow the alias first.
    """

    try:
        metadata = Path(path).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def is_unsafe_stored_path(
    path: Path,
    *,
    boundary: Path,
    reject_hardlinks: bool = False,
) -> bool:
    """Reject aliases between ``boundary`` and a persisted-data path.

    The boundary itself may be a user-configured redirected directory; only
    descendants are treated as untrusted aliases.  Both lexical and resolved
    containment are checked.  A nonexistent final path is allowed when all
    existing parents are safe, which lets callers use this before writes.
    """

    candidate = Path(os.path.abspath(path))
    root = Path(os.path.abspath(boundary))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True

    # Reject known hard-link aliases before resolving every ancestor. Large
    # package trees often contain thousands of these. Keep the final check too:
    # a link created while the parent chain is inspected must still be rejected.
    if reject_hardlinks:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            return True
        else:
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                return True

    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if is_link_or_reparse(cursor):
            return True

    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        resolved_root = root.resolve(strict=False)
    try:
        resolved_candidate = candidate.resolve(strict=candidate.exists())
        _resolved_comparison_path(resolved_candidate).relative_to(
            _resolved_comparison_path(resolved_root)
        )
    except (OSError, RuntimeError, ValueError):
        return True

    if reject_hardlinks:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
            return True
    return False
