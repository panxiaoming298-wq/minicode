"""Native tools for application-managed services; approvals remain command-scoped."""
from __future__ import annotations

import json
from pathlib import Path
import threading
from weakref import WeakKeyDictionary

from minicode.development_services import DevelopmentServiceManager, ready_address
from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.tooling import ToolCapability, ToolDefinition, ToolMetadata, ToolResult
from minicode.tools._validation import validate_contract
from minicode.tools.run_command import _build_execution_command, _is_allowed_command
from minicode.workspace import resolve_tool_path

START_INPUT = {"type": "object", "additionalProperties": False, "properties": {
    "command": {"type": "string", "minLength": 1, "maxLength": 4096,
                "description": "Executable only, e.g. npm; put run/dev in args. No background shell operators."},
    "args": {"type": ["array", "null"], "maxItems": 128, "items": {"type": "string", "maxLength": 4096}},
    "cwd": {"type": ["string", "null"], "maxLength": 4096},
    "ready_url": {"type": "string", "minLength": 1, "maxLength": 4096,
                  "description": "Expected local HTTP URL, e.g. http://127.0.0.1:5173/; must belong to this service."},
    "startup_timeout": {"type": ["integer", "null"], "minimum": 1, "maximum": 600,
                        "description": "Seconds to wait for readiness, default 30. Does NOT limit service lifetime."},
}, "required": ["command", "ready_url"]}
STATUS_INPUT = {"type": "object", "additionalProperties": False, "properties": {
    "service_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 128,
                   "description": "Omit to list the current project's services."},
}}
CONTROL_INPUT = {"type": "object", "additionalProperties": False, "properties": {
    "service_id": {"type": "string", "minLength": 1, "maxLength": 128},
}, "required": ["service_id"]}
_LOCAL_MANAGER = None
_LOCAL_LOCK = threading.Lock()
_NATIVE_ACTIONS = WeakKeyDictionary()


def is_native_service_tool(tool: ToolDefinition | None, action: str | None = None) -> bool:
    if tool is None:
        return False
    names = {"start": "start_service", "status": "service_status", "logs": "service_logs", "stop": "stop_service"}
    try:
        native_action = _NATIVE_ACTIONS.get(tool.run)
    except TypeError:
        return False
    return native_action is not None and tool.name == names[native_action] and (action is None or native_action == action)


def validate_start(value: dict) -> dict:
    validate_contract(value, START_INPUT)
    command = value["command"].strip()
    args = value.get("args") or []
    if not command or any("\x00" in item or "\n" in item or "\r" in item for item in [command, *args]):
        raise ValueError("启动服务需要完整可执行文件名和不含换行/NUL 的参数")
    if any(token in command for token in ("|", "&", ";", "<", ">")):
        raise ValueError("请将可执行文件和 args 分开；启动服务不接受后台 shell 片段")
    if any(character.isspace() for character in command) and not Path(command).is_file():
        raise ValueError("command 只填写可执行文件，例如 npm；args 填写 ['run', 'dev']")
    return {"command": command, "args": args, "cwd": value.get("cwd"),
            "ready_url": ready_address(value["ready_url"])[0], "startup_timeout": value.get("startup_timeout") or 30}


def validate_status(value: dict) -> dict:
    validate_contract(value, STATUS_INPUT)
    return dict(value)


def validate_control(value: dict) -> dict:
    validate_contract(value, CONTROL_INPUT)
    return dict(value)


def local_manager() -> DevelopmentServiceManager:
    global _LOCAL_MANAGER
    with _LOCAL_LOCK:
        if _LOCAL_MANAGER is None:
            import os
            from minicode.config import MINI_CODE_DIR
            _LOCAL_MANAGER = DevelopmentServiceManager(MINI_CODE_DIR / "development-services" / f"cli-{os.getpid()}")
        return _LOCAL_MANAGER


def create_service_tools(gateway=None) -> list[ToolDefinition]:
    def runner(action):
        def run(data, context):
            cancelled = lambda: context.cancellation_event is not None and context.cancellation_event.is_set()
            if cancelled():
                return ToolResult(ok=False, output="服务操作已取消。", failureKind="canceled")
            payload = dict(data)
            if action == "start":
                cwd = str(resolve_tool_path(context, payload["cwd"], "command_cwd")) if payload.get("cwd") else context.cwd
                executable, args = _build_execution_command(payload["command"], payload["command"], payload["args"],
                                                            use_shell=False, background_shell=False)
                if context.permissions is not None:
                    reason = None if _is_allowed_command(payload["command"]) else "启动未列入内置开发集合的服务命令"
                    context.permissions.ensure_command(executable, args, cwd, force_prompt_reason=reason)
                payload["cwd"] = cwd
            if cancelled():
                return ToolResult(ok=False, output="服务操作已取消。", failureKind="canceled")
            try:
                result = (gateway(action, payload, context) if gateway is not None
                          else local_manager().invoke(action, payload, workspace=context.cwd,
                              session_id=getattr(context.session, "session_id", None), cancelled=cancelled))
            except (ValueError, OSError, RuntimeError) as error:
                return ToolResult(ok=False, output=str(error),
                                  failureKind="canceled" if cancelled() else "dependency_pending")
            successful = action != "start" or result.get("service", {}).get("status") == "running"
            return ToolResult(ok=successful, output=json.dumps(result, ensure_ascii=False),
                              failureKind="canceled" if cancelled() and not successful else None)
        _NATIVE_ACTIONS[run] = action
        return run

    definitions = [
        ("start_service", "Start or reuse a managed long-running development server. Returns after its owned local HTTP endpoint is ready. Service survives this agent turn; use service_status/service_logs/stop_service to manage it. Use run_command for finite builds/tests, never detached shell workarounds.", START_INPUT, validate_start, "start", ToolEffect.PRIVILEGED),
        ("service_status", "Inspect a managed service or list this project's services; includes URL, process status and errors.", STATUS_INPUT, validate_status, "status", ToolEffect.READ_ONLY),
        ("service_logs", "Read the bounded log tail of a managed development service.", CONTROL_INPUT, validate_control, "logs", ToolEffect.READ_ONLY),
        ("stop_service", "Stop only the identified application-owned development service and its children. Never terminates an unrelated process using its port.", CONTROL_INPUT, validate_control, "stop", ToolEffect.PRIVILEGED),
    ]
    return [ToolDefinition(name=name, description=description, input_schema=schema, validator=validator, run=runner(action),
                metadata=ToolMetadata(name=name, description=description, input_schema=schema, effect=effect,
                    capabilities={ToolCapability.READ_ONLY, ToolCapability.CONCURRENCY_SAFE} if effect is ToolEffect.READ_ONLY else {ToolCapability.REQUIRES_PERMISSION},
                    replay_policy=ReplayPolicy.SAFE if effect is ToolEffect.READ_ONLY else ReplayPolicy.NEVER_AUTO_RETRY))
            for name, description, schema, validator, action, effect in definitions]
