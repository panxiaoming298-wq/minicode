"""Select a process-wide user-state directory before component imports."""
from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Mapping

from minicode.path_safety import is_unsafe_stored_path


def _saved_location(home: Path) -> dict | None:
    path = home / ".mini-code-location.json"
    if not path.exists():
        return None
    if is_unsafe_stored_path(path, boundary=home, reject_hardlinks=True) or path.stat().st_size > 4096:
        raise ValueError("MiniCode saved storage location is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "minicode-state-location-v1":
        raise ValueError("MiniCode saved storage location is invalid")
    return value


def select_state_directory(environ: Mapping[str, str], home: Path) -> tuple[Path, bool]:
    """Return storage root and whether an explicit, separate profile was chosen.

    An invalid explicit value never falls back to real user state. This selects
    application paths, not HOME, credentials, tool access or an OS sandbox.
    """
    raw = environ.get("MINI_CODE_STATE_DIR")
    if raw is None:
        saved = _saved_location(home)
        if saved is None:
            return home / ".mini-code", False
        raw = saved.get("state_dir")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ValueError("MINI_CODE_STATE_DIR must be a nonblank absolute directory")
    path = Path(raw)
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError("MINI_CODE_STATE_DIR must be an absolute directory, not a filesystem root")
    path = Path(os.path.abspath(path))
    if path == Path(path.anchor):
        raise ValueError("MINI_CODE_STATE_DIR must not normalize to a filesystem root")
    if is_unsafe_stored_path(path, boundary=Path(path.anchor)):
        raise ValueError("MINI_CODE_STATE_DIR must not follow filesystem aliases")
    if path.exists() and not path.is_dir():
        raise ValueError("MINI_CODE_STATE_DIR must be a directory")
    return path, True


def configure_process_temporary_directory(environ, home: Path, state_root: Path) -> None:
    """Keep relocated installations' subprocesses and tempfile writes together."""
    raw = environ.get("MINI_CODE_TEMP_DIR")
    if raw is None:
        saved = _saved_location(home)
        if saved is None or saved.get("state_dir") != str(state_root):
            return
        raw = saved.get("temporary_dir")
    if raw is None:
        return
    path, _ = select_state_directory({"MINI_CODE_STATE_DIR": raw}, home)
    path.mkdir(parents=True, exist_ok=True)
    environ["TEMP"] = environ["TMP"] = str(path)
    environ["MINI_CODE_TEMP_DIR"] = str(path)
    if environ is os.environ:
        import tempfile
        tempfile.tempdir = str(path)
