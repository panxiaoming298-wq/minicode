"""Local-only HTTP bridge for the MiniCode browser workbench."""

from __future__ import annotations

from copy import deepcopy
from contextlib import asynccontextmanager

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from minicode.web_launch import prefer_project_python, startup_report, terminate_worker

if __name__ == "__main__":
    prefer_project_python(sys.argv[1:])

try:
    import httpx
    import uvicorn
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse, Response
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field, SecretStr, StrictBool, ValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException
except ImportError as exc:  # pragma: no cover - exercised by the CLI message
    raise RuntimeError(
        "MiniCode Web dependencies are not installed. Run: pip install -e \".[web]\""
    ) from exc

from minicode.config import (
    MINI_CODE_SETTINGS_PATH,
    load_effective_settings,
    load_runtime_config,
    read_settings_file,
    save_mini_code_settings,
    validate_provider_runtime,
)
from minicode.extension_agents import list_extension_agent_profiles
from minicode.extension_commands import list_extension_commands
from minicode.extensions import (
    ExtensionError,
    ExtensionValidationError,
    collect_extension_mcp_servers,
    extension_provider_presets,
    extension_root,
    inspect_extension_health,
    install_extension,
    install_mcp_extension,
    resolve_extension,
    resolve_extension_set,
    rollback_extension,
    set_extension_enabled,
    set_extension_trusted,
    uninstall_extension,
)
from minicode.context_sources import load_sources, memory_inventory, revise_memory
from minicode.product_surfaces import build_product_snapshot
from minicode.release_readiness import redact_sensitive_payload
from minicode.provider_balance import BalanceQueryError, is_official_deepseek_url, query_deepseek_balance
from minicode.web_connection_history import connection_history
from minicode.session import (
    create_new_session,
    delete_session,
    list_sessions,
    load_session,
    save_session,
)
from minicode import session as session_store
from minicode.session_context import ConversationContext, load_context_settings, save_context_settings
from minicode.session_trash import SessionTrash, SessionTrashError, TrashedSession
from minicode.store_lock import StoreBusyError
from minicode.tools import create_default_tool_registry
from minicode.turn_changes import change_summary, project_change_event, project_file_changes
from minicode.unified_runtime import requires_workspace_write
from minicode.headless_task import HeadlessTask, load_headless_task
from minicode.entry_task import selected_task, task_packet, task_preview
from minicode.web_runtime_settings import ExecutionLocation, apply_execution_location
from minicode.web_runtime_settings import (
    RunBudget,
    WebAgentSettings,
    RuntimeModelPricing,
    RuntimeSettingsUpdate,
    runtime_settings_updates,
    runtime_settings_view,
)
from minicode.web_surfaces import (
    capability_surface,
    public_checkpoints,
    public_context_stats,
    public_delegation_status,
    public_extensions,
    public_hook_status,
    public_instruction_layers,
    public_mcp_servers,
    public_readiness,
    public_runtime_trace,
    public_skills,
)
from minicode.workspace_lock import normalize_workspace_path
from minicode.web_run_events import RunEventJournal

RunStatus = Literal["queued", "running", "stopping", "completed", "failed", "stopped"]
ExecutionProfile = Literal["standard", "deep", "ultra"]
_TERMINAL_STATUSES = {"completed", "failed", "stopped"}
_LOCAL_HOST_RE = re.compile(
    r"^(localhost|127\.0\.0\.1|\[::1\]|testserver)(:\d+)?$", re.IGNORECASE
)
_LOCAL_ORIGIN_RE = re.compile(
    r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.IGNORECASE
)
_WEB_STATE_LOCK = threading.Lock()
_WORKSPACE_PICKER_LOCK = threading.Lock()
_MAX_DIRECTORY_ENTRIES = 500
_MAX_FILE_PREVIEW_BYTES = 1_500_000
_MAX_RETAINED_RUN_EVENTS = 512
_MAX_RETAINED_RUNS = 128
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
    "bailian": {
        "label": "Alibaba Cloud Bailian",
        "baseEnv": "DASHSCOPE_BASE_URL",
        "keyEnv": "DASHSCOPE_API_KEY",
        "defaultBaseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo"],
    },
    "custom": {
        "label": "Custom",
        "baseEnv": "CUSTOM_API_BASE_URL",
        "keyEnv": "CUSTOM_API_KEY",
        "defaultBaseUrl": "",
        "models": [],
    },
}


class SettingsUpdate(RuntimeSettingsUpdate):
    image_input_mode: Literal["auto", "supported", "unsupported"] | None = Field(default=None, alias="imageInputMode")
    web_agent_settings: WebAgentSettings = Field(default_factory=WebAgentSettings, alias="webAgentSettings")
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
    mode: Literal["local", "worktree"] = "local"
    request_id: str | None = Field(default=None, alias="requestId", min_length=1, max_length=128)
    include_ignored: list[str] = Field(default_factory=list, alias="includeIgnored", max_length=32)


class WorkspaceOpen(BaseModel):
    path: str = Field(min_length=1, max_length=4_096)


class WorkspacePick(BaseModel):
    initial_path: str | None = Field(default=None, alias="initialPath", max_length=4_096)


class NavigationPinUpdate(BaseModel):
    kind: Literal["workspace", "session"]
    target: str = Field(min_length=1, max_length=4_096)
    pinned: bool


class NavigationWorkspaceUpdate(BaseModel):
    target: str = Field(min_length=1, max_length=4_096)


class NavigationNameUpdate(BaseModel):
    kind: Literal["workspace", "session"]
    target: str = Field(min_length=1, max_length=4_096)
    name: str = Field(min_length=1, max_length=160)


class TaskFileSelection(BaseModel):
    model_config = {"extra": "forbid"}
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commands_trusted: StrictBool = Field(alias="commandsTrusted")


class TaskFilePreview(BaseModel):
    model_config = {"extra": "forbid"}
    workspace: str | None = Field(default=None, max_length=4096)
    session_id: str | None = Field(default=None, alias="sessionId", max_length=64)
    scope: Literal["project", "standalone"] = "project"
    path: str = Field(min_length=1, max_length=4096)


class RunCreate(BaseModel):
    model_config = {"extra": "forbid"}
    workspace: str | None = Field(default=None, max_length=4_096)
    prompt: str = Field(min_length=1, max_length=200_000)
    session_id: str | None = Field(default=None, alias="sessionId", max_length=64)
    scope: Literal["project", "standalone"] = "project"
    execution_profile: ExecutionProfile = Field(default="standard", alias="executionProfile")
    permission_mode: Literal["default", "bypass"] = Field(default="bypass", alias="permissionMode")
    execution_location: ExecutionLocation = Field(default="project", alias="executionLocation")
    task_file: TaskFileSelection | None = Field(default=None, alias="taskFile")
    continuation: dict[str, str] | None = None
    resume_queue_from: str | None = Field(default=None, alias="resumeQueueFrom", min_length=1, max_length=64)
    context_settings: ConversationContext | None = Field(default=None, alias="contextSettings")


class BudgetedRunCreate(RunCreate):
    run_budget: RunBudget = Field(alias="runBudget")


class ApprovalResponse(BaseModel):
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    decision: str = Field(min_length=1, max_length=64)
    feedback: str = Field(default="", max_length=8_000)


class QuestionResponse(BaseModel):
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    answer: str = Field(min_length=1, max_length=12_000)


class QuestionGroupResponse(BaseModel):
    answers: list[QuestionResponse] = Field(min_length=1, max_length=3)


class RunInputCreate(BaseModel):
    input_id: str = Field(alias="inputId", min_length=1, max_length=128)
    kind: Literal["steer", "supplement", "next_task"]
    text: str = Field(min_length=1, max_length=200_000)
    execution_profile: ExecutionProfile = Field(default="standard", alias="executionProfile")
    context_settings: ConversationContext | None = Field(default=None, alias="contextSettings")
    stage: bool = False


class AgentStopRequest(BaseModel):
    model_config = {"extra": "forbid"}
    execution_id: str = Field(alias="executionId", min_length=1, max_length=160)
    task_id: str = Field(alias="taskId", min_length=1, max_length=128)
    operation_id: str = Field(alias="operationId", min_length=1, max_length=128)


class ExtensionInstallRequest(BaseModel):
    source: str = Field(min_length=1, max_length=8_192)
    workspace: str | None = Field(default=None, max_length=4_096)
    context_scope: Literal["project", "standalone"] = Field(default="project", alias="contextScope")
    scope: Literal["project", "global"] = "project"
    trusted: bool = False
    granted_permissions: list[str] | None = Field(
        default=None,
        alias="grantedPermissions",
        max_length=128,
    )
    revision: str | None = Field(default=None, max_length=256)


class McpExtensionInstallRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    transport: Literal["streamable-http", "stdio"]
    workspace: str | None = Field(default=None, max_length=4_096)
    context_scope: Literal["project", "standalone"] = Field(default="project", alias="contextScope")
    scope: Literal["project", "global"] = "project"
    url: str = Field(default="", max_length=8_192)
    command: str = Field(default="", max_length=4_096)
    args: list[str] = Field(default_factory=list, max_length=128)


class ExtensionStateUpdate(BaseModel):
    workspace: str | None = Field(default=None, max_length=4_096)
    context_scope: Literal["project", "standalone"] = Field(default="project", alias="contextScope")
    enabled: bool | None = None
    trusted: bool | None = None
    granted_permissions: list[str] | None = Field(
        default=None,
        alias="grantedPermissions",
        max_length=128,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_payload(value: Any) -> Any:
    try:
        return redact_sensitive_payload(value)
    except Exception:  # noqa: BLE001
        if isinstance(value, dict):
            return {
                "kind": "error",
                "message": "Sensitive event omitted because redaction failed.",
            }
        return "[REDACTION_FAILED]"


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


def _navigation_names(state: dict[str, Any], key: str) -> dict[str, str]:
    names = state.get(key)
    if not isinstance(names, dict):
        return {}
    return {target: name for target, name in names.items()
            if isinstance(target, str) and isinstance(name, str) and name.strip()}


def _workspace_payload(path: Path, state: dict[str, Any] | None = None) -> dict[str, str]:
    if state is None:
        with _WEB_STATE_LOCK:
            state = _read_web_state_unlocked()
    return {
        "name": _navigation_names(state, "workspaceNames").get(_path_identity(path))
        or path.name or path.anchor or str(path),
        "path": str(path),
    }


def _record_recent_workspace(workspace: Path) -> list[dict[str, str]]:
    """Keep opened projects in recency order until the user closes them."""
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

        state["recentWorkspaces"] = recent
        state["lastWorkspace"] = str(workspace)
        _write_web_state_unlocked(state)
    return [_workspace_payload(Path(value), state) for value in recent]


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
        recent.append(_workspace_payload(candidate, state))
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

    # The standalone workspace stores standalone conversations; it is not a
    # project the rail may offer to open or close.
    standalone_identity = _path_identity(_standalone_workspace())
    recent = [
        item
        for item in _recent_workspaces(current)
        if _path_identity(Path(item["path"])) != standalone_identity
    ]
    known_workspace_paths = {item["path"] for item in recent}
    pinned_workspaces: list[dict[str, str]] = []
    for value in _clean_string_list(state.get("pinnedWorkspaces")):
        try:
            candidate = Path(value).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if not candidate.is_dir():
            continue
        payload = _workspace_payload(candidate, state)
        if _path_identity(candidate) == standalone_identity:
            continue
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
                navigation_state=state,
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


def _set_navigation_name(update: NavigationNameUpdate) -> None:
    name = update.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名称不能为空。")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise HTTPException(status_code=400, detail="名称不能包含换行或控制字符。")
    if update.kind == "workspace":
        target = _path_identity(_resolve_workspace(update.target))
        key = "workspaceNames"
    else:
        _require_unarchived_session(update.target)
        if session_store.load_session_metadata(update.target) is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        target = update.target
        key = "sessionNames"
    # Names are navigation preferences. Keeping them here avoids rewriting a
    # running conversation or moving the project directory it is bound to.
    with _WEB_STATE_LOCK:
        state = _read_web_state_unlocked()
        names = _navigation_names(state, key)
        names[target] = name
        state[key] = names
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
        names = _navigation_names(state, "sessionNames")
        names.pop(session_id, None)
        state["sessionNames"] = names
        _write_web_state_unlocked(state)


def _close_navigation_workspace(
    target: str, *, current: Path, fallback: Path
) -> tuple[Path, Path, list[dict[str, str]]]:
    """Remove one project from the rail list without touching its conversations.

    Closing is a navigation change: the workspace leaves the recent and pinned
    lists, and nothing on disk, in sessions or in the memory authority is
    deleted. It stays reusable through "open project" at any time. When the
    closed project is the active one, the navigation authority moves to the
    next listed project, then to the configured default workspace, and finally
    to the standalone workspace when the default is the closed project itself.
    """
    closed = _resolve_workspace(target)
    identity = _path_identity(closed)
    with _WEB_STATE_LOCK:
        state = _read_web_state_unlocked()
        raw_recent = [
            str(value)
            for value in state.get("recentWorkspaces", [])
            if isinstance(value, str)
        ]
        candidates: list[Path] = []
        seen: set[str] = set()
        for value in [str(closed), *raw_recent]:
            try:
                candidate = Path(value).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            candidate_identity = _path_identity(candidate)
            if candidate_identity in seen:
                continue
            seen.add(candidate_identity)
            candidates.append(candidate)
        remaining = [item for item in candidates if _path_identity(item) != identity]
        state["recentWorkspaces"] = [str(item) for item in remaining]
        state["pinnedWorkspaces"] = [
            value
            for value in _clean_string_list(state.get("pinnedWorkspaces"))
            if _path_identity(Path(value)) != identity
        ]
        next_workspace = current
        if identity == _path_identity(current):
            next_workspace = next(
                (item for item in remaining if item.is_dir()),
                fallback
                if fallback.is_dir() and _path_identity(fallback) != identity
                else _standalone_workspace(),
            )
            state["lastWorkspace"] = str(next_workspace)
        _write_web_state_unlocked(state)
    return closed, next_workspace, [_workspace_payload(item, state) for item in remaining]


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


def _provider_catalog(cwd: str | Path | None = None) -> dict[str, dict[str, Any]]:
    catalog = {name: dict(value) for name, value in _PROVIDERS.items()}
    if cwd is None:
        return catalog
    try:
        presets = extension_provider_presets(cwd)
    except (OSError, RuntimeError, ValueError):
        presets = {}
    for name, descriptor in presets.items():
        api_key = descriptor.get("apiKey")
        key_env = ""
        if isinstance(api_key, dict) and set(api_key) == {"secretRef"}:
            key_env = str(api_key.get("secretRef") or "").strip()
        catalog[name] = {
            "label": str(descriptor.get("label") or name),
            "baseEnv": str(descriptor.get("baseUrlEnv") or ""),
            "keyEnv": key_env,
            "defaultBaseUrl": str(descriptor.get("baseUrl") or ""),
            "models": [
                str(item)
                for item in descriptor.get("models", [])
                if str(item).strip()
            ] if isinstance(descriptor.get("models"), list) else [],
            "extension": True,
            "extensionId": str(descriptor.get("extensionId") or ""),
        }
    return catalog


def _context_runtime(cwd: str | Path | None, model: str = "default") -> dict[str, Any]:
    try:
        return load_runtime_config(cwd or Path.cwd())
    except (RuntimeError, ValueError):
        # Context selection remains available before provider credentials exist.
        effective = load_effective_settings(cwd)
        return {**effective, "model": os.environ.get("MINI_CODE_MODEL") or effective.get("model") or model}


def get_settings_view(cwd: str | Path | None = None) -> dict[str, Any]:
    from minicode.image_input import capability
    stored = _stored_settings()
    providers = _provider_catalog(cwd)
    provider = str(
        os.environ.get("MINI_CODE_PROVIDER", "")
        or stored.get("provider", "")
        or (stored.get("env", {}) or {}).get("MINI_CODE_PROVIDER", "")
        or "deepseek"
    ).strip().lower()
    if provider not in providers:
        provider = "custom"
    spec = providers[provider]
    model = str(
        os.environ.get("MINI_CODE_MODEL", "")
        or stored.get("model", "")
        or (stored.get("env", {}) or {}).get("MINI_CODE_MODEL", "")
        or (spec["models"][0] if spec["models"] else "")
    ).strip()
    root_base_key = {
        "deepseek": "deepseekBaseUrl",
        "bailian": "bailianBaseUrl",
        "openai": "openaiBaseUrl",
        "custom": "customBaseUrl",
    }.get(provider, "")
    root_key_key = {
        "deepseek": "deepseekApiKey",
        "bailian": "bailianApiKey",
        "openai": "openaiApiKey",
        "custom": "customApiKey",
    }.get(provider, "")
    base_url, base_source = _setting_with_source(
        stored,
        spec["baseEnv"],
        root_base_key,
        spec["defaultBaseUrl"],
    ) if spec["baseEnv"] else (spec["defaultBaseUrl"], "extension")
    api_key, key_source = (
        _setting_with_source(stored, spec["keyEnv"], root_key_key)
        if spec["keyEnv"]
        else ("", "none")
    )
    thinking, _ = _setting_with_source(stored, "DEEPSEEK_THINKING_MODE")
    effort, _ = _setting_with_source(stored, "DEEPSEEK_REASONING_EFFORT")
    execution_profile = str(
        os.environ.get("MINI_CODE_EXECUTION_PROFILE", "")
        or (stored.get("env", {}) or {}).get("MINI_CODE_EXECUTION_PROFILE", "")
        or stored.get("executionProfile", "")
        or stored.get("profile", "")
        or "standard"
    ).strip().lower()
    if execution_profile not in {"standard", "deep", "ultra"}:
        execution_profile = "standard"
    return {
        "provider": provider,
        "model": model,
        "baseUrl": base_url,
        "baseUrlSource": base_source,
        "connectionHistory": connection_history(
            [{"provider": provider, "model": model, "baseUrl": base_url}],
            stored.get("webConnectionHistory"),
        ),
        "imageInput": capability(provider, model, base_url, load_effective_settings(cwd)),
        "apiKeyConfigured": bool(api_key),
        "apiKeyMask": _mask_secret(api_key),
        "apiKeySource": key_source if api_key else "none",
        "balanceQuerySupported": provider == "deepseek" and is_official_deepseek_url(base_url),
        "thinkingMode": thinking or "enabled",
        "reasoningEffort": effort or "high",
        "executionProfile": execution_profile,
        **({"webAgentSettings": stored["webAgentSettings"]} if "webAgentSettings" in stored else {}),
        **runtime_settings_view(load_effective_settings(cwd)),
        "runBudgetSupported": True,
        "permissionModeSupported": True,
        "runUnlimitedBudgetSupported": True,
        "runContinuationSupported": True,
        "executionLocationSupported": True,
        "conversationContext": ConversationContext().view(_context_runtime(cwd, model)),
        "settingsPath": str(MINI_CODE_SETTINGS_PATH),
        "providers": [
            {
                "id": provider_id,
                "label": provider_spec["label"],
                "defaultBaseUrl": provider_spec["defaultBaseUrl"],
                "models": provider_spec["models"],
            }
            for provider_id, provider_spec in providers.items()
        ],
    }


def save_settings(update: SettingsUpdate, cwd: str | Path | None = None) -> dict[str, Any]:
    provider = update.provider.strip().lower()
    providers = _provider_catalog(cwd)
    if provider not in providers:
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

    spec = providers[provider]
    env_updates: dict[str, str] = {
        "MINI_CODE_PROVIDER": provider,
        "MINI_CODE_MODEL": model,
    }
    if spec["baseEnv"]:
        env_updates[spec["baseEnv"]] = base_url
    if provider == "deepseek":
        env_updates["DEEPSEEK_MODEL"] = model
        env_updates["DEEPSEEK_THINKING_MODE"] = update.thinking_mode or "enabled"
        if "reasoning_effort" in update.model_fields_set:
            env_updates["DEEPSEEK_REASONING_EFFORT"] = update.reasoning_effort or "high"
    if update.clear_api_key and spec["keyEnv"]:
        env_updates[spec["keyEnv"]] = ""
    elif update.api_key is not None and spec["keyEnv"]:
        key_value = update.api_key.get_secret_value().strip()
        if key_value:
            env_updates[spec["keyEnv"]] = key_value

    runtime_updates = runtime_settings_updates(update)
    if update.image_input_mode is not None:
        from minicode.image_input import route_key
        runtime_updates["imageInputOverrides"] = {
            route_key(provider, model, base_url): update.image_input_mode,
        }
    if "web_agent_settings" in update.model_fields_set:
        runtime_updates["webAgentSettings"] = update.web_agent_settings.model_dump(by_alias=True)
    effective = load_effective_settings(cwd)
    if "runtimeModelPricing" in runtime_updates:
        runtime_updates["runtimeModelPricing"] = {
            **(effective.get("runtimeModelPricing") or {}),
            **runtime_updates["runtimeModelPricing"],
        }
    max_cost = runtime_updates.get("runtimeMaxCost", effective.get("runtimeMaxCost"))
    pricing_key = f"{provider}:{model}"
    pricing = runtime_updates.get("runtimeModelPricing", {}).get(
        pricing_key, (effective.get("runtimeModelPricing") or {}).get(pricing_key),
    )
    if max_cost is not None and not pricing:
        raise HTTPException(status_code=400, detail="设置费用上限时，请填写当前提供商和模型的全部四项单价。单价为 0 时请明确填写 0。")
    if max_cost is not None:
        try:
            RuntimeModelPricing.model_validate(pricing)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail="当前模型计价无效，请填写全部四项非负单价。") from exc
    # Include the previous connection to preserve settings created before history existed.
    previous = get_settings_view(cwd)
    history = connection_history(
        [{"provider": provider, "model": model, "baseUrl": base_url}],
        previous["connectionHistory"],
    )
    save_mini_code_settings({
        "provider": provider, "model": model, "env": env_updates,
        **runtime_updates, "webConnectionHistory": history,
    })
    try:
        if os.name != "nt":
            MINI_CODE_SETTINGS_PATH.chmod(0o600)
    except OSError:
        pass
    return get_settings_view(cwd)


