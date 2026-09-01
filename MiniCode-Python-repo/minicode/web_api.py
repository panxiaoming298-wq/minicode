"""Local-only HTTP bridge for the MiniCode browser workbench."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import urlparse

try:
    import httpx
    import uvicorn
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field, SecretStr
    from starlette.exceptions import HTTPException as StarletteHTTPException
except ImportError as exc:  # pragma: no cover - exercised by the CLI message
    raise RuntimeError(
        "MiniCode Web dependencies are not installed. Run: pip install -e \".[web]\""
    ) from exc

from minicode.config import (
    MINI_CODE_SETTINGS_PATH,
    load_runtime_config,
    read_settings_file,
    save_mini_code_settings,
    validate_provider_runtime,
)
from minicode.release_readiness import redact_sensitive_payload
from minicode.session import (
    create_new_session,
    delete_session,
    list_sessions,
    load_session,
    save_session,
)


RunStatus = Literal["queued", "running", "stopping", "completed", "failed", "stopped"]
_TERMINAL_STATUSES = {"completed", "failed", "stopped"}
_LOCAL_HOST_RE = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]|testserver)(:\d+)?$", re.I)
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.I)
_WEB_STATE_LOCK = threading.Lock()
_WORKSPACE_PICKER_LOCK = threading.Lock()
_MAX_RECENT_WORKSPACES = 8
_MAX_DIRECTORY_ENTRIES = 500
_MAX_FILE_PREVIEW_BYTES = 1_500_000
_IGNORED_BROWSER_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "venv",
}
_BINARY_PREVIEW_SUFFIXES = {
    ".7z",
    ".avi",
    ".bmp",
    ".class",
    ".dll",
    ".doc",
    ".docx",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pyc",
    ".rar",
    ".so",
    ".tar",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".zip",
}
_PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "baseEnv": "DEEPSEEK_BASE_URL",
        "keyEnv": "DEEPSEEK_API_KEY",
        "defaultBaseUrl": "https://api.deepseek.com",
        "models": [
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
        ],
    },
    "openai": {
        "label": "OpenAI",
        "baseEnv": "OPENAI_BASE_URL",
        "keyEnv": "OPENAI_API_KEY",
        "defaultBaseUrl": "https://api.openai.com",
        "models": ["gpt-5.5", "gpt-4o", "gpt-4o-mini", "o3-mini"],
    },
    "anthropic": {
        "label": "Anthropic",
        "baseEnv": "ANTHROPIC_BASE_URL",
        "keyEnv": "ANTHROPIC_API_KEY",
        "defaultBaseUrl": "https://api.anthropic.com",
        "models": [
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-haiku-3-20240307",
        ],
    },
    "openrouter": {
        "label": "OpenRouter",
        "baseEnv": "OPENROUTER_BASE_URL",
        "keyEnv": "OPENROUTER_API_KEY",
        "defaultBaseUrl": "https://openrouter.ai/api",
        "models": [
            "openrouter/auto",
            "anthropic/claude-sonnet-4",
            "deepseek/deepseek-r1",
        ],
    },
    "custom": {
        "label": "Custom",
        "baseEnv": "CUSTOM_API_BASE_URL",
        "keyEnv": "CUSTOM_API_KEY",
        "defaultBaseUrl": "",
        "models": [],
    },
}


class SettingsUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(alias="baseUrl", min_length=1, max_length=2_000)
    api_key: SecretStr | None = Field(default=None, alias="apiKey")
    clear_api_key: bool = Field(default=False, alias="clearApiKey")
    thinking_mode: str = Field(default="", alias="thinkingMode", max_length=20)
    reasoning_effort: str = Field(default="", alias="reasoningEffort", max_length=20)


class SessionCreate(BaseModel):
    workspace: str | None = Field(default=None, max_length=4_096)
    scope: Literal["project", "standalone"] = "project"


class WorkspaceOpen(BaseModel):
    path: str = Field(min_length=1, max_length=4_096)


class WorkspacePick(BaseModel):
    initial_path: str | None = Field(default=None, alias="initialPath", max_length=4_096)


class NavigationPinUpdate(BaseModel):
    kind: Literal["workspace", "session"]
    target: str = Field(min_length=1, max_length=4_096)
    pinned: bool


class RunCreate(BaseModel):
    workspace: str | None = Field(default=None, max_length=4_096)
    prompt: str = Field(min_length=1, max_length=200_000)
    session_id: str | None = Field(default=None, alias="sessionId", max_length=64)
    scope: Literal["project", "standalone"] = "project"


class ApprovalResponse(BaseModel):
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    decision: str = Field(min_length=1, max_length=64)
    feedback: str = Field(default="", max_length=8_000)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_payload(value: Any) -> Any:
    try:
        return redact_sensitive_payload(value)
    except Exception:  # noqa: BLE001
        return value


def _resolve_workspace(value: str) -> Path:
    try:
        workspace = Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"无法解析工作区: {exc}") from exc
    if workspace.exists() and not workspace.is_dir():
        raise HTTPException(status_code=400, detail=f"请选择项目文件夹，而不是单个文件: {workspace}")
    if not workspace.is_dir():
        raise HTTPException(status_code=400, detail=f"工作区不存在: {workspace}")
    return workspace


def _web_state_path() -> Path:
    return MINI_CODE_SETTINGS_PATH.with_name("web-state.json")


def _standalone_workspace() -> Path:
    """Return the private scratch directory used by projectless conversations."""

    workspace = MINI_CODE_SETTINGS_PATH.parent / "standalone"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace.resolve()
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"无法创建独立对话工作区: {exc}") from exc


def _conversation_workspace(
    scope: Literal["project", "standalone"],
    workspace: str | None,
) -> Path:
    if scope == "standalone":
        return _standalone_workspace()
    if not workspace or not workspace.strip():
        raise HTTPException(status_code=400, detail="项目对话必须选择一个项目文件夹。")
    return _resolve_workspace(workspace)


def _read_web_state_unlocked() -> dict[str, Any]:
    path = _web_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_web_state_unlocked(payload: dict[str, Any]) -> None:
    path = _web_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _path_identity(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _workspace_payload(path: Path) -> dict[str, str]:
    return {
        "name": path.name or path.anchor or str(path),
        "path": str(path),
    }


def _record_recent_workspace(workspace: Path) -> list[dict[str, str]]:
    with _WEB_STATE_LOCK:
        state = _read_web_state_unlocked()
        raw_recent = state.get("recentWorkspaces", [])
        candidates = [str(workspace)]
        if isinstance(raw_recent, list):
            candidates.extend(str(value) for value in raw_recent if isinstance(value, str))

        recent: list[str] = []
        seen: set[str] = set()
        for value in candidates:
            candidate = Path(value).expanduser()
            try:
                candidate = candidate.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            identity = _path_identity(candidate)
            if identity in seen or not candidate.is_dir():
                continue
            seen.add(identity)
            recent.append(str(candidate))
            if len(recent) >= _MAX_RECENT_WORKSPACES:
                break

        state["recentWorkspaces"] = recent
        state["lastWorkspace"] = str(workspace)
        _write_web_state_unlocked(state)
    return [_workspace_payload(Path(value)) for value in recent]


def _recent_workspaces(current: Path) -> list[dict[str, str]]:
    with _WEB_STATE_LOCK:
        state = _read_web_state_unlocked()
    raw_recent = state.get("recentWorkspaces", [])
    candidates = [str(current)]
    if isinstance(raw_recent, list):
        candidates.extend(str(value) for value in raw_recent if isinstance(value, str))

    recent: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in candidates:
        candidate = Path(value).expanduser()
        try:
            candidate = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        identity = _path_identity(candidate)
        if identity in seen or not candidate.is_dir():
            continue
        seen.add(identity)
        recent.append(_workspace_payload(candidate))
        if len(recent) >= _MAX_RECENT_WORKSPACES:
            break
    return recent


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _standalone_session_ids(state: dict[str, Any] | None = None) -> set[str]:
    payload = state
    if payload is None:
        with _WEB_STATE_LOCK:
            payload = _read_web_state_unlocked()
    return set(_clean_string_list(payload.get("standaloneSessions")))


def _session_scope(session_id: str, workspace: str, state: dict[str, Any] | None = None) -> Literal["project", "standalone"]:
    if session_id in _standalone_session_ids(state):
        return "standalone"
    try:
        if _path_identity(Path(workspace).resolve()) == _path_identity(_standalone_workspace()):
            return "standalone"
    except (OSError, RuntimeError, ValueError):
        pass
    return "project"


def _set_session_scope(session_id: str, scope: Literal["project", "standalone"]) -> None:
    with _WEB_STATE_LOCK:
        state = _read_web_state_unlocked()
        values = [value for value in _clean_string_list(state.get("standaloneSessions")) if value != session_id]
        if scope == "standalone":
            values.insert(0, session_id)
        state["standaloneSessions"] = values[:500]
        _write_web_state_unlocked(state)


def _navigation_payload(current: Path) -> dict[str, Any]:
    with _WEB_STATE_LOCK:
        state = _read_web_state_unlocked()

    sessions = list_sessions()
    session_ids = {item.session_id for item in sessions}
    pinned_session_ids = [
        value
        for value in _clean_string_list(state.get("pinnedSessions"))
        if value in session_ids
    ]

    recent = _recent_workspaces(current)
    known_workspace_paths = {item["path"] for item in recent}
    pinned_workspaces: list[dict[str, str]] = []
    for value in _clean_string_list(state.get("pinnedWorkspaces")):
        try:
            candidate = Path(value).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if not candidate.is_dir():
            continue
        payload = _workspace_payload(candidate)
        if payload["path"] not in known_workspace_paths:
            recent.append(payload)
            known_workspace_paths.add(payload["path"])
        pinned_workspaces.append(payload)

    return {
        "projects": recent,
        "sessions": [
            _session_metadata_payload(
                item,
                scope=_session_scope(item.session_id, item.workspace, state),
            )
            for item in sessions
        ],
        "pinnedSessionIds": pinned_session_ids,
        "pinnedWorkspacePaths": [item["path"] for item in pinned_workspaces],
    }


def _set_navigation_pin(update: NavigationPinUpdate) -> None:
    key = "pinnedWorkspaces" if update.kind == "workspace" else "pinnedSessions"
    if update.kind == "workspace":
        target = str(_resolve_workspace(update.target))
    else:
        if load_session(update.target) is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        target = update.target

    with _WEB_STATE_LOCK:
        state = _read_web_state_unlocked()
        values = _clean_string_list(state.get(key))
        identity = _path_identity(Path(target)) if update.kind == "workspace" else target
        kept = [
            value
            for value in values
            if (_path_identity(Path(value)) if update.kind == "workspace" else value) != identity
        ]
        if update.pinned:
            kept.insert(0, target)
        state[key] = kept[:50]
        _write_web_state_unlocked(state)


def _remove_session_navigation_state(session_id: str) -> None:
    with _WEB_STATE_LOCK:
        state = _read_web_state_unlocked()
        state["pinnedSessions"] = [
            value for value in _clean_string_list(state.get("pinnedSessions")) if value != session_id
        ]
        state["standaloneSessions"] = [
            value for value in _clean_string_list(state.get("standaloneSessions")) if value != session_id
        ]
        _write_web_state_unlocked(state)


def _parse_picker_output(result: subprocess.CompletedProcess[str]) -> Path | None:
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "系统文件夹选择器不可用。"
        raise HTTPException(status_code=503, detail=detail)
    output_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    try:
        selected = json.loads(output_lines[-1] if output_lines else '""')
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="文件夹选择器返回了无效结果。") from exc
    if not selected:
        return None
    return _resolve_workspace(str(selected))


def _pick_windows_workspace_dialog(initial: Path) -> Path | None:
    """Open a foreground-owned WinForms folder dialog on Windows."""

    picker = r'''
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class MiniCodeWindow {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@
$initial = if ([System.IO.Directory]::Exists($env:MINICODE_PICKER_INITIAL)) { $env:MINICODE_PICKER_INITIAL } else { [Environment]::CurrentDirectory }
$owner = New-Object System.Windows.Forms.Form
$owner.Text = "MiniCode"
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "选择 MiniCode 项目文件夹"
$dialog.SelectedPath = $initial
$dialog.ShowNewFolderButton = $true
try {
    $owner.Show()
    $owner.Activate()
    [MiniCodeWindow]::SetForegroundWindow($owner.Handle) | Out-Null
    $result = $dialog.ShowDialog($owner)
    $selected = if ($result -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath } else { "" }
    Write-Output (ConvertTo-Json $selected -Compress)
} finally {
    $dialog.Dispose()
    $owner.Close()
    $owner.Dispose()
}
'''
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-Command",
                picker,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "MINICODE_PICKER_INITIAL": str(initial)},
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="文件夹选择已超时。") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"无法启动 Windows 文件夹选择器: {exc}") from exc
    return _parse_picker_output(result)


def _pick_tk_workspace_dialog(initial: Path) -> Path | None:
    """Portable fallback for non-Windows desktop environments.

    Browsers do not expose a selected directory's absolute path to JavaScript,
    so the local bridge owns the native dialog and returns a real path.
    """

    picker = r'''
import json
import os
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except tk.TclError:
    pass
root.lift()
root.focus_force()
root.update_idletasks()
initial = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else os.getcwd()
selected = filedialog.askdirectory(
    parent=root,
    initialdir=initial,
    mustexist=True,
    title="选择 MiniCode 项目文件夹",
)
print(json.dumps(selected, ensure_ascii=False))
root.destroy()
'''
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [sys.executable, "-c", picker, str(initial)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="文件夹选择已超时，请改为输入路径。") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"无法打开系统文件夹选择器: {exc}") from exc

    return _parse_picker_output(result)


def _pick_workspace_dialog(initial: Path) -> Path | None:
    if not _WORKSPACE_PICKER_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="文件夹选择器已经打开，请先完成或取消当前选择。")
    try:
        if os.name == "nt":
            return _pick_windows_workspace_dialog(initial)
        return _pick_tk_workspace_dialog(initial)
    finally:
        _WORKSPACE_PICKER_LOCK.release()


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    suffix = secret[-4:] if len(secret) >= 4 else secret[-1:]
    return f"••••••••{suffix}"


def _stored_settings() -> dict[str, Any]:
    return read_settings_file(MINI_CODE_SETTINGS_PATH)


def _setting_with_source(
    stored: dict[str, Any],
    env_name: str,
    root_key: str = "",
    default: str = "",
) -> tuple[str, str]:
    process_value = os.environ.get(env_name, "").strip()
    if process_value:
        return process_value, "environment"
    stored_env = stored.get("env", {})
    if isinstance(stored_env, dict):
        value = str(stored_env.get(env_name, "") or "").strip()
        if value:
            return value, "settings"
    if root_key:
        value = str(stored.get(root_key, "") or "").strip()
        if value:
            return value, "settings"
    return default, "default"


def get_settings_view() -> dict[str, Any]:
    stored = _stored_settings()
    provider = str(
        os.environ.get("MINI_CODE_PROVIDER", "")
        or stored.get("provider", "")
        or (stored.get("env", {}) or {}).get("MINI_CODE_PROVIDER", "")
        or "deepseek"
    ).strip().lower()
    if provider not in _PROVIDERS:
        provider = "custom"
    spec = _PROVIDERS[provider]
    model = str(
        os.environ.get("MINI_CODE_MODEL", "")
        or stored.get("model", "")
        or (stored.get("env", {}) or {}).get("MINI_CODE_MODEL", "")
        or (spec["models"][0] if spec["models"] else "")
    ).strip()
    root_base_key = {
        "deepseek": "deepseekBaseUrl",
        "openai": "openaiBaseUrl",
        "custom": "customBaseUrl",
    }.get(provider, "")
    root_key_key = {
        "deepseek": "deepseekApiKey",
        "openai": "openaiApiKey",
        "custom": "customApiKey",
    }.get(provider, "")
    base_url, base_source = _setting_with_source(
        stored,
        spec["baseEnv"],
        root_base_key,
        spec["defaultBaseUrl"],
    )
    api_key, key_source = _setting_with_source(stored, spec["keyEnv"], root_key_key)
    thinking, _ = _setting_with_source(stored, "DEEPSEEK_THINKING_MODE")
    effort, _ = _setting_with_source(stored, "DEEPSEEK_REASONING_EFFORT")
    return {
        "provider": provider,
        "model": model,
        "baseUrl": base_url,
        "baseUrlSource": base_source,
        "apiKeyConfigured": bool(api_key),
        "apiKeyMask": _mask_secret(api_key),
        "apiKeySource": key_source if api_key else "none",
        "thinkingMode": thinking or "enabled",
        "reasoningEffort": effort or "high",
        "settingsPath": str(MINI_CODE_SETTINGS_PATH),
        "providers": [
            {
                "id": provider_id,
                "label": provider_spec["label"],
                "defaultBaseUrl": provider_spec["defaultBaseUrl"],
                "models": provider_spec["models"],
            }
            for provider_id, provider_spec in _PROVIDERS.items()
        ],
    }


def save_settings(update: SettingsUpdate) -> dict[str, Any]:
    provider = update.provider.strip().lower()
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=400, detail="不支持的模型提供商。")
    model = update.model.strip()
    base_url = update.base_url.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Base URL 必须是有效的 http(s) 地址。")
    if provider == "deepseek" and update.thinking_mode not in {"enabled", "disabled", ""}:
        raise HTTPException(status_code=400, detail="DeepSeek 思考模式只能是 enabled 或 disabled。")
    if provider == "deepseek" and update.reasoning_effort not in {"low", "high", "max", ""}:
        raise HTTPException(status_code=400, detail="DeepSeek 推理强度只能是 low、high 或 max。")

    spec = _PROVIDERS[provider]
    env_updates: dict[str, str] = {
        "MINI_CODE_PROVIDER": provider,
        "MINI_CODE_MODEL": model,
        spec["baseEnv"]: base_url,
    }
    if provider == "deepseek":
        env_updates["DEEPSEEK_MODEL"] = model
        env_updates["DEEPSEEK_THINKING_MODE"] = update.thinking_mode or "enabled"
        env_updates["DEEPSEEK_REASONING_EFFORT"] = update.reasoning_effort or "high"
    if update.clear_api_key:
        env_updates[spec["keyEnv"]] = ""
    elif update.api_key is not None:
        key_value = update.api_key.get_secret_value().strip()
        if key_value:
            env_updates[spec["keyEnv"]] = key_value

    save_mini_code_settings({"provider": provider, "model": model, "env": env_updates})
    try:
        if os.name != "nt":
            MINI_CODE_SETTINGS_PATH.chmod(0o600)
    except OSError:
        pass
    return get_settings_view()


def _models_endpoint(provider: str, base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if provider == "deepseek":
        return f"{base}/models"
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def test_saved_provider() -> dict[str, Any]:
    if os.environ.get("MINI_CODE_WEB_FORCE_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}:
        return {"ok": True, "latencyMs": 1, "message": "模拟运行模式已就绪。"}
    try:
        runtime = load_runtime_config(Path.cwd())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    errors = validate_provider_runtime(runtime, probe_openai_models=False)
    if errors:
        raise HTTPException(status_code=400, detail=" ".join(errors))
    provider = str(runtime.get("provider", "")).strip().lower()
    started = time.monotonic()
    headers = {"User-Agent": "MiniCode-Web/0.1"}
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            if provider == "anthropic":
                key = str(runtime.get("apiKey") or runtime.get("authToken") or "")
                headers.update(
                    {
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    }
                )
                response = client.post(
                    f"{str(runtime.get('baseUrl', '')).rstrip('/')}/v1/messages",
                    headers=headers,
                    json={
                        "model": runtime.get("model"),
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                )
            else:
                key_name = {
                    "deepseek": "deepseekApiKey",
                    "openai": "openaiApiKey",
                    "openrouter": "openrouterApiKey",
                    "custom": "customApiKey",
                }.get(provider, "openaiApiKey")
                base_name = {
                    "deepseek": "deepseekBaseUrl",
                    "openai": "openaiBaseUrl",
                    "openrouter": "openrouterBaseUrl",
                    "custom": "customBaseUrl",
                }.get(provider, "openaiBaseUrl")
                headers["Authorization"] = f"Bearer {runtime.get(key_name, '')}"
                response = client.get(
                    _models_endpoint(provider, str(runtime.get(base_name, ""))),
                    headers=headers,
                )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        raise HTTPException(
            status_code=502,
            detail=f"提供商返回 HTTP {exc.response.status_code}: {detail}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接模型提供商: {exc}") from exc
    return {
        "ok": True,
        "latencyMs": round((time.monotonic() - started) * 1000),
        "message": f"{_PROVIDERS.get(provider, {}).get('label', provider)} 连接成功。",
    }


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    workspace: str
    prompt_preview: str
    status: RunStatus = "queued"
    created_at: int = field(default_factory=_now_ms)
    updated_at: int = field(default_factory=_now_ms)
    process: subprocess.Popen[str] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)
    saw_done: bool = False
    sequence: int = 0
    condition: threading.Condition = field(default_factory=threading.Condition, repr=False)
    write_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_event(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.condition:
            self.sequence += 1
            payload = dict(_safe_payload(event))
            payload["sequence"] = self.sequence
            payload["timestamp"] = _now_ms()
            self.events.append(payload)
            self.updated_at = payload["timestamp"]
            if payload.get("kind") == "done":
                self.saw_done = True
            self.condition.notify_all()
            return payload

    def public(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "sessionId": self.session_id,
            "workspace": self.workspace,
            "promptPreview": self.prompt_preview,
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "eventCount": len(self.events),
        }


class RunManager:
    def __init__(self, max_active: int = 4) -> None:
        self.max_active = max_active
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self._runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="任务不存在。")
        return record

    def _active_count(self) -> int:
        return sum(1 for run in self._runs.values() if run.status not in _TERMINAL_STATUSES)

    def start(self, workspace: Path, prompt: str, session_id: str | None) -> RunRecord:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise HTTPException(status_code=400, detail="请输入任务内容。")
        with self._lock:
            if self._active_count() >= self.max_active:
                raise HTTPException(status_code=429, detail="同时运行的任务过多，请稍后再试。")
            if session_id:
                session = load_session(session_id)
                if session is None:
                    raise HTTPException(status_code=404, detail="会话不存在。")
                if Path(session.workspace).resolve() != workspace:
                    raise HTTPException(status_code=400, detail="会话不属于当前工作区。")
                if any(
                    run.session_id == session_id and run.status not in _TERMINAL_STATUSES
                    for run in self._runs.values()
                ):
                    raise HTTPException(status_code=409, detail="该会话已有任务正在运行。")
            else:
                session = create_new_session(str(workspace))
                save_session(session, force_full=True)
            run_id = uuid.uuid4().hex[:16]
            record = RunRecord(
                run_id=run_id,
                session_id=session.session_id,
                workspace=str(workspace),
                prompt_preview=clean_prompt[:120],
            )
            self._runs[run_id] = record

        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repo_root) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "minicode.web_worker",
                    "--workspace",
                    str(workspace),
                    "--session",
                    session.session_id,
                ],
                cwd=str(workspace),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        except OSError as exc:
            record.status = "failed"
            record.add_event({"kind": "error", "message": f"无法启动任务进程: {exc}"})
            raise HTTPException(status_code=500, detail="无法启动任务进程。") from exc

        record.process = process
        record.status = "running"
        record.add_event({"kind": "status", "status": "running", "content": "任务已启动"})
        threading.Thread(target=self._read_stdout, args=(record,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(record,), daemon=True).start()
        threading.Thread(target=self._wait_for_exit, args=(record,), daemon=True).start()
        self._write(
            record,
            {"kind": "start", "prompt": clean_prompt},
        )
        return record

    def _write(self, record: RunRecord, payload: dict[str, Any]) -> None:
        process = record.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise HTTPException(status_code=409, detail="任务进程已结束。")
        try:
            with record.write_lock:
                process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise HTTPException(status_code=409, detail="无法向任务进程发送消息。") from exc

    def _read_stdout(self, record: RunRecord) -> None:
        process = record.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                record.stderr_tail.append(line[:1_000])
                record.stderr_tail[:] = record.stderr_tail[-20:]
                continue
            if isinstance(payload, dict):
                record.add_event(payload)

    def _read_stderr(self, record: RunRecord) -> None:
        process = record.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            clean = str(_safe_payload(line.strip()))
            if clean:
                record.stderr_tail.append(clean[:1_000])
                record.stderr_tail[:] = record.stderr_tail[-20:]

    def _wait_for_exit(self, record: RunRecord) -> None:
        process = record.process
        if process is None:
            return
        return_code = process.wait()
        if record.status == "stopping":
            final_status: RunStatus = "stopped"
        elif return_code == 0:
            final_status = "completed"
        else:
            final_status = "failed"
        try:
            session = load_session(record.session_id)
            if session is not None:
                session.metadata.status = final_status
                save_session(session, force_full=True)
        except (OSError, TypeError, ValueError):
            # The run event remains authoritative even if a damaged legacy
            # session cannot be updated during process teardown.
            pass
        if final_status == "failed":
            message = record.stderr_tail[-1] if record.stderr_tail else f"工作进程退出码: {return_code}"
            record.add_event({"kind": "error", "message": message})
        if not record.saw_done:
            record.add_event(
                {
                    "kind": "done",
                    "stopReason": final_status,
                }
            )
        record.status = final_status
        record.add_event({"kind": "status", "status": final_status, "content": "任务已结束"})
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass

    def stop(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        if record.status in _TERMINAL_STATUSES:
            return record
        record.status = "stopping"
        record.add_event({"kind": "status", "status": "stopping", "content": "正在停止任务"})

        def terminate() -> None:
            process = record.process
            if process is None or process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

        threading.Thread(target=terminate, daemon=True).start()
        return record

    def approve(self, run_id: str, response: ApprovalResponse) -> RunRecord:
        record = self.get(run_id)
        allowed = {
            "allow_once",
            "allow_turn",
            "allow_all_turn",
            "allow_always",
            "deny_once",
            "deny_with_feedback",
            "deny_always",
        }
        if response.decision not in allowed:
            raise HTTPException(status_code=400, detail="无效的审批决定。")
        self._write(
            record,
            {
                "kind": "approval_response",
                "requestId": response.request_id,
                "decision": response.decision,
                "feedback": response.feedback,
            },
        )
        return record

    def session_is_active(self, session_id: str) -> bool:
        with self._lock:
            return any(
                record.session_id == session_id and record.status not in _TERMINAL_STATUSES
                for record in self._runs.values()
            )


def _session_metadata_payload(
    metadata: Any,
    *,
    scope: Literal["project", "standalone"] = "project",
) -> dict[str, Any]:
    value = asdict(metadata)
    workspace = value.get("workspace", "")
    return {
        "sessionId": value.get("session_id", ""),
        "createdAt": round(float(value.get("created_at", 0)) * 1000),
        "updatedAt": round(float(value.get("updated_at", 0)) * 1000),
        "firstMessage": value.get("first_message", ""),
        "lastMessage": value.get("last_message", ""),
        "messageCount": value.get("message_count", 0),
        "workspace": workspace,
        "scope": scope,
        "projectPath": workspace if scope == "project" else None,
        "model": value.get("model"),
        "status": value.get("status", "active"),
        "toolCallCount": value.get("tool_call_count", 0),
    }


def _run_git(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail=f"Git 不可用: {exc}") from exc


def _workspace_status(workspace: Path) -> dict[str, Any]:
    result = _run_git(workspace, ["status", "--short", "--branch", "--", "."])
    if result.returncode != 0:
        return {"isGitRepository": False, "branch": "", "files": [], "summary": "非 Git 工作区"}
    lines = result.stdout.splitlines()
    branch = lines[0][3:] if lines and lines[0].startswith("## ") else ""
    files = []
    for line in lines[1:] if lines and lines[0].startswith("## ") else lines:
        if len(line) < 4:
            continue
        files.append({"status": line[:2], "path": line[3:]})
    return {
        "isGitRepository": True,
        "branch": branch,
        "files": files,
        "summary": f"{len(files)} 个文件有变更" if files else "工作区干净",
    }


def _resolve_workspace_member(workspace: Path, value: str, *, allow_root: bool = False) -> Path:
    clean_value = value.strip()
    if not clean_value or clean_value == ".":
        if allow_root:
            return workspace
        raise HTTPException(status_code=400, detail="请选择工作区内的文件。")
    relative = Path(clean_value)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise HTTPException(status_code=400, detail="路径必须位于当前工作区内。")

    cursor = workspace
    for part in relative.parts:
        if part in {"", "."}:
            continue
        cursor /= part
        if cursor.is_symlink():
            raise HTTPException(status_code=400, detail="文件浏览器不跟随符号链接。")
    try:
        resolved = (workspace / relative).resolve(strict=True)
        resolved.relative_to(workspace)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="工作区中的路径不存在。") from exc
    return resolved


def _list_workspace_entries(workspace: Path, directory: str) -> dict[str, Any]:
    target = _resolve_workspace_member(workspace, directory, allow_root=True)
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="要展开的路径不是文件夹。")
    try:
        candidates = list(target.iterdir())
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"无法读取文件夹: {exc}") from exc

    entries: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.is_symlink():
            continue
        try:
            is_directory = candidate.is_dir()
            is_file = candidate.is_file()
        except OSError:
            continue
        if not is_directory and not is_file:
            continue
        if is_directory and candidate.name.casefold() in _IGNORED_BROWSER_DIRECTORIES:
            continue
        try:
            relative = candidate.relative_to(workspace).as_posix()
            size = candidate.stat().st_size if is_file else None
        except OSError:
            continue
        entries.append(
            {
                "name": candidate.name,
                "path": relative,
                "kind": "directory" if is_directory else "file",
                "size": size,
            }
        )
    entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold()))
    truncated = len(entries) > _MAX_DIRECTORY_ENTRIES
    directory_value = "" if target == workspace else target.relative_to(workspace).as_posix()
    return {
        "directory": directory_value,
        "entries": entries[:_MAX_DIRECTORY_ENTRIES],
        "truncated": truncated,
    }


def _read_workspace_file(workspace: Path, file: str) -> dict[str, Any]:
    target = _resolve_workspace_member(workspace, file)
    if not target.is_file():
        raise HTTPException(status_code=400, detail="选择的路径不是文件。")
    if target.suffix.casefold() in _BINARY_PREVIEW_SUFFIXES:
        raise HTTPException(status_code=415, detail="该文件是二进制格式，暂不支持原文预览。")
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            raw = handle.read(_MAX_FILE_PREVIEW_BYTES + 1)
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"无法读取文件: {exc}") from exc
    if b"\x00" in raw[:8_192]:
        raise HTTPException(status_code=415, detail="该文件包含二进制内容，暂不支持原文预览。")

    truncated = len(raw) > _MAX_FILE_PREVIEW_BYTES
    preview = raw[:_MAX_FILE_PREVIEW_BYTES]
    encoding = "utf-8"
    try:
        content = preview.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = preview.decode("gb18030")
            encoding = "gb18030"
        except UnicodeDecodeError:
            content = preview.decode("utf-8", errors="replace")
            encoding = "unknown"
    relative = target.relative_to(workspace).as_posix()
    return {
        "path": relative,
        "name": target.name,
        "content": content,
        "encoding": encoding,
        "size": size,
        "lineCount": content.count("\n") + (1 if content else 0),
        "truncated": truncated,
    }


class SpaStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]):  # type: ignore[override]
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in Path(path).name:
                return await super().get_response("index.html", scope)
            raise
        if response.status_code == 404 and "." not in Path(path).name:
            return await super().get_response("index.html", scope)
        return response


def create_app(
    *,
    default_workspace: str | Path | None = None,
    frontend_dir: str | Path | None = None,
    allow_remote: bool = False,
) -> FastAPI:
    workspace_default = Path(default_workspace or Path.cwd()).expanduser().resolve()
    manager = RunManager()
    app = FastAPI(title="MiniCode Local Web", version="0.1.0")
    app.state.run_manager = manager
    app.state.default_workspace = workspace_default
    app.state.current_workspace = workspace_default
    if workspace_default.is_dir():
        _record_recent_workspace(workspace_default)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Accept"],
    )

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if not allow_remote:
            host = request.headers.get("host", "")
            if not _LOCAL_HOST_RE.fullmatch(host):
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=403, content={"detail": "MiniCode Web 只接受本机请求。"})
            origin = request.headers.get("origin", "")
            if origin and not _LOCAL_ORIGIN_RE.fullmatch(origin):
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=403, content={"detail": "请求来源不被允许。"})
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "minicode-web", "localOnly": not allow_remote}

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        current_workspace: Path = app.state.current_workspace
        return {
            "workspace": str(current_workspace),
            "settings": get_settings_view(),
            "workspaceStatus": _workspace_status(current_workspace) if current_workspace.is_dir() else None,
            "recentWorkspaces": _recent_workspaces(current_workspace),
        }

    @app.get("/api/settings")
    def settings_get() -> dict[str, Any]:
        return get_settings_view()

    @app.put("/api/settings")
    def settings_put(update: SettingsUpdate) -> dict[str, Any]:
        return save_settings(update)

    @app.post("/api/settings/test")
    async def settings_test() -> dict[str, Any]:
        return await asyncio.to_thread(test_saved_provider)

    @app.post("/api/workspaces/pick")
    def workspace_pick(body: WorkspacePick) -> dict[str, Any]:
        initial: Path = app.state.current_workspace
        if body.initial_path:
            try:
                initial = _resolve_workspace(body.initial_path)
            except HTTPException:
                pass
        selected = _pick_workspace_dialog(initial)
        return {
            "cancelled": selected is None,
            "workspace": str(selected) if selected else "",
        }

    @app.post("/api/workspaces/open")
    def workspace_open(body: WorkspaceOpen) -> dict[str, Any]:
        workspace = _resolve_workspace(body.path)
        app.state.current_workspace = workspace
        recent = _record_recent_workspace(workspace)
        return {
            "workspace": str(workspace),
            "workspaceStatus": _workspace_status(workspace),
            "recentWorkspaces": recent,
            "sessions": [
                _session_metadata_payload(item)
                for item in list_sessions(str(workspace))
            ],
        }

    @app.get("/api/navigation")
    def navigation_get() -> dict[str, Any]:
        return _navigation_payload(app.state.current_workspace)

    @app.put("/api/navigation/pins")
    def navigation_pin(update: NavigationPinUpdate) -> dict[str, Any]:
        _set_navigation_pin(update)
        return _navigation_payload(app.state.current_workspace)

    @app.get("/api/sessions")
    def sessions_get(workspace: str | None = Query(default=None)) -> dict[str, Any]:
        resolved = str(_resolve_workspace(workspace)) if workspace else str(app.state.current_workspace)
        return {
            "sessions": [
                _session_metadata_payload(
                    item,
                    scope=_session_scope(item.session_id, item.workspace),
                )
                for item in list_sessions(resolved)
            ]
        }

    @app.post("/api/sessions")
    def sessions_post(body: SessionCreate) -> dict[str, Any]:
        workspace = _conversation_workspace(body.scope, body.workspace)
        session = create_new_session(str(workspace))
        save_session(session, force_full=True)
        _set_session_scope(session.session_id, body.scope)
        return {"session": _session_metadata_payload(session.metadata, scope=body.scope)}

    @app.get("/api/sessions/{session_id}")
    def session_get(session_id: str) -> dict[str, Any]:
        session = load_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        messages = [
            _safe_payload(message)
            for message in session.messages
            if isinstance(message, dict) and message.get("role") != "system"
        ]
        return {
            "session": _session_metadata_payload(
                session.metadata,
                scope=_session_scope(session.session_id, session.workspace),
            ),
            "messages": messages[-300:],
        }

    @app.delete("/api/sessions/{session_id}")
    def session_delete(session_id: str) -> dict[str, Any]:
        if manager.session_is_active(session_id):
            raise HTTPException(status_code=409, detail="任务运行中，请先停止再删除会话。")
        if not delete_session(session_id):
            raise HTTPException(status_code=404, detail="会话不存在。")
        _remove_session_navigation_state(session_id)
        return {"deleted": True}

    @app.post("/api/runs")
    def runs_post(body: RunCreate) -> dict[str, Any]:
        workspace = _conversation_workspace(body.scope, body.workspace)
        if body.session_id:
            session = load_session(body.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="会话不存在。")
            existing_scope = _session_scope(session.session_id, session.workspace)
            if existing_scope != body.scope:
                raise HTTPException(status_code=400, detail="不能在项目对话与独立对话之间切换会话类型。")
        record = manager.start(workspace, body.prompt, body.session_id)
        _set_session_scope(record.session_id, body.scope)
        return {"run": {**record.public(), "scope": body.scope}}

    @app.get("/api/runs/{run_id}")
    def run_get(run_id: str) -> dict[str, Any]:
        record = manager.get(run_id)
        return {"run": record.public(), "events": list(record.events)}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str, after: int = Query(default=0, ge=0)):
        record = manager.get(run_id)

        async def stream() -> Iterator[str]:
            cursor = after
            heartbeat_at = time.monotonic()
            while True:
                if await request.is_disconnected():
                    break
                with record.condition:
                    pending = [event for event in record.events if int(event.get("sequence", 0)) > cursor]
                    status = record.status
                for event in pending:
                    cursor = int(event["sequence"])
                    yield f"id: {cursor}\nevent: message\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if status in _TERMINAL_STATUSES and not pending:
                    break
                if time.monotonic() - heartbeat_at > 10:
                    heartbeat_at = time.monotonic()
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/stop")
    def run_stop(run_id: str) -> dict[str, Any]:
        return {"run": manager.stop(run_id).public()}

    @app.post("/api/runs/{run_id}/approvals")
    def run_approval(run_id: str, body: ApprovalResponse) -> dict[str, Any]:
        return {"run": manager.approve(run_id, body).public()}

    @app.get("/api/workspace/status")
    def workspace_status(path: str = Query(min_length=1, max_length=4_096)) -> dict[str, Any]:
        return _workspace_status(_resolve_workspace(path))

    @app.get("/api/workspace/files")
    def workspace_files(
        path: str = Query(min_length=1, max_length=4_096),
        directory: str = Query(default="", max_length=4_096),
    ) -> dict[str, Any]:
        return _list_workspace_entries(_resolve_workspace(path), directory)

    @app.get("/api/workspace/file")
    def workspace_file(
        path: str = Query(min_length=1, max_length=4_096),
        file: str = Query(min_length=1, max_length=4_096),
    ) -> dict[str, Any]:
        return _read_workspace_file(_resolve_workspace(path), file)

    @app.get("/api/workspace/diff")
    def workspace_diff(
        path: str = Query(min_length=1, max_length=4_096),
        file: str | None = Query(default=None, max_length=4_096),
    ) -> dict[str, Any]:
        workspace = _resolve_workspace(path)
        args = ["diff", "--no-ext-diff", "--unified=3"]
        if file:
            file_path = Path(file)
            if file_path.is_absolute():
                raise HTTPException(status_code=400, detail="文件必须位于当前工作区内。")
            try:
                resolved_file = (workspace / file_path).resolve()
                relative_file = resolved_file.relative_to(workspace)
            except (OSError, RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="文件必须位于当前工作区内。") from exc
            args.extend(["--", relative_file.as_posix()])
        result = _run_git(workspace, args)
        if result.returncode not in {0, 1}:
            raise HTTPException(status_code=400, detail=result.stderr.strip() or "无法读取 Git diff。")
        return {"diff": result.stdout, "file": file or ""}

    resolved_frontend = Path(
        frontend_dir
        or os.environ.get("MINI_CODE_WEB_FRONTEND", "")
        or (Path(__file__).resolve().parents[2] / "MiniCode-Web" / "dist")
    ).expanduser()
    if resolved_frontend.is_dir() and (resolved_frontend / "index.html").is_file():
        app.mount("/", SpaStaticFiles(directory=str(resolved_frontend), html=True), name="frontend")
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Start the local MiniCode browser workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", default=str(Path.cwd()))
    parser.add_argument("--frontend", default=None)
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
        parser.error("非本机绑定必须显式传入 --allow-remote。")
    uvicorn.run(
        create_app(
            default_workspace=args.workspace,
            frontend_dir=args.frontend,
            allow_remote=args.allow_remote,
        ),
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
