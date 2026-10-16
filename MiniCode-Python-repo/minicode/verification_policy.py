"""Workspace choice and per-execution output directories for registered checks."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import tempfile

from minicode.path_safety import is_unsafe_stored_path


def verification_isolated(runtime, command=None):
    """Trusted configuration only; ordinary checks use the current workspace."""
    default = runtime.get("runtimeVerificationIsolation")
    if default is None:  # Optional Web continuation settings can be null.
        default = False
    if type(default) is not bool:
        raise TypeError("runtimeVerificationIsolation must be a boolean")
    value = command.get("isolate", default) if command is not None else default
    if type(value) is not bool:
        raise TypeError("verification command isolate must be a boolean")
    return value


def verification_environment(workspace):
    """Redirect temporary data/caches without changing cwd or process-global env.

    Output stays available for diagnosis. No existing data is cleaned or restored.
    Custom check commands can use MINICODE_VERIFICATION_OUTPUT_DIR for reports.
    """
    root = Path(workspace).resolve()
    if os.name == "nt":
        value = str(root)
        if not value.startswith("\\\\?\\"):
            root = Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
    directory = root / ".temp" / "verification"
    if is_unsafe_stored_path(directory, boundary=root):
        raise ValueError("verification output directory contains filesystem aliases")
    directory.mkdir(parents=True, exist_ok=True)
    if is_unsafe_stored_path(directory, boundary=root):
        raise ValueError("verification output directory changed during preparation")
    output = Path(tempfile.mkdtemp(prefix="check-", dir=directory))
    temporary, cache = output / "temporary", output / "cache"
    temporary.mkdir()
    cache.mkdir()
    environment = os.environ.copy()
    environment.update({
        "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
        "MINI_CODE_TEMP_DIR": str(temporary),
        "PYTEST_DEBUG_TEMPROOT": str(temporary),
        "PYTHONDONTWRITEBYTECODE": "1",
        "XDG_CACHE_HOME": str(cache), "MYPY_CACHE_DIR": str(cache / "mypy"),
        "RUFF_CACHE_DIR": str(cache / "ruff"),
        "COVERAGE_FILE": str(output / ".coverage"),
        "MINICODE_VERIFICATION_OUTPUT_DIR": str(output),
        "MINICODE_VERIFICATION_TEMP_DIR": str(temporary),
    })
    # Pytest parses this environment variable with shlex, including on Windows.
    environment["PYTEST_ADDOPTS"] = (environment.get("PYTEST_ADDOPTS", "") + " " +
        shlex.join(["-o", "cache_dir=" + str(cache / "pytest")])).strip()
    return environment, output
