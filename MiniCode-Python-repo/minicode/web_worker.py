"""Isolated worker process used by the local MiniCode web bridge.

The worker speaks newline-delimited JSON over stdin/stdout.  Keeping model
execution in a child process gives the web UI a real stop boundary and keeps
provider credentials out of browser memory and command-line arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from minicode.agent_loop import run_agent_turn
from minicode.config import load_runtime_config
from minicode.context_manager import ContextManager
from minicode.memory import MemoryManager
from minicode.model_registry import create_model_adapter
from minicode.permissions import PermissionManager
from minicode.prompt import build_system_prompt_bundle
from minicode.release_readiness import redact_sensitive_payload
from minicode.session import load_session, save_session
from minicode.state import create_app_store
from minicode.tools import create_default_tool_registry
from minicode.turn_events import TurnEvent


_MAX_EVENT_TEXT = 60_000


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    try:
        value = redact_sensitive_payload(value)
    except Exception:  # noqa: BLE001
        pass
    if isinstance(value, str) and len(value) > _MAX_EVENT_TEXT:
        omitted = len(value) - _MAX_EVENT_TEXT
        return f"{value[:_MAX_EVENT_TEXT]}\n\n[output truncated: {omitted} characters omitted]"
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _emit(payload: dict[str, Any]) -> None:
    safe_payload = _json_safe(payload)
    sys.stdout.write(json.dumps(safe_payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


class WebEventSink:
    """Convert core turn events into the compact public web event schema."""

    include_stream_chunks = True
    # Never forward raw hidden-reasoning deltas to the browser.
    include_thinking_chunks = True

    def __init__(self) -> None:
        self._tool_started: dict[tuple[int | None, str], float] = {}
        self._thinking_steps: set[int | None] = set()

    def on_event(self, event: TurnEvent) -> None:
        if event.kind == "tool_start":
            self._tool_started[(event.step, event.tool_name)] = time.monotonic()
            _emit(
                {
                    "kind": "tool_start",
                    "step": event.step,
                    "toolName": event.tool_name,
                    "input": event.tool_input,
                }
            )
            return

        if event.kind == "tool_result":
            started = self._tool_started.pop((event.step, event.tool_name), None)
            duration_ms = round((time.monotonic() - started) * 1000) if started else None
            _emit(
                {
                    "kind": "tool_result",
                    "step": event.step,
                    "toolName": event.tool_name,
                    "output": event.output,
                    "isError": event.is_error,
                    "durationMs": duration_ms,
                }
            )
            return

        if event.kind == "assistant":
            _emit({"kind": "assistant", "step": event.step, "content": event.content})
            return

        if event.kind == "assistant_stream":
            _emit({"kind": "assistant_stream", "step": event.step, "content": event.content})
            return

        if event.kind == "thinking":
            if event.step not in self._thinking_steps:
                self._thinking_steps.add(event.step)
                _emit(
                    {
                        "kind": "thinking_status",
                        "step": event.step,
                        "content": "正在分析任务和工作区上下文",
                    }
                )
            return

        if event.kind == "progress":
            _emit({"kind": "progress", "step": event.step, "content": event.content})
            return

        if event.kind == "runtime":
            runtime_event = asdict(event.runtime_event) if event.runtime_event else None
            _emit(
                {
                    "kind": "runtime",
                    "step": event.step,
                    "content": event.content,
                    "stopReason": event.stop_reason,
                    "runtime": runtime_event,
                }
            )
            return

        if event.kind == "done":
            _emit(
                {
                    "kind": "done",
                    "step": event.step,
                    "stopReason": event.stop_reason,
                }
            )


def _approval_prompt(request: dict[str, Any]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    _emit(
        {
            "kind": "approval_requested",
            "requestId": request_id,
            "request": request,
        }
    )
    while True:
        line = sys.stdin.readline()
        if not line:
            return {"decision": "deny_once", "feedback": "Web client disconnected."}
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            continue
        if response.get("kind") != "approval_response":
            continue
        if response.get("requestId") != request_id:
            continue
        decision = str(response.get("decision", "deny_once"))
        result = {"decision": decision}
        feedback = str(response.get("feedback", "")).strip()
        if feedback:
            result["feedback"] = feedback
        _emit(
            {
                "kind": "approval_resolved",
                "requestId": request_id,
                "decision": decision,
            }
        )
        return result


def _dataclass_or_value(value: Any) -> Any:
    return asdict(value) if is_dataclass(value) else value


def _run(workspace: str, session_id: str, prompt: str) -> int:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        _emit({"kind": "error", "message": f"工作区不存在: {root}"})
        return 2

    session = load_session(session_id)
    if session is None:
        _emit({"kind": "error", "message": "会话不存在或已被删除。"})
        return 2
    if Path(session.workspace).resolve() != root:
        _emit({"kind": "error", "message": "会话与工作区不匹配。"})
        return 2

    runtime: dict[str, Any] | None
    try:
        runtime = load_runtime_config(root)
    except Exception:
        runtime = None

    force_mock = os.environ.get("MINI_CODE_WEB_FORCE_MOCK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    } or runtime is None

    tools = create_default_tool_registry(str(root), runtime=runtime)
    permissions = PermissionManager(str(root), prompt=_approval_prompt)
    memory_manager = MemoryManager(project_root=root)
    context_manager = (
        ContextManager(model=str(runtime.get("model", "default"))) if runtime else None
    )
    model_name = str(runtime.get("model", "mock")) if runtime else "mock"
    model = create_model_adapter(
        model=model_name,
        tools=tools,
        runtime=runtime,
        force_mock=force_mock,
    )
    store = create_app_store(
        initial={
            "session_id": session.session_id,
            "workspace": str(root),
            "model": model_name,
        }
    )

    prompt_bundle = build_system_prompt_bundle(
        str(root),
        permissions.get_summary(),
        {
            "skills": tools.get_skills(),
            "mcpServers": tools.get_mcp_servers(),
            "memory_context": memory_manager.get_relevant_context(query=prompt),
            "runtime": runtime,
        },
    )
    messages = list(session.messages)
    system_message = {"role": "system", "content": prompt_bundle.prompt}
    if messages and messages[0].get("role") == "system":
        messages[0] = system_message
    else:
        messages.insert(0, system_message)
    messages.append({"role": "user", "content": prompt})

    session.messages = messages
    session.history.append(prompt)
    session.metadata.model = model_name
    session.metadata.status = "running"
    save_session(session, force_full=True)
    _emit(
        {
            "kind": "run_started",
            "sessionId": session.session_id,
            "model": model_name,
            "provider": str((runtime or {}).get("provider", "mock")) or "mock",
            "mock": force_mock,
        }
    )

    permissions.begin_turn()
    try:
        result_messages = run_agent_turn(
            model=model,
            tools=tools,
            messages=messages,
            cwd=str(root),
            permissions=permissions,
            session=session,
            store=store,
            context_manager=context_manager,
            memory_manager=memory_manager,
            runtime=runtime,
            system_prompt=prompt_bundle.prompt,
            callbacks=WebEventSink(),
        )
        session.messages = result_messages
        session.permissions_summary = {"items": permissions.get_summary()}
        session.skills = tools.get_skills()
        session.mcp_servers = tools.get_mcp_servers()
        session.instruction_layers = prompt_bundle.instruction_layers
        session.hook_status = _dataclass_or_value(prompt_bundle.hook_status)
        session.delegation_status = _dataclass_or_value(prompt_bundle.delegation_status)
        session.extension_manifests = prompt_bundle.extension_manifests
        session.readiness_report = _dataclass_or_value(prompt_bundle.readiness_report)
        session.metadata.status = "completed"
        save_session(session, force_full=True)
        return 0
    except KeyboardInterrupt:
        session.metadata.status = "stopped"
        save_session(session, force_full=True)
        return 130
    except Exception as exc:  # noqa: BLE001
        session.metadata.status = "failed"
        save_session(session, force_full=True)
        _emit({"kind": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        permissions.end_turn()
        try:
            tools.dispose()
        except Exception:  # noqa: BLE001
            pass


def _read_start_message() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or value.get("kind") != "start":
        return None
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MiniCode local web worker")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args(argv)

    message = _read_start_message()
    prompt = str((message or {}).get("prompt", "")).strip()
    if not prompt:
        _emit({"kind": "error", "message": "请输入任务内容。"})
        raise SystemExit(2)
    raise SystemExit(_run(args.workspace, args.session, prompt))


if __name__ == "__main__":
    main()
