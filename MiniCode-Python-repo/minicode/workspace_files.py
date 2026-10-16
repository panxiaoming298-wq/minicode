"""Distinguish application-owned state from user project file changes."""
from __future__ import annotations

import os
from pathlib import PurePosixPath

INTERNAL_STATE_DIRS = frozenset({".mini-code", ".mini-code-memory-local", ".mini-code-memory-v2"})


def is_internal_state_path(name: str) -> bool:
    # Persisted Windows records may use either separator. Match whole directory
    # components only; ordinary lock files and shared rule/memory files are user data.
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if os.name == "nt":
        parts = tuple(part.lower() for part in parts)
    return bool(INTERNAL_STATE_DIRS.intersection(parts))


def workspace_changes_integrated(*, candidate_first: bool, status: str, primary_committed: bool) -> bool:
    """Both direct writes and candidate integration need a native commit receipt."""
    return primary_committed is True and status == ("committed" if candidate_first else "idle")