def get_saved_provider_balance() -> dict[str, Any]:
    stored = _stored_settings()
    provider = str(
        os.environ.get("MINI_CODE_PROVIDER", "") or stored.get("provider", "")
        or (stored.get("env", {}) or {}).get("MINI_CODE_PROVIDER", "") or "deepseek"
    ).strip().lower()
    if provider != "deepseek":
        raise HTTPException(status_code=400, detail="当前仅支持查询 DeepSeek 官方账户余额。")
    base_url, _ = _setting_with_source(stored, "DEEPSEEK_BASE_URL", "deepseekBaseUrl", "https://api.deepseek.com")
    if not is_official_deepseek_url(base_url):
        raise HTTPException(status_code=400, detail="余额查询仅支持 DeepSeek 官方地址 https://api.deepseek.com。")
    api_key, _ = _setting_with_source(stored, "DEEPSEEK_API_KEY", "deepseekApiKey")
    if not api_key:
        raise HTTPException(status_code=400, detail="请先保存 DeepSeek API Key，再查询余额。")
    try:
        return query_deepseek_balance(api_key)
    except BalanceQueryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


def _models_endpoint(provider: str, base_url: str) -> str:
    base = base_url.rstrip("/")
    base = base.removesuffix("/chat/completions")
    if provider == "deepseek":
        return f"{base}/models"
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def test_saved_provider(cwd: str | Path | None = None) -> dict[str, Any]:
    if os.environ.get("MINI_CODE_WEB_FORCE_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}:
        return {"ok": True, "latencyMs": 1, "message": "模拟运行模式已就绪。"}
    try:
        runtime = load_runtime_config(cwd or Path.cwd())
    except Exception as exc:
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
                    "bailian": "bailianApiKey",
                    "openai": "openaiApiKey",
                    "openrouter": "openrouterApiKey",
                    "custom": "customApiKey",
                }.get(provider, "openaiApiKey")
                base_name = {
                    "deepseek": "deepseekBaseUrl",
                    "bailian": "bailianBaseUrl",
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
        "message": f"{_provider_catalog(cwd).get(str(runtime.get('_extensionProvider') or provider), {}).get('label', provider)} 连接成功。",
    }


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    workspace: str
    prompt_preview: str
    execution_profile: ExecutionProfile
    requires_workspace_write: bool = True
    status: RunStatus = "queued"
    created_at: int = field(default_factory=_now_ms)
    updated_at: int = field(default_factory=_now_ms)
    process: subprocess.Popen[str] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)
    saw_done: bool = False
    stdout_thread: threading.Thread | None = field(default=None, repr=False)
    stderr_thread: threading.Thread | None = field(default=None, repr=False)
    sequence: int = 0
    events_discarded_through: int = 0
    condition: threading.Condition = field(default_factory=threading.Condition, repr=False)
    write_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    input_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    accepting_inputs: bool = True
    accepting_steering: bool = True
    input_protocol: int = 1
    queue_protocol: int = 0
    queue_resumed_run_id: str | None = None
    control_responses: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    control_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    execution: dict[str, Any] | None = None
    public_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    waiting_models: dict[str, str] = field(default_factory=dict)
    pending_approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_questions: dict[str, dict[str, Any]] = field(default_factory=dict)
    question_answers: dict[str, str] = field(default_factory=dict, repr=False)
    history_sequence: int = 0
    history_count: int = 0
    task_file: dict[str, Any] | None = None
    run_budget: dict[str, Any] | None = None
    file_changes: dict[str, Any] | None = None
    context_settings: dict[str, Any] | None = None
    execution_location: ExecutionLocation | None = None
    permission_mode: Literal["default", "bypass"] = "default"
    prompt: str = field(default="", repr=False)
    restored: bool = False
    recovery_pending: bool = False
    review_pending: bool = False
    review_closeout: str = ""
    history_start_count: int = 0
    event_journal: RunEventJournal | None = field(default=None, repr=False)
    event_history_error: str | None = None
    _history_warning_sent: bool = field(default=False, repr=False)

    def restore_event_history(self) -> None:
        try:
            self._restore_event_history()
        except (OSError, ValueError, sqlite3.Error) as error:
            self.event_history_error = type(error).__name__
        if self.event_history_error:
            self.event_journal = None

    def _restore_event_history(self) -> None:
        journal = RunEventJournal(self.session_id, self.run_id)
        if not journal.exists():
            return
        self.event_journal = journal
        cursor = 0
        while page := journal.page(cursor):
            for payload in page:
                self._remember_event(payload, restoring=True)
                self.sequence = int(payload["sequence"])
                self.events.append(payload)
            cursor = self.sequence
            self._trim_events()

    def _trim_events(self) -> None:
        excess = len(self.events) - _MAX_RETAINED_RUN_EVENTS
        if excess > 0:
            self.events_discarded_through = int(self.events[excess - 1]["sequence"])
            del self.events[:excess]

    def _remember_event(self, payload: dict[str, Any], *, restoring: bool = False) -> None:
        if payload.get("kind") == "run_prompt":
            self.history_start_count = int(payload.get("historyCount", 0))
        if payload.get("kind") == "history_saved":
            self.history_sequence = int(payload["sequence"])
            self.history_count = int(payload.get("messageCount", 0))
        if restoring and payload.get("kind") == "file_changes" and isinstance(payload.get("fileChanges"), dict):
            self.file_changes = payload["fileChanges"]
        if payload.get("kind") == "execution_snapshot":
            self.execution = payload.get("execution")
        if payload.get("kind") == "model_status":
            model_key = f"{payload.get('executionId', '')}:{payload.get('nodeId', '')}:{payload.get('attemptId', '')}"
            if payload.get("modelState") == "waiting":
                self.waiting_models[model_key] = str(payload.get("nodeId", ""))
            else:
                self.waiting_models.pop(model_key, None)
        if payload.get("kind") == "approval_requested":
            self.pending_approvals[str(payload["requestId"])] = payload
        elif payload.get("kind") == "approval_resolved":
            self.pending_approvals.pop(str(payload["requestId"]), None)
        if payload.get("kind") == "question_requested":
            self.pending_questions[str(payload["requestId"])] = payload
        elif payload.get("kind") == "question_resolved":
            self.pending_questions.pop(str(payload["requestId"]), None)
            if payload.get("resolution") == "answered" and isinstance(payload.get("answer"), str):
                self.question_answers.setdefault(str(payload["requestId"]), payload["answer"])
        if payload.get("kind") == "run_input" and isinstance(payload.get("runInput"), dict):
            item = {**payload["runInput"], "updatedAt": payload["timestamp"]}
            self.public_inputs[str(item["inputId"])] = item
        self.updated_at = max(self.updated_at, int(payload["timestamp"]))
        if payload.get("kind") == "done":
            self.saw_done = True
            self.review_pending = payload.get("stopReason") == "review_pending"
        elif payload.get("kind") == "error":
            self.review_pending = False

    def add_event(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.condition:
            payload = dict(_safe_payload(event))
            if payload.get("kind") == "file_changes" and isinstance(payload.get("fileChanges"), dict):
                self.file_changes = payload["fileChanges"]
                # Diffs are fetched on demand; SSE retention keeps only metadata.
                payload["fileChanges"] = change_summary(self.file_changes)
            payload["sequence"] = self.sequence + 1
            payload["webRunId"] = self.run_id
            payload["timestamp"] = _now_ms()
            if self.event_journal is not None:
                try:
                    self.event_journal.append(payload)
                except (OSError, ValueError, sqlite3.Error) as error:
                    # This journal is a display projection. Queue receipts and
                    # Runtime checkpoints have their own authoritative stores.
                    # Keep the original event and drain the worker even when
                    # display history is unavailable.
                    self.event_history_error = type(error).__name__
                    self.event_journal = None
                    payload["sequence"] = self.sequence + 1
            self.sequence = int(payload["sequence"])
            self._remember_event(payload)
            self.events.append(payload)
            self._trim_events()
            self.condition.notify_all()
            if self.event_history_error and not self._history_warning_sent:
                self._history_warning_sent = True
                message = "部分实时日志未能保存，可能无法完整回放；已保存的对话和执行记录仍保留。"
                if self.status not in _TERMINAL_STATUSES:
                    message += "任务继续执行。"
                self.add_event({"kind": "runtime", "content": message,
                    "runtime": {"category": "storage_warning", "message": message}})
            return payload

    def public(self) -> dict[str, Any]:
        from minicode.run_images import RunImageStore
        images = RunImageStore(self.session_id).records(
            web_run_id=self.run_id, turn_id=(self.file_changes or {}).get("turnId", ""))
        # One Web worker can execute several queued human turns. Historical
        # turns restore their own attachments through the conversation view.
        if images and not (self.file_changes or {}).get("turnId"):
            images = [image for image in images if image.get("turnId") == images[-1].get("turnId")]
        return {
            "runId": self.run_id,
            "images": images,
            "restored": self.restored,
            "sessionId": self.session_id,
            "workspace": self.workspace,
            "promptPreview": self.prompt_preview,
            "executionProfile": self.execution_profile,
            "permissionMode": self.permission_mode,
            "executionLocation": self.execution_location,
            "taskFile": self.task_file,
            "runBudget": self.run_budget,
            "contextSettings": self.context_settings,
            "requiresWorkspaceWrite": self.requires_workspace_write,
            "status": self.status,
            "reviewPending": self.status == "failed" and self.review_pending,
            "reviewCloseout": self.review_closeout if self.status == "failed" and self.review_pending else "",
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "eventCount": self.sequence,
            "retainedEventCount": len(self.events),
            "eventsDiscardedThrough": self.events_discarded_through,
            "eventHistoryAvailable": self.event_journal is not None,
            "eventHistoryError": self.event_history_error,
            "execution": self.execution,
            "inputs": list(self.public_inputs.values()),
            "acceptingInputs": self.accepting_inputs and self.status in {"queued", "running"},
            "queueProtocol": self.queue_protocol,
            "acceptingSteering": self.input_protocol >= 2 and self.accepting_steering and self.status in {"queued", "running"},
            "inputProtocol": self.input_protocol,
            "waitingModels": list(self.waiting_models),
            "pendingApprovals": list(self.pending_approvals.values()),
            "pendingQuestions": list(self.pending_questions.values()) if self.status == "running" else [],
            "historySequence": self.history_sequence,
            "historyCount": self.history_count,
            "fileChanges": change_summary(self.file_changes),
        }

    def _events_after_locked(self, after: int) -> list[dict[str, Any]]:
        cursor = max(0, after)
        if self.event_journal is not None:
            try:
                return [project_change_event(event) for event in
                        self.event_journal.page(cursor, through=self.sequence, limit=_MAX_RETAINED_RUN_EVENTS)]
            except (OSError, ValueError, sqlite3.Error) as error:
                self.event_history_error = type(error).__name__
                self.event_journal = None
                self.add_event({"kind": "progress", "content": "历史日志暂不可读，正在显示仍保留的实时记录。"})
        result: list[dict[str, Any]] = []
        if cursor < self.events_discarded_through:
            cursor = self.events_discarded_through
            result.append(
                {
                    "kind": "event_gap",
                    "sequence": cursor,
                    "timestamp": _now_ms(),
                    "message": (
                        "这次旧任务未保存较早的实时日志。"
                        "已保存的对话和工具记录可在工作过程中查看。"
                    ),
                }
            )
        result.extend(project_change_event(event) for event in self.events if int(event["sequence"]) > cursor)
        return result

    def snapshot(self, *, after: int = 0) -> dict[str, Any]:
        """Return one consistent browser-safe view of the run and its events."""

        with self.condition:
            events = list(self._events_after_locked(after))
            return {
                "run": self.public(),
                "events": events,
                "hasMoreEvents": bool(events and int(events[-1]["sequence"]) < self.sequence),
            }


class RunManager:
    def __init__(self, max_active: int = 4, *, web_runtime: str = "legacy", web_ultra_runtime: str = "legacy",
                 web_agent_teams: bool = False, web_agent_team_writes: bool = False, web_primary_role: str = "worker",
                 services=None) -> None:
        from minicode.web_runtime_settings import WebRuntimePolicy
        self.web_runtime_policy = WebRuntimePolicy(orchestration=web_runtime, ultra=web_ultra_runtime,
            agent_teams=web_agent_teams, agent_team_writes=web_agent_team_writes, primary_role=web_primary_role)
        self.max_active = max_active
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.RLock()
        from minicode.development_services import DevelopmentServiceManager
        from minicode.web_services import WebServiceBridge
        self.services = services if services is not None else DevelopmentServiceManager()
        self.service_bridge = WebServiceBridge(self.services)

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.recovery_pending or (record.restored and record.status not in _TERMINAL_STATUSES):
                from minicode.web_run_inputs import find_inputs
                try:
                    saved = find_inputs(run_id)
                    if saved is not None:
                        _require_unarchived_session(saved["sessionId"])
                        record = self._restore_record(saved)
                except (OSError, ValueError, TypeError) as error:
                    raise HTTPException(500, "无法读取任务恢复记录，请检查会话存储。") from error
            if record is None:
                raise HTTPException(status_code=404, detail="任务不存在。")
        _require_unarchived_session(record.session_id)
        return record

    def _restore_record(self, saved: dict) -> RunRecord:
        from contextlib import ExitStack
        from minicode.run_ownership import RuntimeRunOwnerLease, RunStateBusyError
        from minicode.workspace_lock import WorkspaceBusyError, WorkspaceWriteLease
        from minicode.web_run_inputs import ledger_path, read_inputs, restored_status
        from minicode.web_candidate_recovery import WebCandidateRecovery
        session_id, run_id = saved["sessionId"], saved["runId"]
        session = None
        status = "stopping"
        try:
            with ExitStack() as ownership:
                ownership.enter_context(RuntimeRunOwnerLease(ledger_path(session_id, run_id), run_id=run_id))
                saved = read_inputs(session_id, run_id) or saved
                session = load_session(session_id)
                recovering = bool(session and any(isinstance(trace, dict) and trace.get("recovery_pending")
                    and trace.get("web_run_id") == run_id for trace in session.runtime_runs))
                if (session is not None and not recovering and not saved.get("prompt")
                    and saved["status"] not in _TERMINAL_STATUSES
                    and session.metadata.status in {"queued", "running", "stopping"}):
                    # Pre-receipt writers still hold the native workspace lease.
                    # Absence from this host's memory is not proof of their exit.
                    ownership.enter_context(WorkspaceWriteLease(session.workspace, run_id="recover-" + run_id))
                if session is not None:
                    WebCandidateRecovery().reconcile_interrupted(session, web_run_id=run_id)
                status = restored_status(saved, session)
                # Only a matching latest run may change the session's status.
                from minicode.web_run_inputs import latest_inputs
                latest = latest_inputs(session_id)
                if (session is not None and (latest is None or latest["runId"] == run_id)
                    and (recovering or session.metadata.status in {"queued", "running", "stopping"}
                         or saved.get("prompt") and int(session.metadata.updated_at * 1000) <= saved["createdAt"])):
                    if saved.get("prompt") and int(session.metadata.updated_at * 1000) <= saved["createdAt"]:
                        from minicode.session_history import begin_turn, finish_turn
                        turn = begin_turn(session, saved["prompt"])
                        message = {"role": "user", "content": saved["prompt"]}
                        session.messages.append(message)
                        finish_turn(session, turn, status="stopped", display_messages=[message])
                    session.metadata.status = status
                    save_session(session, force_full=True)
                # Fence a worker that has not reached its ownership gate yet.
                if saved.get("prompt") and saved["status"] not in _TERMINAL_STATUSES:
                    saved["status"] = status
                    from minicode.session_contract import atomic_write_json
                    atomic_write_json(ledger_path(session_id, run_id), saved)
        except (RunStateBusyError, WorkspaceBusyError):
            # The orphan observes stdin EOF and cancels itself. Until its OS
            # lease is released this host must neither resume nor overwrite it.
            status = "stopping"
        record = RunRecord(run_id=run_id, session_id=session_id, workspace=saved["workspace"],
            prompt_preview=saved["promptPreview"], prompt=saved.get("prompt", ""),
            execution_profile=saved["executionProfile"], permission_mode=saved.get("permissionMode", "default"),
            execution_location=saved.get("executionLocation"), task_file=saved.get("taskFile"),
            run_budget=saved.get("runBudget"), context_settings=saved.get("contextSettings"),
            requires_workspace_write=saved.get("requiresWorkspaceWrite", True),
            created_at=saved["createdAt"], updated_at=saved["updatedAt"], status=status,
            input_protocol=2, queue_protocol=saved.get("queueProtocol", 0),
            queue_resumed_run_id=saved.get("queueResumedRunId"), restored=True,
            accepting_inputs=False, accepting_steering=False, inputs=deepcopy(saved["inputs"]),
            event_history_error=saved.get("eventHistoryError"))
        record.restore_event_history()
        if status == "failed" and session is not None:
            from minicode.runtime_delivery import review_pending_reason, review_pending_closeout, review_pending_error_explained
            last_trace = next((trace for trace in reversed(session.runtime_runs)
                if isinstance(trace, dict) and trace.get("web_run_id") == run_id), None)
            record.review_pending = (bool(review_pending_reason(last_trace))
                and not any(event.get("kind") == "error" and not review_pending_error_explained(last_trace, event.get("message"))
                    for event in record.events))
            record.review_closeout = review_pending_closeout(last_trace) if record.review_pending else ""
        record.sequence = max(record.sequence, saved.get("eventSequence", 0))
        for item in record.inputs.values():
            if status in _TERMINAL_STATUSES and item["status"] in {"accepted", "starting", "applied"}:
                item.update(status="held", reason="服务重启后未确认处理结果；可撤回到草稿重新发送。")
            record.add_event({"kind": "run_input", "runInput": dict(item)})
        if session is not None and status in _TERMINAL_STATUSES:
            record.add_event({"kind": "history_saved", "messageCount": len(session.messages)})
        record.add_event({"kind": "status", "status": status, "content":
            "旧执行正在退出，请稍候。" if status == "stopping" else "已从任务记录恢复；可检查保留进度后继续。"})
        self._runs[run_id] = record
        return record

    def latest_for_session(self, session_id: str) -> RunRecord | None:
        """Return the newest in-memory run for a session, including terminal runs."""

        with self._lock:
            matches = [record for record in self._runs.values() if record.session_id == session_id]
            if matches:
                return self.get(max(matches, key=lambda record: (record.created_at, record.run_id)).run_id)
            from minicode.web_run_inputs import latest_inputs
            try:
                saved = latest_inputs(session_id)
                if saved is None:
                    session = load_session(session_id)
                    if session is None or session.metadata.status not in {"queued", "running", "stopping"}:
                        return None
                    # Compatibility with tasks created before startup receipts.
                    legacy = RunRecord("interrupted-" + session_id, session_id, session.workspace,
                        "中断的历史任务", "standard", status="running")
                    from minicode.web_run_inputs import persist_inputs
                    persist_inputs(legacy)
                    saved = latest_inputs(session_id)
                return self._restore_record(saved)
            except (OSError, ValueError, TypeError) as error:
                raise HTTPException(500, "无法读取任务恢复记录，请检查会话存储。") from error

    def overlay_navigation_statuses(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Keep the task rail authoritative while worker metadata is catching up."""

        sessions = payload.get("sessions", [])
        if not isinstance(sessions, list):
            return payload
        with self._lock:
            latest_by_session: dict[str, RunRecord] = {}
            for record in self._runs.values():
                previous = latest_by_session.get(record.session_id)
                if previous is None or (record.created_at, record.run_id) > (
                    previous.created_at,
                    previous.run_id,
                ):
                    latest_by_session[record.session_id] = record
        for item in sessions:
            if not isinstance(item, dict):
                continue
            active_record = latest_by_session.get(str(item.get("sessionId", "")))
            if active_record is None and item.get("status") in {"queued", "running", "stopping"}:
                active_record = self.latest_for_session(str(item.get("sessionId", "")))
            if active_record is None:
                continue
            item["status"] = active_record.status
            item["reviewPending"] = active_record.status == "failed" and active_record.review_pending
            item["updatedAt"] = max(
                int(item.get("updatedAt", 0)), active_record.updated_at
            )
        return payload

    def _active_count(self) -> int:
        return sum(1 for run in self._runs.values() if run.status not in _TERMINAL_STATUSES)

    def _prune_terminal_runs(self) -> None:
        excess = len(self._runs) - _MAX_RETAINED_RUNS + 1
        if excess <= 0:
            return
        terminal = sorted(
            (
                record
                for record in self._runs.values()
                if record.status in _TERMINAL_STATUSES
            ),
            key=lambda record: (record.updated_at, record.run_id),
        )
        for record in terminal[:excess]:
            self._runs.pop(record.run_id, None)

    def start(
        self,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        execution_profile: ExecutionProfile = "standard",
        task: HeadlessTask | None = None,
        run_budget: RunBudget | None = None,
        continuation: dict[str, str] | None = None,
        context_settings: ConversationContext | None = None,
        execution_location: ExecutionLocation | None = "project",
        permission_mode: Literal["default", "bypass"] = "bypass",
        queued_inputs: list[dict[str, Any]] | None = None,
        queue_initial: bool = False,
        queue_source: RunRecord | None = None,
    ) -> RunRecord:
        if permission_mode not in {"default", "bypass"}:
            raise HTTPException(status_code=400, detail="权限模式只能是默认审批或完全访问。")
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise HTTPException(status_code=400, detail="请输入任务内容。")
        try:
            from minicode.image_input import preflight
            preflight(clean_prompt, _context_runtime(workspace))
            previous_context = load_context_settings(session_id) if session_id else ConversationContext()
            context_settings = context_settings.merge(previous_context) if context_settings is not None else previous_context
            context_settings.validate_capacity(_context_runtime(workspace))
        except (ValueError, OSError) as error:
            raise HTTPException(400, "无法应用对话上下文设置。" if isinstance(error, OSError) else str(error)) from error
        # Capture once before creating a session/process. Saving settings later
        # affects the next submission, never this run or a running child.
        policy = self.web_runtime_policy
        if execution_profile == "ultra" and policy.applies(execution_profile):
            try:
                policy = policy.for_settings(_stored_settings())
            except ValidationError as exc:
                raise HTTPException(status_code=400, detail="多 Agent 设置无效，请在设置中重新保存。") from exc
        if continuation is not None:
            execution_location = "isolated"
        try:
            if execution_location is not None:
                apply_execution_location(policy.apply({"executionProfile": execution_profile}), execution_location)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with self._lock:
            if queue_source is None and session_id and queued_inputs is None:
                previous = self.latest_for_session(session_id)
                if previous is not None and previous.status in _TERMINAL_STATUSES and previous.queue_protocol:
                    with previous.input_lock:
                        remaining = [dict(item) for item in previous.inputs.values()
                                     if item["kind"] != "steer" and item["status"] in {"accepted", "held"}]
                        if remaining and not previous.queue_resumed_run_id:
                            queue_source, queued_inputs = previous, remaining
            self._prune_terminal_runs()
            if self._active_count() >= self.max_active:
                raise HTTPException(status_code=429, detail="同时运行的任务过多，请稍后再试。")
            write_required = continuation is not None or permission_mode == "bypass" or execution_profile == "standard" or requires_workspace_write(
                clean_prompt,
                {**(task.fields() if task else {}), "runtimeOrchestration":
                    "primary" if policy.applies(execution_profile) else "legacy"},
            )
            normalized_workspace = normalize_workspace_path(workspace)
            if write_required and any(
                run.status not in _TERMINAL_STATUSES
                and run.requires_workspace_write
                and normalize_workspace_path(run.workspace) == normalized_workspace
                for run in self._runs.values()
            ):
                raise HTTPException(
                    status_code=409,
                    detail="该工作区已有写入任务正在运行。",
                )
            if session_id:
                _require_unarchived_session(session_id)
                session = load_session(session_id)
                if session is None:
                    raise HTTPException(status_code=404, detail="会话不存在。")
                if Path(session.workspace).resolve() != workspace:
                    raise HTTPException(status_code=400, detail="会话不属于当前工作区。")
                if session.binding.get("worktree_id"):
                    from minicode.git_workspaces import GitWorktreeService
                    GitWorktreeService().get(session.binding["worktree_id"], require_ready=True)
                if any(
                    run.session_id == session_id and run.status not in _TERMINAL_STATUSES
                    for run in self._runs.values()
                ):
                    raise HTTPException(status_code=409, detail="该会话已有任务正在运行。")
            else:
                session = create_new_session(str(workspace))
                from minicode.session_history import project_binding
                session.binding = project_binding(str(workspace))
                save_session(session, force_full=True)
            save_context_settings(session.session_id, context_settings)
            run_id = uuid.uuid4().hex[:16]
            record = RunRecord(
                run_id=run_id,
                session_id=session.session_id,
                workspace=str(workspace),
                prompt_preview=clean_prompt[:120],
                prompt=clean_prompt,
                execution_profile=execution_profile,
                permission_mode=permission_mode,
                execution_location=execution_location,
                requires_workspace_write=write_required,
                task_file=task.summary() if task else None,
                run_budget=run_budget.request() if run_budget else None,
                context_settings=context_settings.request(),
                accepting_inputs=task is None and run_budget is None and not self.web_runtime_policy.applies(execution_profile),
                input_protocol=2,
                queue_protocol=1,
                history_start_count=len(session.messages),
            )
            if queued_inputs:
                record.inputs = {item["inputId"]: {**deepcopy(item),
                    "status": "applied" if queue_initial and index == 0 else "accepted",
                    "version": item.get("version", 0) + 1,
                    "queueMode": "automatic" if record.accepting_inputs and not policy.applies(item["executionProfile"]) else "manual"}
                    for index, item in enumerate(queued_inputs)}
            from minicode.web_run_inputs import persist_inputs
            try:
                persist_inputs(record)
            except (OSError, ValueError, sqlite3.Error) as error:
                raise HTTPException(500, "无法保存任务启动记录；任务尚未启动，原队列仍保留。") from error
            try:
                record.event_journal = RunEventJournal(record.session_id, record.run_id, create=True)
            except (OSError, ValueError, sqlite3.Error) as error:
                record.event_history_error = type(error).__name__
            record.add_event({"kind": "run_prompt", "content": clean_prompt,
                              "historyCount": record.history_start_count})
            if queued_inputs:
                for item in record.inputs.values():
                    record.add_event({"kind": "run_input", "runInput": dict(item)})
            self._runs[run_id] = record
            if queue_source is not None:
                with queue_source.input_lock:
                    queue_source.queue_resumed_run_id = run_id
                    try:
                        persist_inputs(queue_source)
                    except (OSError, ValueError):
                        record.add_event({"kind": "error", "message": "队列已保留到新任务，但旧队列的跳转记录保存失败。"})

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
            if queued_inputs:
                with record.input_lock:
                    for item in record.inputs.values():
                        item["status"] = "held"
                        record.add_event({"kind": "run_input", "runInput": dict(item)})
            persist_inputs(record)
            record.add_event({"kind": "error", "message": f"无法启动任务进程: {exc}"})
            raise HTTPException(status_code=500, detail="无法启动任务进程。") from exc

        record.process = process
        record.status = "running"
        record.add_event({"kind": "status", "status": "running", "content": "任务已启动"})
        record.stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(record,),
            daemon=True,
        )
        record.stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(record,),
            daemon=True,
        )
        record.stdout_thread.start()
        record.stderr_thread.start()
        threading.Thread(target=self._wait_for_exit, args=(record,), daemon=True).start()
        self._write(
            record,
            {
                "kind": "start",
                "runId": record.run_id,
                "prompt": clean_prompt,
                "executionProfile": execution_profile,
                "permissionMode": permission_mode,
                "executionLocation": execution_location,
                "contextSettings": context_settings.request(),
                "inputProtocol": 2,
                "questionProtocol": 1,
                **({"initialInput": dict(next(iter(record.inputs.values())))} if queue_initial and queued_inputs else {}),
                **({"webRuntimePolicy": policy.model_dump()}
                   if policy.applies(execution_profile) else {}),
                **({"taskSnapshot": task_packet(task)} if task else {}),
                **({"runBudget": run_budget.request()} if run_budget else {}),
                **({"continuation": continuation} if continuation else {}),
            },
        )
        return record

    def resume_queue(self, source_id: str, workspace: Path, session_id: str | None, **settings) -> RunRecord:
        """Start the first held message with a fresh budget and carry the rest forward."""
        source = self.get(source_id)
        with self._lock, source.input_lock:
            if source.session_id != session_id or Path(source.workspace).resolve() != workspace:
                raise HTTPException(409, "队列不属于当前会话。")
            if source.queue_resumed_run_id:
                existing = self._runs.get(source.queue_resumed_run_id)
                if existing is not None:
                    return existing
                raise HTTPException(409, "这份队列已经继续，请刷新会话查看结果。")
            if source.status not in _TERMINAL_STATUSES:
                raise HTTPException(409, "请等待当前任务结束后继续队列。")
            items = [dict(item) for item in source.inputs.values()
                     if item["kind"] != "steer" and item["status"] in {"accepted", "held"}]
            if not items:
                raise HTTPException(409, "队列中没有待发送的消息。")
            first = items[0]
            settings["execution_profile"] = first["executionProfile"]
            if first.get("contextSettings"):
                settings["context_settings"] = ConversationContext.model_validate(first["contextSettings"])
            return self.start(workspace, first["text"], session_id, queued_inputs=items,
                              queue_initial=True, queue_source=source, **settings)

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
                if payload.get("kind") in {"service_request", "service_ack", "service_cancel"}:
                    self.service_bridge.dispatch(record, payload, self._write)
                    continue
                if payload.get("kind") == "user_input_receipt":
                    with record.condition:
                        record.control_responses[str(payload.get("requestId", ""))] = payload
                        record.condition.notify_all()
                    continue
                if payload.get("kind") == "user_input_checkpoint":
                    self._user_input_checkpoint(record, payload)
                    continue
                if payload.get("kind") == "input_capabilities" and payload.get("runId") == record.run_id:
                    with record.input_lock:
                        record.accepting_steering = bool(payload.get("acceptingSteering")) and record.status != "stopping"
                if payload.get("kind") == "input_checkpoint":
                    # Serialize acceptance with the worker's final checkpoint.
                    # An HTTP 200 can never race past the last input drain.
                    with record.input_lock:
                        item = next((item for item in record.inputs.values()
                                     if item["status"] == "accepted" and item["kind"] != "steer"), None)
                        if record.status == "stopping" or (item and item.get("queueMode") == "manual"):
                            item = None
                        if item is None:
                            record.accepting_inputs = False
                        else:
                            item["status"] = "starting"
                        try:
                            if item is not None:
                                if record.queue_protocol:
                                    from minicode.web_run_inputs import persist_inputs
                                    persist_inputs(record)
                                record.add_event({"kind": "run_input", "runInput": dict(item)})
                            self._write(record, {"kind": "input_checkpoint_response",
                                        "requestId": payload["requestId"], "input": item})
                        except (HTTPException, OSError, ValueError):
                            record.accepting_inputs = False
                            # Wake the worker even when persisting the drain failed.
                            try:
                                self._write(record, {"kind": "input_checkpoint_response",
                                    "requestId": payload["requestId"], "input": None})
                            except HTTPException:
                                pass
                    continue
                if payload.get("kind") == "run_input":
                    with record.input_lock:
                        item = payload.get("runInput", {})
                        if item.get("inputId") in record.inputs:
                            existing = record.inputs[item["inputId"]]
                            if existing["status"] in {"completed", "canceled"} and item.get("status") in {"accepted", "starting", "applied"}:
                                continue
                            existing.update(item)
                            # Native steering receipts carry runtime identity, not
                            # the host queue version. Preserve both in SSE/HTTP.
                            payload["runInput"] = dict(existing)
                            if existing["kind"] == "steer" or record.queue_protocol:
                                try:
                                    from minicode.web_run_inputs import persist_inputs
                                    persist_inputs(record)
                                except (OSError, ValueError) as error:
                                    record.accepting_steering = False
                                    record.add_event({"kind": "error", "message": "补充消息状态保存失败：" + str(error)})
                record.add_event(payload)

    def _user_input_checkpoint(self, record: RunRecord, payload: dict) -> None:
        from minicode.web_run_inputs import persist_inputs
        with record.input_lock:
            if payload.get("runId") != record.run_id:
                return
            pending = [dict(item) for item in record.inputs.values()
                       if item["kind"] == "steer" and item["status"] == "accepted"]
            if record.status == "stopping":
                pending = []
            closed = bool(payload.get("final")) and not pending
            proposed = {key: dict(item) for key, item in record.inputs.items()}
            for item in pending:
                proposed[item["inputId"]]["status"] = "starting"
            try:
                if pending or closed:
                    persist_inputs(record, inputs=proposed,
                        accepting_steering=False if closed else record.accepting_steering)
                record.inputs = proposed
                if closed:
                    record.accepting_steering = False
                    record.add_event({"kind": "input_capabilities", "acceptingSteering": False})
                self._write(record, {"kind": "user_input_checkpoint_response",
                    "requestId": payload["requestId"], "inputs": pending, "closed": closed})
            except (HTTPException, OSError, ValueError) as error:
                record.accepting_steering = False
                record.add_event({"kind": "error", "message": "无法交付补充消息：" + str(error)})
                try:
                    self._write(record, {"kind": "user_input_checkpoint_response",
                        "requestId": payload["requestId"], "error": "human input checkpoint could not be persisted"})
                except HTTPException:
                    pass

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
        # A child can exit before the pipe-reader threads have reduced its
        # final delivery/error messages.  Drain both pipes before deciding
        # whether a generic process error is still needed.
        if record.stdout_thread is not None:
            record.stdout_thread.join(timeout=2)
        if record.stderr_thread is not None:
            record.stderr_thread.join(timeout=2)
        self.service_bridge.cancel_run(record.run_id)
        if record.status == "stopping":
            final_status: RunStatus = "stopped"
        elif return_code == 0:
            final_status = "completed"
        else:
            final_status = "failed"
        with record.input_lock:
            record.accepting_inputs = False
            record.accepting_steering = False
            for item in record.inputs.values():
                if item["status"] in {"accepted", "starting", "applied"}:
                    item["status"] = "held"
                    record.add_event({"kind": "run_input", "runInput": dict(item)})
            if record.input_protocol >= 2 and record.inputs:
                try:
                    from minicode.web_run_inputs import persist_inputs
                    persist_inputs(record)
                except (OSError, ValueError):
                    record.add_event({"kind": "error", "message": "任务已退出，但补充消息回执保存失败。"})
        try:
            from minicode.run_ownership import RuntimeRunOwnerLease
            from minicode.web_run_inputs import ledger_path
            with RuntimeRunOwnerLease(ledger_path(record.session_id, record.run_id), run_id=record.run_id):
                session = load_session(record.session_id)
                if session is not None:
                    record.recovery_pending = any(isinstance(trace, dict) and trace.get("recovery_pending")
                        and trace.get("web_run_id") == record.run_id for trace in session.runtime_runs)
                    if record.recovery_pending:
                        from minicode.web_candidate_recovery import WebCandidateRecovery
                        # Even exit code zero cannot certify an unpublished delivery.
                        if final_status == "completed":
                            final_status = "failed"
                        WebCandidateRecovery().reconcile_interrupted(session, web_run_id=record.run_id)
                    session.metadata.status = final_status
                    from minicode.runtime_delivery import review_pending_reason, review_pending_closeout
                    last_trace = next((trace for trace in reversed(session.runtime_runs)
                        if isinstance(trace, dict) and trace.get("web_run_id") == record.run_id), None)
                    record.review_pending = (return_code == 1 and final_status == "failed"
                        and not record.recovery_pending and bool(review_pending_reason(last_trace))
                        and not any(event.get("kind") == "error" for event in record.events))
                    session.metadata.review_pending = record.review_pending
                    record.review_closeout = review_pending_closeout(last_trace) if record.review_pending else ""
                    save_session(session, force_full=True)
                    record.recovery_pending = False
        except Exception:  # A storage failure must not strand an exited run as "running".
            record.review_pending = False
            if final_status == "completed":
                final_status = "failed"
            record.add_event({"kind": "error", "message": "任务进程已结束，但会话状态保存失败。请检查历史存储后重试；已有记录未删除。"})
        if final_status == "failed" and not record.review_pending:
            with record.condition:
                already_reported = any(event.get("kind") == "error" for event in record.events)
            if not already_reported:
                message = record.stderr_tail[-1] if record.stderr_tail else f"工作进程退出码: {return_code}"
                record.add_event({"kind": "error", "message": message})
        if not record.saw_done:
            record.add_event(
                {
                    "kind": "done",
                    "stopReason": "review_pending" if record.review_pending else final_status,
                }
            )
        with record.condition:
            record.status = final_status
            record.add_event({"kind": "status", "status": final_status,
                "reviewPending": record.review_pending,
                "content": "复核仍有待处理项，已有进度已保留。" if record.review_pending else "任务已结束"})
        if record.input_protocol >= 2:
            try:
                from minicode.web_run_inputs import persist_inputs
                with record.input_lock:
                    persist_inputs(record)
            except (OSError, ValueError):
                record.add_event({"kind": "error", "message": "最终补充消息回执保存失败。"})
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass

    def add_input(self, run_id: str, body: RunInputCreate) -> dict[str, Any]:
        record = self.get(run_id)
        try:
            from minicode.image_input import preflight
            preflight(body.text, _context_runtime(record.workspace))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        if body.kind == "steer":
            return self._add_steering(record, body)
        with self._lock, record.input_lock:
            existing = record.inputs.get(body.input_id)
            if existing is not None:
                if existing["kind"] != body.kind or existing["text"] != body.text.strip() or (
                    body.kind == "next_task" and existing["executionProfile"] != body.execution_profile
                ) or (
                    body.kind == "next_task" and body.context_settings is not None
                    and existing.get("contextSettings") != body.context_settings.request()
                ):
                    raise HTTPException(status_code=409, detail="该消息编号已用于另一条内容。")
                return dict(_safe_payload(existing))
            can_stage = body.stage and record.queue_protocol == 1 and record.accepting_steering
            if (not record.accepting_inputs and not can_stage) or record.status not in {"queued", "running"}:
                raise HTTPException(status_code=409, detail="本轮已结束接收，请直接发送新任务；草稿仍保留。")
            if not body.text.strip():
                raise HTTPException(status_code=400, detail="请输入补充要求或下一条任务。")
            queued_inputs = [item for item in record.inputs.values() if item["kind"] != "steer"]
            if (len(record.inputs) >= 128 or len(queued_inputs) >= 32
                or sum(item["status"] == "accepted" for item in queued_inputs) >= 8):
                raise HTTPException(status_code=409, detail="待处理消息已满，请等处理后再发送。")
            profile = ((record.execution or {}).get("profile", record.execution_profile)
                       if body.kind == "supplement" else body.execution_profile)
            if self.web_runtime_policy.applies(profile) and not can_stage:
                raise HTTPException(status_code=409, detail="Primary 试运行需独立启动并重新计算预算，请在本轮结束后发送新任务。")
            write_required = record.permission_mode == "bypass" or profile == "standard" or requires_workspace_write(
                body.text, {"runtimeOrchestration":
                    "primary" if self.web_runtime_policy.applies(profile) else "legacy"},
            )
            if write_required and any(
                other.run_id != record.run_id and other.status not in _TERMINAL_STATUSES
                and other.requires_workspace_write
                and normalize_workspace_path(other.workspace) == normalize_workspace_path(record.workspace)
                for other in self._runs.values()
            ):
                raise HTTPException(status_code=409, detail="该工作区已有其他写入任务，请稍后发送。")
            context = None
            if body.kind == "next_task":
                try:
                    previous_context = load_context_settings(record.session_id)
                    context = body.context_settings.merge(previous_context) if body.context_settings is not None else previous_context
                    context.validate_capacity(_context_runtime(record.workspace))
                    save_context_settings(record.session_id, context)
                except (OSError, ValueError) as error:
                    raise HTTPException(400, "无法保存对话上下文设置。" if isinstance(error, OSError) else str(error)) from error
            record.requires_workspace_write = record.requires_workspace_write or write_required
            item = {"inputId": body.input_id, "kind": body.kind, "text": body.text.strip(),
                    "executionProfile": profile, "status": "accepted",
                    "version": 1,
                    "queueMode": "automatic" if record.accepting_inputs and not self.web_runtime_policy.applies(profile) else "manual",
                    **({"contextSettings": context.request()} if context else {})}
            proposed = {**record.inputs, body.input_id: item}
            if record.queue_protocol:
                from minicode.web_run_inputs import persist_inputs
                try:
                    persist_inputs(record, inputs=proposed)
                except (OSError, ValueError) as error:
                    raise HTTPException(500, "无法保存队列消息；草稿仍保留。") from error
            record.inputs = proposed
            record.add_event({"kind": "run_input", "runInput": dict(item)})
            return dict(_safe_payload(item))

    def steer_input(self, run_id: str, input_id: str) -> dict[str, Any]:
        """Promote atomically under the same lock used by the final queue drain."""
        from minicode.web_run_inputs import persist_inputs
        record = self.get(run_id)
        with record.input_lock:
            previous = record.inputs.get(input_id)
            if previous is None:
                raise HTTPException(404, "队列消息不存在。")
            if previous["kind"] == "steer":
                return dict(_safe_payload(previous))
            if previous["status"] != "accepted":
                raise HTTPException(409, "这条消息已开始处理或队列已暂停，无法引导当前任务。")
            if not record.accepting_steering or record.input_protocol < 2 or record.status not in {"queued", "running"}:
                raise HTTPException(409, "当前任务已结束接收引导；消息仍保留在队列。")
            if len(previous["text"]) > 60000:
                raise HTTPException(400, "引导消息最长为 60000 字符，请取回编辑后缩短。")
            if sum(item["kind"] == "steer" and item["status"] in {"accepted", "starting", "applied"}
                   for item in record.inputs.values()) >= 8:
                raise HTTPException(409, "待处理引导消息已满；消息仍保留在队列。")
            item = {"inputId": input_id, "kind": "steer", "text": previous["text"],
                    "executionProfile": record.execution_profile, "status": "accepted",
                    "sequence": previous.get("sequence", list(record.inputs).index(input_id) + 1),
                    "version": previous.get("version", 0) + 1}
            proposed = {**record.inputs, input_id: item}
            try:
                persist_inputs(record, inputs=proposed)
            except (OSError, ValueError) as error:
                raise HTTPException(500, "无法保存引导；消息仍保留在队列。") from error
            record.inputs = proposed
            record.add_event({"kind": "run_input", "runInput": dict(item)})
            try:
                self._write(record, {"kind": "user_input_control", "action": "queue",
                    "runId": record.run_id, "input": item})
            except HTTPException:
                item.update(status="held", reason="任务进程已结束，消息已保留，可取回重新发送。")
                persist_inputs(record)
                record.add_event({"kind": "run_input", "runInput": dict(item)})
            return dict(_safe_payload(item))

    def _add_steering(self, record: RunRecord, body: RunInputCreate) -> dict[str, Any]:
        from minicode.web_run_inputs import persist_inputs
        text = body.text.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", body.input_id) or not 1 <= len(text) <= 60000:
            raise HTTPException(400, "引导消息需要有效编号，内容长度为 1 至 60000 字符。")
        with record.input_lock:
            existing = record.inputs.get(body.input_id)
            if existing is not None:
                if existing["kind"] != "steer" or existing["text"] != text:
                    raise HTTPException(409, "该消息编号已用于另一条内容。")
                return dict(_safe_payload(existing))
            if (record.input_protocol < 2 or not record.accepting_steering
                or record.status not in {"queued", "running"}):
                raise HTTPException(409, "当前任务已关闭引导输入；草稿仍保留，可发送为新任务。")
            if len(record.inputs) >= 128 or sum(item["kind"] == "steer" and item["status"] in
                {"accepted", "starting", "applied"} for item in record.inputs.values()) >= 8:
                raise HTTPException(409, "待处理引导消息已满，请等处理后再发送。")
            item = {"inputId": body.input_id, "kind": "steer", "text": text,
                "executionProfile": record.execution_profile, "status": "accepted",
                "sequence": len(record.inputs) + 1}
            proposed = {**record.inputs, body.input_id: item}
            try:
                persist_inputs(record, inputs=proposed)
            except (OSError, ValueError) as error:
                raise HTTPException(500, "无法保存引导消息；草稿仍保留。") from error
            record.inputs = proposed
            record.add_event({"kind": "run_input", "runInput": dict(item)})
            try:
                self._write(record, {"kind": "user_input_control", "action": "queue",
                    "runId": record.run_id, "input": item})
            except HTTPException:
                item["status"] = "held"
                persist_inputs(record)
                record.add_event({"kind": "run_input", "runInput": dict(item)})
            return dict(_safe_payload(item))

    def cancel_input(self, run_id: str, input_id: str) -> dict[str, Any]:
        record = self.get(run_id)
        if record.restored and record.status not in _TERMINAL_STATUSES:
            raise HTTPException(409, "旧执行尚未退出，暂不能撤回消息；请等待状态确认。")
        with record.input_lock:
            item = record.inputs.get(input_id)
            steering = item is not None and item["kind"] == "steer"
        if steering:
            return self._cancel_steering(record, input_id)
        with record.input_lock:
            item = record.inputs.get(input_id)
            if item is None:
                raise HTTPException(status_code=404, detail="待处理消息不存在。")
            if record.queue_resumed_run_id:
                raise HTTPException(409, "队列已经转入新任务，请刷新会话后操作。")
            if item["status"] not in {"accepted", "held", "canceled"}:
                raise HTTPException(status_code=409, detail="这条消息已开始处理，无法撤回。")
            proposed = {**record.inputs, input_id: {**item, "status": "canceled"}}
            if record.queue_protocol:
                from minicode.web_run_inputs import persist_inputs
                try:
                    persist_inputs(record, inputs=proposed)
                except (OSError, ValueError) as error:
                    raise HTTPException(500, "无法保存撤回状态；消息仍保留在队列。") from error
            record.inputs = proposed
            item = record.inputs[input_id]
            record.add_event({"kind": "run_input", "runInput": dict(item)})
            return dict(_safe_payload(item))

    def _cancel_steering(self, record: RunRecord, input_id: str) -> dict[str, Any]:
        from minicode.web_run_inputs import persist_inputs
        # Never hold input_lock while awaiting stdout: that reader also drains
        # checkpoints. The worker is the authority on consumption/cancellation.
        with record.control_lock:
            with record.input_lock:
                item = record.inputs[input_id]
                if item["status"] == "canceled":
                    return dict(_safe_payload(item))
                if item["status"] not in {"accepted", "held"}:
                    raise HTTPException(409, "这条引导已开始处理，无法撤回。")
                if record.status in _TERMINAL_STATUSES or item["status"] == "held":
                    item["status"] = "canceled"
                    persist_inputs(record)
                    record.add_event({"kind": "run_input", "runInput": dict(item)})
                    return dict(_safe_payload(item))
            request_id = uuid.uuid4().hex
            self._write(record, {"kind": "user_input_control", "action": "cancel",
                "runId": record.run_id, "inputId": input_id, "requestId": request_id})
            deadline = time.monotonic() + 10
            with record.condition:
                while request_id not in record.control_responses:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or record.status in _TERMINAL_STATUSES:
                        raise HTTPException(409, "未确认撤回结果；请刷新消息状态后重试。")
                    record.condition.wait(min(remaining, 0.1))
                receipt = record.control_responses.pop(request_id)
            if receipt.get("error") or not isinstance(receipt.get("runInput"), dict):
                raise HTTPException(409, receipt.get("error") or "无法确认引导消息状态。")
            with record.input_lock:
                # A checkpoint can replace the host's input dictionary while
                # this RPC waits. Update the current record, not a stale alias.
                item = record.inputs[input_id]
                item.update(receipt["runInput"])
                persist_inputs(record)
                record.add_event({"kind": "run_input", "runInput": dict(item)})
                return dict(_safe_payload(item))

    def stop(self, run_id: str) -> RunRecord:
        # Stopping is still allowed if another/older server archived the session.
        # Other operations continue to reject archived sessions in get().
        with self._lock:
            record = self._runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="任务不存在。")
        if record.status in _TERMINAL_STATUSES:
            return record
        record.status = "stopping"
        self.service_bridge.cancel_run(record.run_id)
        record.add_event({"kind": "status", "status": "stopping", "content": "正在停止任务"})

        def terminate() -> None:
            process = record.process
            if process is None or process.poll() is not None:
                return
            try:
                if self.web_runtime_policy.applies(record.execution_profile):
                    try:
                        self._write(record, {"kind": "run_control", "action": "cancel",
                            "executionId": (record.execution or {}).get("executionId") or record.run_id})
                        # Persist cancel intent and allow native usage/candidate
                        # settlement. The old process-tree stop remains bounded
                        # fallback when assembly or a provider does not return.
                        process.wait(timeout=12)
                        return
                    except (HTTPException, subprocess.TimeoutExpired):
                        pass
                terminate_worker(process)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                record.add_event({"kind": "error", "message": "未能停止任务进程，请检查本机进程权限后重试。"})

        threading.Thread(target=terminate, daemon=True).start()
        return record

    def answer_question(self, run_id: str, response: QuestionResponse) -> RunRecord:
        record = self.get(run_id)
        answer = response.answer.strip()
        if not answer:
            raise HTTPException(status_code=422, detail="请填写答案或选择一个选项。")
        with record.condition:
            previous = record.question_answers.get(response.request_id)
            if previous is not None:
                if previous != answer:
                    raise HTTPException(status_code=409, detail="这个问题已经回答，不能重复提交不同答案。")
                return record
            if record.status != "running" or response.request_id not in record.pending_questions:
                raise HTTPException(status_code=409, detail="这个问题已结束或不属于当前任务，请刷新任务状态。")
            self._write(record, {"kind": "question_response", "requestId": response.request_id, "answer": answer})
            record.question_answers[response.request_id] = answer
        return record

    def answer_question_group(self, run_id: str, group_id: str, response: QuestionGroupResponse) -> RunRecord:
        record = self.get(run_id)
        answers = {item.request_id: item.answer.strip() for item in response.answers}
        if len(answers) != len(response.answers) or any(not answer for answer in answers.values()):
            raise HTTPException(status_code=422, detail="请为本组每道问题填写一个有效答案。")
        with record.condition:
            for request_id, answer in answers.items():
                previous = record.question_answers.get(request_id)
                if previous is not None and previous != answer:
                    raise HTTPException(status_code=409, detail="这组问题已经提交，不能重复提交不同答案。")
            remaining = {key: answer for key, answer in answers.items() if key not in record.question_answers}
            if not remaining:
                return record
            pending = {key for key, question in record.pending_questions.items()
                       if question.get("questionGroupId") == group_id and key not in record.question_answers}
            if record.status != "running" or set(remaining) != pending:
                raise HTTPException(status_code=409, detail="请完整回答同一组问题；问题已结束时请刷新任务状态。")
            # Validate the entire group before sending a single worker message.
            # No answer is committed while the user is still editing earlier questions.
            self._write(record, {"kind": "question_responses", "questionGroupId": group_id,
                                 "answers": [{"requestId": key, "answer": answer} for key, answer in remaining.items()]})
            record.question_answers.update(remaining)
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

    def stop_agent(self, run_id: str, request: AgentStopRequest) -> dict[str, Any]:
        record = self.get(run_id)
        execution = record.execution or {}
        if (record.status not in {"queued", "running"} or execution.get("executionId") != request.execution_id
            or not any(node.get("nodeId") == request.task_id and request.task_id.startswith(("agent-", "builder-"))
                       for node in execution.get("nodes", []))):
            raise HTTPException(status_code=409, detail="子任务不属于当前活动运行。")
        self._write(record, {"kind": "agent_control", **request.model_dump(by_alias=True)})
        return {"status": "queued", "taskId": request.task_id, "operationId": request.operation_id,
            "notice": "停止请求已发送；实际停止与退出以运行状态为准。"}

    def session_is_active(self, session_id: str) -> bool:
        with self._lock:
            return any(
                record.session_id == session_id and (
                    record.status not in _TERMINAL_STATUSES
                    or (record.process is not None and record.process.poll() is None)
                )
                for record in self._runs.values()
            )


def _session_trash() -> SessionTrash:
    return SessionTrash(session_store.SESSIONS_DIR)


def _require_unarchived_session(session_id: str) -> None:
    if _session_trash().get(session_id) is not None:
        raise HTTPException(status_code=409, detail="对话已移入回收站，请先恢复后再打开。")


def _trash_session_payload(entry: TrashedSession) -> dict[str, Any]:
    metadata = session_store.SessionMetadata(**entry.metadata)
    return {
        **_session_metadata_payload(
            metadata, scope=_session_scope(metadata.session_id, metadata.workspace),
        ),
        "archivedAt": round(entry.archived_at * 1000),
        "deletionPending": entry.deletion_pending,
    }


def _session_metadata_payload(
    metadata: Any,
    *,
    scope: Literal["project", "standalone"] = "project",
    navigation_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = asdict(metadata)
    if navigation_state is None:
        with _WEB_STATE_LOCK:
            navigation_state = _read_web_state_unlocked()
    workspace = value.get("workspace", "")
    binding = value.get("binding") or {}
    worktree_status = None
    if binding.get("worktree_id"):
        from minicode.git_workspaces import GitWorktreeService
        from minicode.git_snapshot import WorktreeError
        try:
            worktree_status = GitWorktreeService().get(binding["worktree_id"])["status"]
        except WorktreeError:
            worktree_status = "missing"
    return {
        "sessionId": value.get("session_id", ""),
        "title": _navigation_names(navigation_state, "sessionNames").get(value.get("session_id", ""), ""),
        "createdAt": round(float(value.get("created_at", 0)) * 1000),
        "updatedAt": round(float(value.get("updated_at", 0)) * 1000),
        "firstMessage": value.get("first_message", ""),
        "lastMessage": value.get("last_message", ""),
        "messageCount": value.get("message_count", 0),
        "workspace": workspace,
        "scope": scope,
        "projectPath": binding.get("project_path", workspace) if scope == "project" else None,
        "projectId": binding.get("project_id"),
        "worktreeId": binding.get("worktree_id"),
        "worktreeStatus": worktree_status,
        "forkedFromId": binding.get("forked_from_id"),
        "forkedAtTurnId": binding.get("forked_at_turn_id"),
        "model": value.get("model"),
        "status": value.get("status", "active"),
        "reviewPending": value.get("status") == "failed" and bool(value.get("review_pending")),
        "toolCallCount": value.get("tool_call_count", 0),
        "runtimeSummary": value.get("runtime_summary", ""),
        "checkpointCount": value.get("checkpoint_count", 0),
        "instructionSummary": value.get("instruction_summary", ""),
        "hookSummary": value.get("hook_summary", ""),
        "delegationSummary": value.get("delegation_summary", ""),
        "extensionSummary": value.get("extension_summary", ""),
        "readinessSummary": value.get("readiness_summary", ""),
        "memorySummary": value.get("memory_summary", ""),
    }


def _workspace_capabilities(workspace: Path, *, source: str = "workspace") -> dict[str, Any]:
    """Build a read-only capability snapshot without starting configured MCP servers."""

    try:
        loaded_runtime = load_runtime_config(workspace)
    except Exception:  # noqa: BLE001 - diagnostics should survive an invalid runtime file
        loaded_runtime = None
    runtime = dict(loaded_runtime or {})
    configured_mcp = runtime.get("mcpServers", {})
    registry_runtime = dict(runtime)
    registry_runtime["mcpServers"] = {}
    registry = create_default_tool_registry(
        str(workspace),
        runtime=registry_runtime,
        inspection_only=True,
    )
    try:
        product_snapshot = build_product_snapshot(workspace, runtime=runtime)
        try:
            memory_stats: Any = memory_inventory(workspace)
        except Exception:  # noqa: BLE001 - memory diagnostics are optional
            memory_stats = {"schema": "minicode-memory-inventory-v1", "status": "unavailable", "scopes": [], "entries": [], "taskHistory": []}
        result = capability_surface(
            registry,
            product_snapshot=product_snapshot,
            memory=memory_stats,
            mcp_servers=configured_mcp,
            source=source,
        )
        from minicode.web_surfaces import public_tool_catalog

        result["toolCatalog"] = public_tool_catalog(registry, str(workspace))
        return result
    finally:
        try:
            registry.dispose()
        except Exception:  # noqa: BLE001,S110 - disposer failures must not hide the snapshot
            pass


def _extension_payload(workspace: Path, manifest: Any) -> dict[str, Any]:
    try:
        checks = [asdict(check) for check in inspect_extension_health(workspace, manifest.key)]
    except (OSError, RuntimeError, ValueError) as exc:
        checks = [{"name": "health", "status": "fail", "detail": str(exc)[:1_000]}]
    contribution_counts = {
        kind: len(value) if isinstance(value, (list, dict)) else 0
        for kind, value in manifest.contributes.items()
        if value
    }
    return {
        "id": manifest.id,
        "key": manifest.key,
        "name": manifest.name,
        "scope": manifest.scope,
        "version": manifest.version,
        "apiVersion": manifest.api_version,
        "description": manifest.description,
        "publisher": manifest.publisher,
        "enabled": manifest.enabled,
        "trusted": manifest.trusted,
        "permissions": list(manifest.permissions),
        "grantedPermissions": list(manifest.granted_permissions),
        "activationEvents": list(manifest.activation_events),
        "dependencies": dict(manifest.dependencies),
        "contributionKinds": list(manifest.contribution_kinds),
        "contributionCounts": contribution_counts,
        "source": manifest.source,
        "manifestPath": manifest.path,
        "integrity": manifest.integrity,
        "health": manifest.health,
        "healthDetail": manifest.health_detail,
        "shadowedBy": manifest.shadowed_by,
        "validationErrors": list(manifest.validation_errors),
        "checks": checks,
    }


def _public_extension_text(value: Any, *, fallback: str = "", limit: int = 500) -> str:
    """Return bounded manifest text without stringifying nested secret-bearing values."""

    if not isinstance(value, str):
        return fallback
    return value.strip()[:limit]


def _public_extension_strings(value: Any, *, limit: int = 32) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _public_extension_text(item, limit=128)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _declared_extension_items(manifest: Any, kind: str) -> list[tuple[str, Any]]:
    raw = manifest.contributes.get(kind, [])
    if not isinstance(raw, list):
        return []
    result: list[tuple[str, Any]] = []
    for index, value in enumerate(raw):
        if isinstance(value, dict):
            name = _public_extension_text(value.get("name") or value.get("id"))
        elif kind == "skills" and isinstance(value, str):
            name = Path(value).name
        else:
            name = ""
        result.append((name or f"{kind}-{index + 1}", value))
    return result


def _extension_skill_payloads(manifests: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for manifest in manifests:
        for name, value in _declared_extension_items(manifest, "skills"):
            descriptor = value if isinstance(value, dict) else {}
            path = value if isinstance(value, str) else descriptor.get("path")
            result.append(
                {
                    "name": name,
                    "description": _public_extension_text(descriptor.get("description")),
                    "path": _public_extension_text(path, limit=320),
                    "extensionId": manifest.id,
                    "scope": manifest.scope,
                    "availability": manifest.health,
                }
            )
    return sorted(result, key=lambda item: (item["extensionId"], item["scope"], item["name"]))


def _extension_tool_payloads(manifests: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for manifest in manifests:
        for name, value in _declared_extension_items(manifest, "tools"):
            descriptor = value if isinstance(value, dict) else {}
            effect = _public_extension_text(
                descriptor.get("effect"), fallback="privileged", limit=64
            ).lower()
            replay_policy = _public_extension_text(descriptor.get("replayPolicy"), limit=64)
            if not replay_policy:
                replay_policy = "safe" if effect == "read_only" else "never_auto_retry"
            input_schema = descriptor.get("inputSchema", {})
            input_schema = input_schema if isinstance(input_schema, dict) else {}
            properties = input_schema.get("properties", {})
            input_fields = (
                [
                    str(field)[:128]
                    for field in properties
                    if isinstance(field, str) and field.strip()
                ][:32]
                if isinstance(properties, dict)
                else []
            )
            extension_segment = manifest.id.replace(".", "_").replace("-", "_")
            tool_segment = name.replace(".", "_").replace("-", "_")
            result.append(
                {
                    "name": name,
                    "runtimeName": f"ext__{extension_segment}__{tool_segment}",
                    "description": _public_extension_text(descriptor.get("description")),
                    "effect": effect,
                    "replayPolicy": replay_policy,
                    "concurrencySafe": bool(
                        descriptor.get("concurrencySafe", effect == "read_only")
                    ),
                    "destructive": bool(descriptor.get("destructive", False)),
                    "inputFields": input_fields,
                    "requiredInputs": _public_extension_strings(input_schema.get("required")),
                    "tags": _public_extension_strings(descriptor.get("tags")),
                    "extensionId": manifest.id,
                    "scope": manifest.scope,
                    "availability": manifest.health,
                }
            )
    return sorted(result, key=lambda item: (item["extensionId"], item["scope"], item["name"]))


def _extension_hook_payloads(manifests: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for manifest in manifests:
        for name, value in _declared_extension_items(manifest, "hooks"):
            descriptor = value if isinstance(value, dict) else {}
            match = descriptor.get("match", {})
            match = match if isinstance(match, dict) else {}
            timeout = descriptor.get("timeoutSeconds")
            result.append(
                {
                    "name": name,
                    "description": _public_extension_text(descriptor.get("description")),
                    "event": _public_extension_text(descriptor.get("event"), limit=64),
                    "matchTools": _public_extension_strings(match.get("tools")),
                    "errorsOnly": bool(match.get("errorsOnly", False)),
                    "timeoutSeconds": (
                        timeout
                        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                        else None
                    ),
                    "extensionId": manifest.id,
                    "scope": manifest.scope,
                    "availability": manifest.health,
                }
            )
    return sorted(result, key=lambda item: (item["extensionId"], item["scope"], item["name"]))


def _extension_ui_payloads(manifests: list[Any]) -> list[dict[str, Any]]:
    allowed_surfaces = {"task", "capabilities", "runtime", "context", "settings"}
    result: list[dict[str, Any]] = []
    for manifest in manifests:
        for name, value in _declared_extension_items(manifest, "ui"):
            descriptor = value if isinstance(value, dict) else {}
            surface = _public_extension_text(
                descriptor.get("surface"), fallback="task", limit=64
            ).lower()
            if surface not in allowed_surfaces:
                surface = "task"
            result.append(
                {
                    "name": name,
                    "label": _public_extension_text(
                        descriptor.get("label") or descriptor.get("title"),
                        fallback=name,
                    ),
                    "description": _public_extension_text(descriptor.get("description")),
                    "path": _public_extension_text(descriptor.get("path"), limit=320),
                    "surface": surface,
                    "extensionId": manifest.id,
                    "scope": manifest.scope,
                    "availability": manifest.health,
                }
            )
    return sorted(result, key=lambda item: (item["extensionId"], item["scope"], item["name"]))


def _extensions_view(workspace: Path) -> dict[str, Any]:
    manifests = resolve_extension_set(workspace)
    _, mcp_diagnostics = collect_extension_mcp_servers(workspace)
    commands = list_extension_commands(workspace)
    agent_profiles = list_extension_agent_profiles(workspace)
    providers = extension_provider_presets(workspace)
    return {
        "installRoots": {scope: str(extension_root(scope, workspace)) for scope in ("project", "global")},
        "extensions": [_extension_payload(workspace, manifest) for manifest in manifests],
        "commands": [
            {
                "name": command.name,
                "usage": command.usage,
                "description": command.description,
                "extensionId": command.extension_id,
                "scope": command.extension_scope,
            }
            for command in commands
        ],
        "agentProfiles": [
            {
                "name": profile.name,
                "description": profile.description,
                "extensionId": profile.extension_id,
                "scope": profile.extension_scope,
                "model": profile.model,
                "provider": profile.provider,
                "executionProfile": profile.execution_profile,
                "toolAllow": list(profile.tool_allow),
                "toolDeny": list(profile.tool_deny),
            }
            for profile in agent_profiles
        ],
        "providers": [
            {
                "id": provider_id,
                "label": str(descriptor.get("label") or provider_id),
                "extensionId": str(descriptor.get("extensionId") or ""),
                "scope": str(descriptor.get("extensionScope") or ""),
                "transport": str(descriptor.get("transport") or "openai-compatible"),
                "models": [str(item) for item in descriptor.get("models", [])]
                if isinstance(descriptor.get("models"), list)
                else [],
            }
            for provider_id, descriptor in providers.items()
        ],
        "skills": _extension_skill_payloads(manifests),
        "tools": _extension_tool_payloads(manifests),
        "hooks": _extension_hook_payloads(manifests),
        "uiContributions": _extension_ui_payloads(manifests),
        "mcpDiagnostics": [asdict(item) for item in mcp_diagnostics],
    }


class MemoryRevision(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    content: str | None = Field(default=None, min_length=1, max_length=16384)
    archived: StrictBool | None = None


def _context_knowledge(session: Any) -> dict[str, Any]:
    from minicode.product_surfaces import collect_instruction_layers
    from minicode.task_note_inventory import task_note_inventory
    return {"rules": public_instruction_layers([asdict(layer) for layer in collect_instruction_layers(session.workspace)]),
            "memory": {**memory_inventory(session.workspace), "workspaceScope": _session_scope(session.session_id, session.workspace)},
            "sources": load_sources(session.session_id),
            "taskNotes": task_note_inventory(session)}


def _session_inspection_payload(session: Any) -> dict[str, Any]:
    from minicode.product_surfaces import collect_instruction_layers
    from minicode.task_note_inventory import task_note_inventory
    workspace = Path(session.workspace)
    try:
        from minicode.context_usage import load_snapshot, stats_from_history, stats_from_snapshot
        snapshot = load_snapshot(session.session_id)
        if snapshot:
            context_stats = public_context_stats(stats_from_snapshot(snapshot))
        else:
            runtime = load_runtime_config(workspace)
            runtime = load_context_settings(session.session_id).apply(runtime)
            model = str(session.metadata.model or runtime.get("model") or "default")
            context_stats = public_context_stats(stats_from_history(session.messages, model, runtime))
    except Exception:  # noqa: BLE001 - inspection remains useful without token accounting
        context_stats = public_context_stats({})
    try:
        memory = memory_inventory(workspace)
        memory["workspaceScope"] = _session_scope(session.session_id, session.workspace)
    except Exception:  # noqa: BLE001
        memory = {"schema": "minicode-memory-inventory-v1", "status": "unavailable", "scopes": [], "entries": [], "taskHistory": []}

    permission_items = session.permissions_summary.get("items", []) \
        if isinstance(session.permissions_summary, dict) else []
    if not isinstance(permission_items, list):
        permission_items = []
    return {
        "contextStats": context_stats,
        "permissions": {
            "configured": bool(permission_items),
            "itemCount": len(permission_items),
        },
        "skills": public_skills(session.skills),
        "mcpServers": public_mcp_servers(session.mcp_servers),
        "instructionLayers": public_instruction_layers([asdict(layer) for layer in collect_instruction_layers(workspace)]),
        "contextSources": load_sources(session.session_id),
        "taskNotes": task_note_inventory(session),
        "hookStatus": public_hook_status(session.hook_status),
        "delegationStatus": public_delegation_status(session.delegation_status),
        "extensions": public_extensions(session.extension_manifests),
        "readiness": public_readiness(session.readiness_report),
        "memory": memory,
        "runtimeRuns": [
            public_runtime_trace(trace, workspace)
            for trace in session.runtime_runs[-20:]
            if isinstance(trace, dict)
        ],
        "checkpoints": public_checkpoints(session.checkpoints, workspace),
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
    from minicode.run_images import IMAGE_SUFFIXES, read_image_file, inspect_image
    if target.suffix.casefold() in IMAGE_SUFFIXES:
        from urllib.parse import urlencode
        try:
            data = read_image_file(target)
            image = inspect_image(data)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=415, detail=str(error)) from error
        return {"path": target.relative_to(workspace).as_posix(), "name": target.name,
                "content": "", "encoding": "", "size": len(data), "lineCount": 0, "truncated": False,
                "image": {**image, "url": "/api/workspace/image?" + urlencode({"path": str(workspace), "file": file})}}
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
        url_path = path.replace("\\", "/").strip("/")
        if url_path == "api" or url_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "当前后端没有此 API。请使用同一版本的前后端，重启 MiniCode 服务后刷新页面。"})
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
    web_runtime: Literal["legacy", "primary"] = "legacy",
    web_ultra_runtime: Literal["legacy", "primary"] = "legacy",
    web_agent_teams: bool = False,
    web_agent_team_writes: bool = False,
    web_primary_role: Literal["worker", "coordinator"] = "worker",
    service_storage_dir: Path | None = None,
) -> FastAPI:
    from minicode.config import MINI_CODE_DIR
    from minicode.development_services import DevelopmentServiceManager
    from minicode.web_services import install_service_routes
    from minicode.web_terminal import TerminalManager, install_terminal_routes
    terminals = TerminalManager()
    workspace_default = Path(default_workspace or Path.cwd()).expanduser().resolve()
    manager = RunManager(web_runtime=web_runtime, web_ultra_runtime=web_ultra_runtime,
        web_agent_teams=web_agent_teams, web_agent_team_writes=web_agent_team_writes, web_primary_role=web_primary_role,
        services=DevelopmentServiceManager(service_storage_dir or MINI_CODE_DIR / "development-services" / "web"))
    runtime_policy = manager.web_runtime_policy
    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await asyncio.to_thread(terminals.close)
            manager.services.close()
    app = FastAPI(title="MiniCode Local Web", version="0.1.0", lifespan=lifespan)
    app.state.terminal_manager = terminals
    install_terminal_routes(app, terminals)
    install_service_routes(app, manager)
    from minicode.web_worktrees import WorktreeOperations, install_worktree_routes
    operations = WorktreeOperations(manager)
    app.state.worktree_operations = operations
    install_worktree_routes(app, operations)
    from minicode.web_candidate_recovery import install_candidate_recovery_routes
    install_candidate_recovery_routes(app, manager)
    from minicode.web_search_settings import install_search_settings_routes
    install_search_settings_routes(app)

    @app.exception_handler(SessionTrashError)
    async def session_trash_error_handler(request: Request, exc: SessionTrashError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(StoreBusyError)
    async def store_busy_error_handler(request: Request, exc: StoreBusyError) -> JSONResponse:
        return JSONResponse(status_code=503, headers={"Retry-After": "1"},
                            content={"detail": "本机存储正在处理其他操作，请稍后重试。"})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # Pydantic's default error payload includes the submitted input, which
        # can contain an API key (including inside an unexpected field).
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]})
    app.state.run_manager = manager
    app.state.default_workspace = workspace_default
    app.state.current_workspace = workspace_default
    if workspace_default.is_dir():
        _record_recent_workspace(workspace_default)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
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

    @app.post("/api/image-attachments")
    async def image_upload(request: Request, name: str = Query(default="image.png", max_length=255)):
        from minicode.image_input import MAX_IMAGE_BYTES, store_image
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(413, "每张图片最多 8 MB，请压缩后重新添加。")
        try:
            return await asyncio.to_thread(store_image, bytes(data), name)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/api/image-attachments/{image_id}")
    def image_get(image_id: str):
        from minicode.image_input import load_image
        try:
            meta, pixels = load_image(image_id, pixels=True)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        return Response(content=pixels, media_type=meta["mediaType"], headers={
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": '"' + meta["sha256"] + '"',
        })

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        current_workspace: Path = app.state.current_workspace
        return {
            "conversationViewVersion": 1,
            "workspace": str(current_workspace),
            "settings": runtime_policy.settings_view(get_settings_view(current_workspace)),
            "workspaceStatus": _workspace_status(current_workspace) if current_workspace.is_dir() else None,
            "recentWorkspaces": _recent_workspaces(current_workspace),
        }

    @app.get("/api/settings")
    def settings_get() -> dict[str, Any]:
        return runtime_policy.settings_view(get_settings_view(app.state.current_workspace))

    @app.get("/api/context-settings")
    def context_defaults(scope: Literal["project", "standalone"] = "standalone",
                         workspace: str | None = Query(default=None, max_length=4096)) -> dict[str, Any]:
        return ConversationContext().view(_context_runtime(_conversation_workspace(scope, workspace)))

    @app.post("/api/context-settings/preview")
    def context_preview(body: ConversationContext, scope: Literal["project", "standalone"] = "standalone",
                        workspace: str | None = Query(default=None, max_length=4096)) -> dict[str, Any]:
        # Drafts have no session to persist yet. Resolve their actual provider,
        # effort and window with the same policy used at request admission.
        runtime = _context_runtime(_conversation_workspace(scope, workspace))
        try:
            body.validate_capacity(runtime)
            return body.view(runtime)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.put("/api/settings")
    def settings_put(update: SettingsUpdate) -> dict[str, Any]:
        if "web_agent_settings" in update.model_fields_set and runtime_policy.ultra != "primary":
            raise HTTPException(status_code=409, detail="本机服务已禁用 Ultra 协作；请恢复 Ultra primary 服务配置后再修改。")
        return runtime_policy.settings_view(save_settings(update, app.state.current_workspace))

    @app.post("/api/settings/test")
    async def settings_test() -> dict[str, Any]:
        return await asyncio.to_thread(test_saved_provider, app.state.current_workspace)

    @app.get("/api/settings/balance")
    def settings_balance() -> JSONResponse:
        return JSONResponse(content=get_saved_provider_balance(), headers={"Cache-Control": "no-store"})


    def extension_workspace(value: str | None, context_scope: str = "project", resource_scope: str | None = None) -> Path:
        if context_scope == "standalone":
            if resource_scope == "project":
                raise HTTPException(status_code=400, detail="当前管理范围是本机全局，请先选择项目再管理项目扩展。")
            return _standalone_workspace()
        return _resolve_workspace(value) if value else app.state.current_workspace

    @app.get("/api/extensions")
    def extensions_get(
        workspace: str | None = Query(default=None, max_length=4_096),
        context_scope: Literal["project", "standalone"] = Query(default="project", alias="contextScope"),
    ) -> dict[str, Any]:
        return _extensions_view(extension_workspace(workspace, context_scope))

    def skill_workspace(workspace: str | None, scope: str) -> Path:
        return _standalone_workspace() if scope == "standalone" else extension_workspace(workspace)

    def invocation_workspace(workspace: str | None, scope: str | None, fallback: Path) -> Path:
        if scope is None:
            return fallback
        if scope == "project" and not workspace:
            raise HTTPException(status_code=400, detail="请明确指定当前对话所属的项目。")
        return skill_workspace(workspace, scope)

    @app.get("/api/skills")
    def skills_get(workspace: str | None = Query(default=None, max_length=4096),
                   scope: Literal["project", "standalone"] = Query(default="project"),
                   invocation_path: str | None = Query(default=None, alias="invocationWorkspace", max_length=4096),
                   invocation_scope: Literal["project", "standalone"] | None = Query(default=None, alias="invocationScope")) -> dict[str, Any]:
        from minicode.skill_inventory import skill_inventory
        managed = skill_workspace(workspace, scope)
        return skill_inventory(managed, invocation_cwd=invocation_workspace(invocation_path, invocation_scope, managed),
                               include_project_roots=scope == "project")

    @app.get("/api/skills/{key}/preview")
    def skill_preview_get(key: str, workspace: str | None = Query(default=None, max_length=4096),
                          scope: Literal["project", "standalone"] = Query(default="project")) -> dict[str, Any]:
        from minicode.skill_inventory import preview_skill
        try:
            return preview_skill(skill_workspace(workspace, scope), key)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/skills/{key}/command")
    def skill_command_get(key: str, workspace: str | None = Query(default=None, max_length=4096),
                          scope: Literal["project", "standalone"] = Query(default="project"),
                          invocation_path: str | None = Query(default=None, alias="invocationWorkspace", max_length=4096),
                          invocation_scope: Literal["project", "standalone"] | None = Query(default=None, alias="invocationScope")) -> dict[str, str]:
        from minicode.skill_inventory import skill_invocation_command
        managed = skill_workspace(workspace, scope)
        try:
            return skill_invocation_command(managed, key, invocation_cwd=invocation_workspace(invocation_path, invocation_scope, managed))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/extensions/install")
    def extensions_install(body: ExtensionInstallRequest) -> dict[str, Any]:
        workspace = extension_workspace(body.workspace, body.context_scope, body.scope)
        try:
            manifest = install_extension(
                workspace,
                body.source,
                scope=body.scope,
                trusted=body.trusted,
                granted_permissions=body.granted_permissions,
                revision=body.revision,
            )
        except (ExtensionValidationError, ExtensionError, OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "extension": _extension_payload(workspace, manifest),
            **_extensions_view(workspace),
        }

    @app.post("/api/extensions/install/mcp")
    def extensions_install_mcp(body: McpExtensionInstallRequest) -> dict[str, Any]:
        workspace = extension_workspace(body.workspace, body.context_scope, body.scope)
        try:
            manifest = install_mcp_extension(
                workspace,
                name=body.name,
                transport=body.transport,
                scope=body.scope,
                command=body.command,
                args=body.args,
                url=body.url,
            )
        except (ExtensionValidationError, ExtensionError, OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "extension": _extension_payload(workspace, manifest),
            **_extensions_view(workspace),
        }

    @app.patch("/api/extensions/{scope}/{extension_id}")
    def extensions_update(
        scope: Literal["project", "global"],
        extension_id: str,
        body: ExtensionStateUpdate,
    ) -> dict[str, Any]:
        workspace = extension_workspace(body.workspace, body.context_scope, scope)
        identifier = f"{scope}:{extension_id}"
        try:
            manifest = resolve_extension(workspace, identifier)
            if body.enabled is not None:
                manifest = set_extension_enabled(workspace, identifier, body.enabled)
            if body.trusted is not None or body.granted_permissions is not None:
                trusted = manifest.trusted if body.trusted is None else body.trusted
                manifest = set_extension_trusted(
                    workspace,
                    identifier,
                    trusted,
                    granted_permissions=body.granted_permissions,
                )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ExtensionValidationError, ExtensionError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "extension": _extension_payload(workspace, manifest),
            **_extensions_view(workspace),
        }

    @app.get("/api/extensions/{scope}/{extension_id}/health")
    def extension_health_get(
        scope: Literal["project", "global"],
        extension_id: str,
        workspace: str | None = Query(default=None, max_length=4_096),
        context_scope: Literal["project", "standalone"] = Query(default="project", alias="contextScope"),
    ) -> dict[str, Any]:
        resolved_workspace = extension_workspace(workspace, context_scope, scope)
        try:
            manifest = resolve_extension(resolved_workspace, f"{scope}:{extension_id}")
            checks = inspect_extension_health(resolved_workspace, manifest.key)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ExtensionValidationError, ExtensionError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "extension": _extension_payload(resolved_workspace, manifest),
            "checks": [asdict(check) for check in checks],
        }

    @app.post("/api/extensions/{scope}/{extension_id}/rollback")
    def extension_rollback_post(
        scope: Literal["project", "global"],
        extension_id: str,
        body: ExtensionStateUpdate,
    ) -> dict[str, Any]:
        workspace = extension_workspace(body.workspace, body.context_scope, scope)
        try:
            manifest = rollback_extension(workspace, f"{scope}:{extension_id}")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ExtensionValidationError, ExtensionError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "extension": _extension_payload(workspace, manifest),
            **_extensions_view(workspace),
        }

    @app.delete("/api/extensions/{scope}/{extension_id}")
    def extension_delete(
        scope: Literal["project", "global"],
        extension_id: str,
        workspace: str | None = Query(default=None, max_length=4_096),
        context_scope: Literal["project", "standalone"] = Query(default="project", alias="contextScope"),
    ) -> dict[str, Any]:
        resolved_workspace = extension_workspace(workspace, context_scope, scope)
        try:
            removed = uninstall_extension(
                resolved_workspace,
                f"{scope}:{extension_id}",
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ExtensionValidationError, ExtensionError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"result": removed, **_extensions_view(resolved_workspace)}

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
        return manager.overlay_navigation_statuses(
            _navigation_payload(app.state.current_workspace)
        )

    @app.delete("/api/navigation/workspaces")
    def navigation_workspace_close(body: NavigationWorkspaceUpdate) -> dict[str, Any]:
        """Close a project: drop it from the rail list, keep every conversation.

        The conversations of the closed project stay in the global list and its
        files and memory authority are untouched; only the rail entry is
        removed. Reopening the same folder restores the entry.
        """
        previous = app.state.current_workspace
        closed, workspace, recent = _close_navigation_workspace(
            body.target,
            current=previous,
            fallback=app.state.default_workspace,
        )
        app.state.current_workspace = workspace
        payload = manager.overlay_navigation_statuses(_navigation_payload(workspace))
        payload["closedWorkspace"] = str(closed)
        payload["workspace"] = str(workspace)
        payload["activeWorkspaceChanged"] = _path_identity(
            workspace
        ) != _path_identity(previous)
        payload["recentWorkspaces"] = recent
        return payload

    @app.put("/api/navigation/pins")
    def navigation_pin(update: NavigationPinUpdate) -> dict[str, Any]:
        _set_navigation_pin(update)
        return manager.overlay_navigation_statuses(
            _navigation_payload(app.state.current_workspace)
        )

    @app.put("/api/navigation/names")
    def navigation_name(update: NavigationNameUpdate) -> dict[str, Any]:
        with manager._lock:
            _set_navigation_name(update)
        return manager.overlay_navigation_statuses(
            _navigation_payload(app.state.current_workspace)
        )

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
        if body.mode == "worktree" and (body.scope != "project" or not body.request_id):
            raise HTTPException(400, "工作树需要项目目录和请求标识。")
        if body.request_id:
            import hashlib
            return {"operation": operations.submit("create", body.request_id, {
                "workspace": str(workspace), "mode": body.mode, "scope": body.scope,
                "includeIgnored": body.include_ignored,
                "worktreeId": hashlib.sha256(f"create:{body.request_id}".encode()).hexdigest()[:32] if body.mode == "worktree" else None,
            })}
        session = create_new_session(str(workspace))
        from minicode.session_history import project_binding
        session.binding = project_binding(str(workspace))
        save_session(session, force_full=True)
        _set_session_scope(session.session_id, body.scope)
        return {"session": _session_metadata_payload(session.metadata, scope=body.scope)}

    @app.get("/api/trash")
    def trash_get() -> dict[str, Any]:
        with manager._lock:
            return {"sessions": [_trash_session_payload(entry) for entry in _session_trash().entries()]}

    @app.post("/api/sessions/{session_id}/archive")
    def session_archive(session_id: str) -> dict[str, Any]:
        # Share the start lock so a run cannot slip between the check and marker.
        with manager._lock:
            if manager.session_is_active(session_id):
                raise HTTPException(status_code=409, detail="任务运行中，请先停止再移入回收站。")
            session = load_session(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="会话不存在。")
            entry = _session_trash().archive(asdict(session.metadata))
            terminals.close_session(session_id)
            return {"archived": True, "session": _trash_session_payload(entry)}

    @app.post("/api/sessions/{session_id}/restore")
    def session_restore(session_id: str) -> dict[str, Any]:
        with manager._lock:
            trash = _session_trash()
            entry = trash.get(session_id)
            if entry is None:
                raise HTTPException(status_code=404, detail="回收站中没有这条对话。")
            if entry.deletion_pending:
                raise HTTPException(status_code=409, detail="对话已开始永久删除，无法恢复，请重试完成删除。")
            if load_session(session_id) is None:
                raise HTTPException(status_code=409, detail="会话文件缺失或损坏，暂时无法恢复。")
            trash.forget(session_id)
            return {"restored": True}

    @app.get("/api/sessions/{session_id}")
    def session_get(session_id: str) -> dict[str, Any]:
        _require_unarchived_session(session_id)
        session = load_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        from minicode.session_history import display_messages
        messages = _safe_payload(display_messages(session))
        return {
            "session": _session_metadata_payload(
                session.metadata,
                scope=_session_scope(session.session_id, session.workspace),
            ),
            "messages": messages[-300:],
        }

    @app.get("/api/sessions/{session_id}/context-settings")
    def session_context_get(session_id: str) -> dict[str, Any]:
        _require_unarchived_session(session_id)
        session = session_store.load_session_metadata(session_id)
        if session is None:
            raise HTTPException(404, "会话不存在。")
        try:
            return load_context_settings(session_id).view(_context_runtime(session.workspace))
        except (OSError, ValueError) as error:
            raise HTTPException(400, "无法读取上下文设置，请重新保存。") from error

    @app.put("/api/sessions/{session_id}/context-settings")
    def session_context_put(session_id: str, body: ConversationContext) -> dict[str, Any]:
        from minicode.store_lock import store_lock
        with manager._lock, store_lock(session_store.SESSIONS_DIR / ".store.lock"):
            _require_unarchived_session(session_id)
            session = load_session(session_id)
            if session is None:
                raise HTTPException(404, "会话不存在。")
            try:
                runtime = _context_runtime(session.workspace)
                if body.model_fields_set != {"max_tokens", "reasoning_effort"}:
                    body = body.merge(load_context_settings(session_id))
                body.validate_capacity(runtime)
                save_context_settings(session_id, body)
                return body.view(runtime)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            except OSError as error:
                raise HTTPException(400, "无法保存上下文设置，请重试。") from error

    @app.get("/api/sessions/{session_id}/inspect")
    def session_inspect(session_id: str) -> dict[str, Any]:
        _require_unarchived_session(session_id)
        manager.latest_for_session(session_id)
        session = load_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        return {
            "session": _session_metadata_payload(
                session.metadata,
                scope=_session_scope(session.session_id, session.workspace),
            ),
            "inspect": _session_inspection_payload(session),
        }

    @app.get("/api/sessions/{session_id}/context-stats")
    def session_context_stats(session_id: str) -> dict[str, Any]:
        _require_unarchived_session(session_id)
        if session_store.load_session_metadata(session_id) is None:
            raise HTTPException(404, "会话不存在。")
        from minicode.context_usage import load_snapshot, stats_from_snapshot
        snapshot = load_snapshot(session_id)
        return {"contextStats": public_context_stats(stats_from_snapshot(snapshot) if snapshot else {})}

    @app.get("/api/sessions/{session_id}/context-knowledge")
    def session_context_knowledge(session_id: str) -> dict[str, Any]:
        _require_unarchived_session(session_id)
        session = load_session(session_id)
        if session is None:
            raise HTTPException(404, "会话不存在。")
        return _context_knowledge(session)

    @app.get("/api/sessions/{session_id}/memory/export")
    def session_memory_export(session_id: str) -> Response:
        from urllib.parse import quote
        from minicode.paged_memory.config import PagedMemoryConfig
        from minicode.paged_memory.markdown_export import render_markdown

        _require_unarchived_session(session_id)
        session = session_store.load_session_metadata(session_id)
        if session is None:
            raise HTTPException(404, "会话不存在。")
        try:
            settings = load_effective_settings(session.workspace)
            config = PagedMemoryConfig.from_sources(settings, cwd=session.workspace,
                environ={**settings.get("env", {}), **os.environ})
            if config.backend != "sqlite":
                raise HTTPException(409, "当前存储配置不支持任务记忆导出。")
            if not Path(config.sqlite_path).is_file():
                raise HTTPException(404, "当前会话还没有可导出的任务记忆。")
            # The server derives the scope from the saved session, never query parameters.
            task_id = session_id if session_id.startswith("task_") else f"task_{session_id}"
            projection = render_markdown(config, tenant_id=config.tenant_id, task_id=task_id)
        except (OSError, ValueError, TypeError, sqlite3.Error) as error:
            raise HTTPException(503, "任务记忆暂时无法读取，请稍后重试。") from error
        if not projection["task_count"]:
            raise HTTPException(404, "当前会话还没有可导出的任务记忆。")
        filename = quote(f"minicode-task-memory-{session_id}.md", safe="")
        return Response(projection["content"], media_type="text/markdown", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    @app.get("/api/sessions/{session_id}/context-knowledge/rules/{name}")
    def session_rule_content(session_id: str, name: str) -> dict[str, Any]:
        _require_unarchived_session(session_id)
        session = load_session(session_id)
        if session is None:
            raise HTTPException(404, "会话不存在。")
        from minicode.product_surfaces import collect_instruction_layers
        layer = next((item for item in collect_instruction_layers(session.workspace) if item.name == name), None)
        if layer is None or layer.status not in {"available", "empty"}:
            raise HTTPException(404, "规则文件不存在或无法读取。")
        return {"content": layer.content, "path": layer.path, "contentHash": layer.content_hash}

    @app.put("/api/sessions/{session_id}/context-knowledge/memories/{scope}/{entry_id}")
    def session_memory_revision(session_id: str, scope: str, entry_id: str, body: MemoryRevision) -> dict[str, Any]:
        _require_unarchived_session(session_id)
        session = load_session(session_id)
        if session is None:
            raise HTTPException(404, "会话不存在。")
        if body.content is None and body.archived is None:
            raise HTTPException(400, "请选择修改内容或归档状态。")
        try:
            revise_memory(session.workspace, scope, entry_id, body.revision, content=body.content, archived=body.archived)
        except (FileNotFoundError, KeyError) as error:
            raise HTTPException(404, "记忆已不存在，请刷新。") from error
        except (ValueError, TypeError) as error:
            if str(error) == "memory_changed":
                raise HTTPException(409, "记忆已被更新，请刷新后重新修改；你的输入尚未保存。") from error
            if str(error) == "memory_capacity_exceeded":
                raise HTTPException(409, "记忆存储超过总容量限制，本次未保存，已有记忆未删除。请缩短内容后重试。") from error
            raise HTTPException(400, "记忆内容或存储格式不符合要求。") from error
        except (OSError, TimeoutError) as error:
            raise HTTPException(409, "暂时无法保存记忆，请稍后重试。") from error
        return _context_knowledge(session)

    @app.get("/api/storage/images")
    def image_storage() -> Response:
        from minicode.run_images import RunImageStore
        rows, unknown = [], 0
        trash = _session_trash()
        for metadata in session_store.list_sessions(include_archived=True):
            try:
                summary = RunImageStore(metadata.session_id).storage_summary()
                if not summary["storedCount"]:
                    continue
                error = ""
            except (OSError, ValueError, RuntimeError):
                unknown += 1
                summary = {"sessionId": metadata.session_id, "storedCount": None, "storedBytes": None, "revision": ""}
                error = "无法读取图片存储，请检查本机文件后刷新。"
            entry = trash.get(metadata.session_id)
            running = manager.session_is_active(metadata.session_id)
            pending = bool(entry and entry.deletion_pending)
            rows.append({**summary, "title": metadata.first_message or "未命名对话",
                         "workspaceName": Path(metadata.workspace).name if _session_scope(metadata.session_id, metadata.workspace) == "project" else "独立对话",
                         "updatedAt": round(metadata.updated_at * 1000),
                         "archived": entry is not None, "running": running,
                         "canClear": not error and not running and not pending,
                         "error": error or ("永久删除尚未完成，请在回收站中重试。" if pending else "")})
        rows.sort(key=lambda row: row["storedBytes"] or 0, reverse=True)
        return JSONResponse({"sessions": rows, "storedBytes": sum(row["storedBytes"] or 0 for row in rows),
                             "unknownSessionCount": unknown, "retention": "until-cleared-or-session-deleted"},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{session_id}/images/status")
    def image_status(session_id: str, image_ids: str) -> Response:
        _require_unarchived_session(session_id)
        if session_store.load_session_metadata(session_id) is None:
            raise HTTPException(404, "会话不存在。")
        ids = list(dict.fromkeys(image_ids.split(",")))
        if not 1 <= len(ids) <= 24 or any(not re.fullmatch(r"artifact-[a-f0-9]{32}", value) for value in ids):
            raise HTTPException(400, "请提供最多 24 个有效的图片标识。")
        from minicode.run_images import RunImageStore
        try:
            store = RunImageStore(session_id)
            return JSONResponse({"images": [store.availability(value) for value in ids]},
                                headers={"Cache-Control": "no-store"})
        except OSError as error:
            raise HTTPException(503, "暂时无法读取图片，请稍后重新加载。") from error

    @app.delete("/api/sessions/{session_id}/images")
    def clear_session_images(session_id: str, revision: str, confirmed: bool = False) -> Response:
        if not confirmed:
            raise HTTPException(400, "请先确认清理该对话的图片副本。")
        from minicode.run_images import RunImageStore
        from minicode.store_lock import store_lock
        with manager._lock, store_lock(session_store.SESSIONS_DIR / ".store.lock"):
            if manager.session_is_active(session_id):
                raise HTTPException(409, "任务运行中，请先停止再清理图片。")
            entry = _session_trash().get(session_id)
            if entry and entry.deletion_pending:
                raise HTTPException(409, "对话已开始永久删除，请在回收站中重试删除。")
            if session_store.load_session_metadata(session_id) is None:
                raise HTTPException(404, "会话不存在。")
            try:
                result = RunImageStore(session_id).clear(expected_revision=revision)
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            except (OSError, RuntimeError) as error:
                raise HTTPException(503, "图片清理未完成，文字记录仍保留；请刷新存储用量后重试。") from error

    @app.get("/api/sessions/{session_id}/images/{image_id}")
    def run_image(session_id: str, image_id: str, download: bool = False):
        _require_unarchived_session(session_id)
        if session_store.load_session_metadata(session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        from urllib.parse import quote

        from minicode.run_images import ImageClearedError, RunImageStore
        from minicode.runtime_artifacts import ArtifactIntegrityError
        try:
            metadata, data = RunImageStore(session_id).get(image_id)
        except ImageClearedError as error:
            raise HTTPException(status_code=410, detail="图片已清理。") from error
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="图片不存在或已清理。") from error
        except (ValueError, OSError, ArtifactIntegrityError) as error:
            raise HTTPException(status_code=410, detail="图片已不可用或完整性校验失败。") from error
        disposition = "attachment" if download else "inline"
        return Response(data, media_type=metadata["mediaType"], headers={
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(metadata['name'], safe='')}",
        })

    @app.get("/api/sessions/{session_id}/runs/{run_id}/project-artifacts/{artifact_id}")
    def project_artifact(session_id: str, run_id: str, artifact_id: str):
        _require_unarchived_session(session_id)
        session = load_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        # Resolve a session-owned trace first. No arbitrary filesystem path or
        # other worker's artifacts can be exposed through this endpoint.
        native_id = None
        for trace in reversed(session.runtime_runs):
            if not isinstance(trace, dict):
                continue
            projected = public_runtime_trace(trace, session.workspace)
            if run_id in {projected["runId"], projected["webRunId"]}:
                native_id = projected["runId"]
                break
        if not native_id:
            raise HTTPException(status_code=404, detail="此会话中没有该运行记录。")
        from minicode.runtime_artifacts import RuntimeArtifactStore, ArtifactIntegrityError
        store = RuntimeArtifactStore()
        try:
            ref = store.owned_reference(artifact_id, run_id=native_id, node_id="npm-build-and-browser")
            if (ref is None or ref.media_type not in {"image/png", "application/json", "application/zip"}
                    or ref.size > (64 * 1024 * 1024 if ref.media_type == "application/zip" else 16_000_000)):
                raise HTTPException(status_code=404, detail="检查附件不存在或不支持预览。")
            data = store.get_bytes(ref)
        except (ArtifactIntegrityError, ValueError, OSError):
            raise HTTPException(status_code=410, detail="检查附件已不可用或完整性校验失败，请重新检查项目。")
        return Response(data, media_type=ref.media_type, headers={"X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store", "Content-Security-Policy": "default-src 'none'; sandbox",
            **({"Content-Disposition": 'attachment; filename="playwright-trace.zip"'} if ref.media_type == "application/zip" else {})})

    @app.get("/api/sessions/{session_id}/run")
    def session_run(session_id: str, compact: bool = False) -> dict[str, Any]:
        _require_unarchived_session(session_id)
        session = session_store.load_session_metadata(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        record = manager.latest_for_session(session_id)
        if record is None:
            return {"run": None, "events": []}
        snapshot = record.snapshot(after=max(0, record.sequence - _MAX_RETAINED_RUN_EVENTS)
                                   if compact and record.status in _TERMINAL_STATUSES else 0)
        if (record.event_journal is not None or record.history_sequence) and (not compact or record.status not in _TERMINAL_STATUSES):
            # Read after capturing the event watermark so the browser cannot
            # combine older history with a newer committed event cursor.
            saved = load_session(session_id)
            count = record.history_start_count if record.event_journal is not None else snapshot["run"]["historyCount"]
            if saved is not None and len(saved.messages) >= count:
                from minicode.session_history import display_messages
                snapshot["historyMessages"] = _safe_payload(display_messages(saved, message_count=count))
        snapshot["run"]["scope"] = _session_scope(session_id, session.workspace)
        return snapshot

    @app.delete("/api/sessions/{session_id}")
    def session_delete(session_id: str, worktree: Literal["clean", "keep", "discard"] = "clean") -> dict[str, Any]:
        with manager._lock:
            if manager.session_is_active(session_id):
                raise HTTPException(status_code=409, detail="任务运行中，请先停止再删除会话。")
            trash = _session_trash()
            if trash.get(session_id) is None:
                raise HTTPException(status_code=409, detail="请先将对话移入回收站，再确认永久删除。")
            current = load_session(session_id)
            terminals.close_session(session_id)
            if current is not None:
                operations.prepare_session_deletion(current, worktree)
            trash.begin_delete(session_id)
        # The durable deletion marker blocks restore/start. Runtime garbage
        # collection may inventory many files; do not hold the global run
        # manager lock while it runs and freeze unrelated tasks or navigation.
        if not delete_session(session_id, missing_ok=True):
            raise HTTPException(status_code=503, detail="永久删除尚未完成，请在回收站中重试删除。")
        with manager._lock:
            _remove_session_navigation_state(session_id)
            manager._runs = {
                run_id: record for run_id, record in manager._runs.items()
                if record.session_id != session_id
            }
            trash.forget(session_id)
            return {"deleted": True}

    def run_workspace(body: RunCreate | TaskFilePreview) -> Path:
        workspace = None
        if body.session_id:
            _require_unarchived_session(body.session_id)
            session = load_session(body.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="会话不存在。")
            existing_scope = _session_scope(session.session_id, session.workspace)
            if existing_scope != body.scope:
                raise HTTPException(status_code=400, detail="不能在项目对话与独立对话之间切换会话类型。")
            # Existing sessions always execute in the durable server-side binding.
            if session.binding.get("worktree_id"):
                operations.service.get(session.binding["worktree_id"], require_ready=True)
            workspace = _resolve_workspace(session.workspace)
        else:
            workspace = _conversation_workspace(body.scope, body.workspace)
        return workspace

    @app.post("/api/task-files/preview")
    def preview_task_file(body: TaskFilePreview) -> dict[str, Any]:
        if body.scope != "project":
            raise HTTPException(status_code=400, detail="任务契约仅用于项目任务。")
        workspace = run_workspace(body)
        try:
            task = load_headless_task(body.path, workspace)
        except (ValueError, OSError) as error:
            detail = str(error) if isinstance(error, ValueError) else "无法读取契约文件，请检查路径与访问权限。"
            raise HTTPException(status_code=400, detail=detail) from error
        path = Path(body.path)
        path = (path if path.is_absolute() else workspace / path).absolute()
        return {"path": str(path), **task_preview(task)}

    @app.post("/api/runs")
    def runs_post(body: RunCreate) -> dict[str, Any]:
        if body.resume_queue_from and body.continuation:
            raise HTTPException(400, "队列继续和候选进度继续需要分别启动。")
        workspace = run_workspace(body)
        task = None
        if body.task_file is not None:
            if body.scope != "project":
                raise HTTPException(status_code=400, detail="任务契约仅用于项目任务。")
            try:
                task = selected_task(body.task_file.path, str(workspace), body.task_file.sha256, body.task_file.commands_trusted)
            except (ValueError, OSError) as error:
                detail = str(error) if isinstance(error, ValueError) else "无法读取契约文件，请重新预览。"
                raise HTTPException(status_code=400, detail=detail) from error
        if body.continuation is not None:
            if "execution_location" in body.model_fields_set and body.execution_location != "isolated":
                raise HTTPException(409, "候选任务继续时必须沿用隔离执行；不能把候选区直接切换为项目目录。")
            if (not body.session_id or body.scope != "project" or not isinstance(body, BudgetedRunCreate)
                or set(body.continuation) != {"runId", "artifactId"}):
                raise HTTPException(400, "继续需要当前项目会话、有效进度和明确的本轮预算。")
            from minicode.web_run_continuation import prepare_continuation
            from minicode.session_contract import atomic_write_json
            from minicode.store_lock import store_lock
            try:
                with manager._lock:
                    manager.latest_for_session(body.session_id)
                    prepared = prepare_continuation(load_session(body.session_id), body.continuation, task,
                                                    run_budget=body.run_budget)
                    if not manager.web_runtime_policy.applies(prepared["profile"]):
                        raise ValueError("当前服务未启用隔离任务继续，请先启用 Primary 运行模式。")
                    path = prepared["directory"] / "continuation.json"
                    from minicode.path_safety import is_unsafe_stored_path
                    if is_unsafe_stored_path(path, boundary=prepared["directory"], reject_hardlinks=True):
                        raise ValueError("继续记录路径不安全。")
                    with store_lock(prepared["directory"] / ".continue.lock"):
                        if path.exists():
                            prior = json.loads(path.read_text(encoding="utf-8"))
                            existing = manager._runs.get(prior.get("runId"))
                            if existing is not None:
                                return {"run": {**existing.public(), "scope": body.scope}}
                            raise HTTPException(409, "这份进度已经发起过继续任务，请查看会话记录；不会重复执行。")
                        if manager.session_is_active(body.session_id):
                            raise HTTPException(409, "当前会话仍在运行，请结束后继续。")
                        atomic_write_json(path, {"status": "preparing"})
                        record = manager.start(workspace, prepared["prompt"], body.session_id,
                            prepared["profile"], task=prepared["task"], run_budget=body.run_budget,
                            permission_mode=body.permission_mode,
                            continuation=prepared["selection"],
                            execution_location="isolated",
                            **({"context_settings": body.context_settings} if body.context_settings is not None else {}))
                        atomic_write_json(path, {"status": "started", "runId": record.run_id})
                        return {"run": {**record.public(), "scope": body.scope}}
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                raise HTTPException(409, str(error)) from error
        start_settings = dict(
            permission_mode=body.permission_mode,
            execution_location=(body.execution_location
                if body.scope == "project" or "execution_location" in body.model_fields_set else None),
            **({"task": task} if task else {}),
            **({"run_budget": body.run_budget} if isinstance(body, BudgetedRunCreate) else {}),
            **({"context_settings": body.context_settings} if body.context_settings is not None else {}),
        )
        record = manager.resume_queue(body.resume_queue_from, workspace, body.session_id, **start_settings) if body.resume_queue_from else manager.start(
            workspace,
            body.prompt,
            body.session_id,
            body.execution_profile,
            **start_settings,
        )
        _set_session_scope(record.session_id, body.scope)
        return {"run": {**record.public(), "scope": body.scope}}

    @app.post("/api/runs/budgeted")
    def budgeted_runs_post(body: BudgetedRunCreate) -> dict[str, Any]:
        # A dedicated route fails on old backends instead of silently ignoring
        # a new optional JSON field and starting an unbounded task.
        return runs_post(body)

    @app.get("/api/runs/{run_id}")
    def run_get(
        run_id: str, after: int = Query(default=0, ge=0)
    ) -> dict[str, Any]:
        record = manager.get(run_id)
        return record.snapshot(after=after)

    @app.get("/api/runs/{run_id}/changes")
    def run_changes(run_id: str, turn_id: str = Query(alias="turnId")) -> dict[str, Any]:
        record = manager.get(run_id)
        with record.condition:
            changes = record.file_changes
            if changes is not None and changes.get("turnId") == turn_id:
                return {"changes": project_file_changes(changes)}
        from minicode.session_history import turn_file_changes
        current = load_session(record.session_id)
        if current is None:
            raise HTTPException(404, "会话不存在。")
        return {"changes": _safe_payload(turn_file_changes(current, turn_id))}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str, after: int = Query(default=0, ge=0)):
        record = manager.get(run_id)

        async def stream() -> AsyncIterator[str]:
            nonlocal record
            cursor = after
            heartbeat_at = time.monotonic()
            ownership_checked_at = 0.0
            while True:
                if await request.is_disconnected():
                    break
                if record.restored and record.status not in _TERMINAL_STATUSES and time.monotonic() - ownership_checked_at >= 1:
                    record = await asyncio.to_thread(manager.get, run_id)
                    ownership_checked_at = time.monotonic()
                with record.condition:
                    pending = list(record._events_after_locked(cursor))
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

    @app.post("/api/runs/{run_id}/agents/stop")
    def agent_stop(run_id: str, body: AgentStopRequest) -> dict[str, Any]:
        return manager.stop_agent(run_id, body)

    @app.post("/api/runs/{run_id}/approvals")
    def run_approval(run_id: str, body: ApprovalResponse) -> dict[str, Any]:
        return {"run": manager.approve(run_id, body).public()}

    @app.post("/api/runs/{run_id}/questions")
    def answer_run_question(run_id: str, body: QuestionResponse) -> dict[str, Any]:
        return {"run": manager.answer_question(run_id, body).public()}

    @app.post("/api/runs/{run_id}/question-groups/{group_id}")
    def answer_run_question_group(run_id: str, group_id: str, body: QuestionGroupResponse) -> dict[str, Any]:
        return {"run": manager.answer_question_group(run_id, group_id, body).public()}

    @app.post("/api/runs/{run_id}/inputs")
    def run_input(run_id: str, body: RunInputCreate) -> dict[str, Any]:
        return {"input": manager.add_input(run_id, body)}

    @app.delete("/api/runs/{run_id}/inputs/{input_id}")
    def cancel_run_input(run_id: str, input_id: str) -> dict[str, Any]:
        return {"input": manager.cancel_input(run_id, input_id)}

    @app.post("/api/runs/{run_id}/inputs/{input_id}/steer")
    def steer_run_input(run_id: str, input_id: str) -> dict[str, Any]:
        return {"input": manager.steer_input(run_id, input_id)}

    @app.get("/api/workspace/status")
    def workspace_status(path: str = Query(min_length=1, max_length=4_096)) -> dict[str, Any]:
        return _workspace_status(_resolve_workspace(path))

    @app.get("/api/workspace/capabilities")
    def workspace_capabilities(
        path: str | None = Query(default=None, max_length=4_096),
        scope: Literal["project", "standalone"] = Query(default="project"),
    ) -> dict[str, Any]:
        if scope == "standalone":
            workspace = _standalone_workspace()
        elif path:
            workspace = _resolve_workspace(path)
        else:
            raise HTTPException(status_code=400, detail="项目能力快照需要工作区路径。")
        return {
            "capabilities": _workspace_capabilities(
                workspace,
                source="standalone" if scope == "standalone" else "workspace",
            )
        }

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

    @app.get("/api/workspace/image")
    def workspace_image(path: str = Query(min_length=1, max_length=4096),
                        file: str = Query(min_length=1, max_length=4096), download: bool = False):
        from minicode.run_images import read_image_file, inspect_image
        from urllib.parse import quote
        target = _resolve_workspace_member(_resolve_workspace(path), file)
        try:
            data = read_image_file(target)
            metadata = inspect_image(data)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=415, detail=str(error)) from error
        disposition = "attachment" if download else "inline"
        return Response(data, media_type=metadata["mediaType"], headers={
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(target.name, safe='')}",
        })

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
    prefer_project_python(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Start the local MiniCode browser workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", default=str(Path.cwd()))
    parser.add_argument("--frontend", default=None)
    parser.add_argument("--web-agent-teams", action="store_true", help="Default Ultra Teams on until the user saves Web collaboration preferences")
    parser.add_argument("--web-agent-team-writes", action="store_true", help="Default Teams isolated writes on; requires --web-agent-teams; saved Web preferences take precedence")
    parser.add_argument("--web-primary-role", choices=("worker", "coordinator"), default="worker", help="Default Teams role until the user saves Web collaboration preferences")
    parser.add_argument("--web-runtime", choices=("legacy", "primary"), default="primary",
                        help="Web-only Standard/Deep rollout (default: primary); use legacy for explicit rollback.")
    parser.add_argument("--web-ultra-runtime", choices=("legacy", "primary"), default="primary",
                        help="Independent Web Ultra rollout (default: primary); use legacy for explicit rollback.")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check the Python environment without starting or changing storage")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
        parser.error("非本机绑定必须显式传入 --allow-remote。")
    report = startup_report(args.workspace)
    if args.check:
        print(json.dumps(report, ensure_ascii=False))
        raise SystemExit(0 if report["ok"] else 1)
    if not report["ok"]:
        missing = ", ".join(name for name, ready in report["dependencies"].items() if not ready)
        parser.error(f"缺少或无法加载依赖：{missing}。请在项目 .venv 中安装 .[web,memory] 后重试。")
    from minicode.config import MINI_CODE_DIR
    from minicode.store_lock import store_lock
    try:
        # Different ports must not bypass the shared session-store ownership.
        with store_lock(MINI_CODE_DIR / ".web-server.lock", timeout=0):
            uvicorn.run(
                create_app(
                    default_workspace=args.workspace,
                    frontend_dir=args.frontend,
                    allow_remote=args.allow_remote,
                    web_runtime=args.web_runtime,
                    web_ultra_runtime=args.web_ultra_runtime,
                    web_agent_teams=args.web_agent_teams,
                    web_agent_team_writes=args.web_agent_team_writes,
                    web_primary_role=args.web_primary_role,
                ),
                host=args.host,
                port=args.port,
                log_level="info",
            )
    except TimeoutError:
        parser.error("当前用户已有 MiniCode Web 后端运行。请使用现有入口，或停止旧服务后再启动；不要同时运行两个端口。")


if __name__ == "__main__":
    main()
