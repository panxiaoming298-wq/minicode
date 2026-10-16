"""Native names/type observations, distinct from reading file contents."""
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from minicode.path_safety import is_unsafe_stored_path


def membership_digest(entries) -> str:
    ordered = sorted(entries, key=lambda item: (item[0].lower(), item[0]))
    return hashlib.sha256(json.dumps(ordered, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def directory_observation(path: Path, *, root: Path) -> tuple[list[tuple[str, str]], str]:
    """Capture exactly the names/types used by list_files, including empty dirs.

    Full membership is retained even when display is truncated. Aliased or
    unavailable paths cannot supply proof that content changes are independent.
    """
    if is_unsafe_stored_path(path, boundary=root) or not path.is_dir():
        raise OSError("directory membership is unavailable or aliased")
    entries = []
    for item in sorted(path.iterdir(), key=lambda value: (value.name.lower(), value.name)):
        if is_unsafe_stored_path(item, boundary=root):
            raise OSError("directory membership contains an aliased path")
        entries.append((item.name, "dir" if item.is_dir() else "file"))
    return entries, membership_digest(entries)


def _patch_creates_directory(root: Path, name: str, writes) -> bool:
    """Prove absence along a safe directory chain, backed by a patch creation.

    A missing directory is not an empty directory observation. It may only be
    used for the projected view when this patch creates a descendant file.
    Unreadable paths, file parents and aliases never provide that proof.
    """
    target = root / name
    prefix = name.rstrip("/") + "/"
    if (name == "." or not writes
            or not any(path.startswith(prefix) and content is not None for path, content in writes.items())
            or is_unsafe_stored_path(target, boundary=root)):
        return False
    cursor = root
    if not stat.S_ISDIR(cursor.stat().st_mode):
        return False
    for part in target.relative_to(root).parts:
        cursor = cursor / part
        try:
            mode = cursor.stat().st_mode
        except FileNotFoundError:
            return True
        if not stat.S_ISDIR(mode):
            return False
    return False


def current_directory_hashes(root: Path | None, names, *, writes=None) -> dict[str, str]:
    """Missing proof remains missing; callers must refuse it, never guess."""
    hashes = {}
    if root is None:
        return hashes
    for name in names:
        try:
            if _patch_creates_directory(root, name, writes):
                entries, digest = [], membership_digest([])
            else:
                entries, digest = directory_observation(root / name, root=root)
            if writes:
                # A Builder may enumerate after its own creation/deletion. Only
                # its immutable patch paths may be overlaid; no outside changes
                # disappear from this native comparison.
                projected = dict(entries)
                prefix = "" if name == "." else name + "/"
                for path, content in writes.items():
                    if not path.startswith(prefix):
                        continue
                    relative = path[len(prefix):]
                    child, separator, _ = relative.partition("/")
                    if separator:
                        if content is not None:
                            if projected.get(child) not in {None, "dir"}:
                                raise OSError("patch parent conflicts with directory membership")
                            projected[child] = "dir"
                    elif content is None:
                        projected.pop(child, None)
                    else:
                        if projected.get(child) == "dir":
                            raise OSError("patch file conflicts with directory membership")
                        projected[child] = "file"
                digest = membership_digest(projected.items())
            hashes[name] = digest
        except (OSError, ValueError, RuntimeError):
            continue
    return hashes
