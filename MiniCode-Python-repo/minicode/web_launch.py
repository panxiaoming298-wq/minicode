"""Select the repository's Python environment and check Web dependencies."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path


def prefer_project_python(argv: list[str]) -> None:
    """A source checkout must not silently start with the global interpreter."""
    root = Path(__file__).resolve().parents[1]
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if (root / "pyproject.toml").is_file() and python.is_file() and Path(sys.prefix).resolve() != environment.resolve():
        raise SystemExit(subprocess.call([str(python), "-m", "minicode.web_api", *argv]))


def startup_report(workspace: str | Path) -> dict:
    """Report only environment metadata; never return settings or credentials."""
    from minicode.config import MINI_CODE_SETTINGS_PATH, load_effective_settings, read_settings_file
    from minicode.paged_memory.config import PagedMemoryConfig

    settings = read_settings_file(MINI_CODE_SETTINGS_PATH)
    config = PagedMemoryConfig.from_sources(load_effective_settings(workspace), cwd=workspace)
    from minicode.history_storage import resolve_history_backend
    history = resolve_history_backend(settings)
    modules = ["fastapi", "uvicorn", "httpx"]
    dependencies = {}
    for name in modules:
        try:
            importlib.import_module(name)
            dependencies[name] = True
        except ImportError:
            dependencies[name] = False
    return {
        "ok": all(dependencies.values()),
        "pythonExecutable": sys.executable,
        "pythonPrefix": sys.prefix,
        "historyBackend": history,
        "memoryBackend": config.backend,
        "memorySqlitePath": config.sqlite_path,
        "artifactStoreType": config.artifact_store_type,
        "artifactStoreRoot": config.artifact_root,
        "memoryExportRoot": config.export_root,
        "dependencies": dependencies,
    }


def terminate_worker(process: subprocess.Popen) -> None:
    """Stop the Windows venv launcher AND its worker, without touching peers."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
        )
        if result.returncode and process.poll() is None:
            raise RuntimeError("无法停止任务进程树，请检查本机进程权限。")
    else:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
