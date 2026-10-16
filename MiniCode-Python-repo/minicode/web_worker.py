"""Isolated worker process used by the local MiniCode web bridge.

The worker speaks newline-delimited JSON over stdin/stdout.  Keeping model
execution in a child process gives the web UI a real stop boundary and keeps
provider credentials out of browser memory and command-line arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import sys
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast

from minicode.config import load_runtime_config
from minicode.context_manager import ContextManager
from minicode.memory import MemoryManager
from minicode.model_registry import create_model_adapter
from minicode.permissions import PermissionManager, PermissionMode, approval_cancelled
from minicode.product_surfaces import build_product_snapshot
from minicode.prompt import build_system_prompt_bundle
from minicode.release_readiness import redact_sensitive_payload
from minicode.runtime_delivery import entry_completion, runtime_failure_message
from minicode.entry_task import bind_task, restore_task
from minicode.session import load_session, save_session
from minicode.session_context import ConversationContext, load_context_settings
from minicode.session_history import begin_turn, finish_turn, project_binding
from minicode.session_contract import build_memory_continuity_snapshot
from minicode.state import create_app_store
from minicode.tools import create_default_tool_registry
from minicode.turn_events import TurnEvent
from minicode.unified_runtime import run_runtime_turn as run_agent_turn
from minicode.web_surfaces import capability_surface, public_approval_request
from minicode.web_runtime_settings import ExecutionLocation, RunBudget, WebRuntimePolicy, apply_execution_location
from minicode.web_run_probe import WebRunProbe
from minicode.turn_changes import TurnChangeTracker

_MAX_EVENT_TEXT = 60_000
_OUTPUT_LOCK = threading.Lock()


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(cast(Any, value))
    try:
        value = redact_sensitive_payload(value)
    except Exception:  # noqa: BLE001 - event transport must fail closed.
        if isinstance(value, dict):
            return {
                "kind": "error",
                "message": "Sensitive event omitted because redaction failed.",
            }
        return "[REDACTION_FAILED]"
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
    with _OUTPUT_LOCK:
        sys.stdout.write(json.dumps(safe_payload, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()


def _emit_process(payload: dict[str, Any], transcript: list[dict] | None) -> None:
    if transcript is not None:
        message = {"role": "run_event", "event": deepcopy(_json_safe(payload))}
        # Keep the primary delivery at the end of the saved turn. Failure
        # details belong to its expandable work transcript.
        if (payload.get("kind") == "error" and transcript
                and transcript[-1].get("role") == "assistant"
                and transcript[-1].get("phase") != "commentary"):
            transcript.insert(len(transcript) - 1, message)
        else:
            transcript.append(message)
    _emit(payload)


class WorkerInbox:
    """One stdin reader routes replies to concurrent approvals and checkpoints."""

    def __init__(self, *, web_run_id: str | None = None, live_inputs: bool = False,
                 question_protocol: int = 0) -> None:
        self.condition = threading.Condition()
        self.responses: dict[str, dict[str, Any]] = {}
        self.closed = False
        self.agent_scheduler = None
        self.pending_run_stop = None
        self.web_run_id = web_run_id
        self.live_inputs = live_inputs
        self.question_protocol = question_protocol
        self.user_controls = None
        self.pending_user_inputs: dict[str, dict] = {}
        self.user_poll_lock = threading.Lock()
        self.user_event_sink = None
        self.displayed_user_inputs: set[str] = set()

    def bind_user_controls(self, scheduler, worker, node_id) -> None:
        from minicode.runtime_steering import HumanInputState
        scheduler.reducer.enable_user_steering(node_id)
        worker.user_steering_transport = self
        worker.user_steering_scheduler = scheduler
        with self.condition:
            self.user_controls = (scheduler, HumanInputState(scheduler.reducer))
            pending = list(self.pending_user_inputs.values())
            self.pending_user_inputs.clear()
        for item in pending:
            self.control_user_input({"action": "queue", "runId": self.web_run_id, "input": item})
        _emit({"kind": "input_capabilities", "runId": self.web_run_id,
               "acceptingSteering": not self.user_controls[1].state["sealed"]})

    def control_user_input(self, value: dict) -> dict:
        if not self.live_inputs or value.get("runId") != self.web_run_id:
            raise ValueError("human input does not belong to this Web task")
        action = value.get("action")
        if action not in {"queue", "cancel"}:
            raise ValueError("unknown human input control")
        with self.condition:
            controls = self.user_controls
            if controls is None:
                if action == "queue":
                    item = value["input"]
                    key = item["inputId"]
                    existing = self.pending_user_inputs.get(key)
                    if existing is not None and existing["text"] != item["text"]:
                        raise ValueError("human input identity conflicts")
                    if existing is None and len(self.pending_user_inputs) >= 8:
                        raise ValueError("pending human input quota exhausted")
                    self.pending_user_inputs[key] = dict(item)
                    return dict(item)
                item = self.pending_user_inputs.pop(value["inputId"], None)
                if item is None:
                    raise ValueError("human input is not pending")
                return {**item, "status": "canceled"}
        scheduler, state = controls
        if action == "queue":
            record = state.queue(value["input"])
            with scheduler._dispatch_lock:
                scheduler.reducer.wake_for_user_input()
                scheduler._dispatch_wake.set()
            if scheduler.on_activity:
                scheduler.on_activity()
            return record
        return state.cancel(value["inputId"])

    def poll_user_inputs(self, invocation, *, final=False) -> None:
        from minicode.runtime_steering import HumanInputState
        with self.user_poll_lock:
            if self.user_controls is None or self.user_controls[0].reducer.run_id != invocation.run_id:
                raise ValueError("human input checkpoint has no bound Runtime")
            request_id = uuid.uuid4().hex
            _emit({"kind": "user_input_checkpoint", "requestId": request_id,
                   "runId": self.web_run_id, "final": final})
            response = self.receive(request_id,
                cancelled=self.user_controls[0].cancellation_event.is_set)
            if response.get("error") or not response and not self.user_controls[0].cancellation_event.is_set():
                raise RuntimeError(response.get("error") or "human input transport closed")
            state = HumanInputState(self.user_controls[0].reducer)
            for item in response.get("inputs", []):
                record = state.queue(item)
                if record["status"] == "canceled":
                    self.publish_user_input(record)

    def publish_user_input(self, record: dict) -> None:
        if record["status"] == "applied" and record["inputId"] not in self.displayed_user_inputs:
            self.displayed_user_inputs.add(record["inputId"])
            if self.user_event_sink is not None:
                self.user_event_sink.display_messages.append({"role": "user", "content": record["text"],
                    "_userInputId": record["inputId"]})
        _emit({"kind": "run_input", "runInput": record})

    def publish_runtime(self, runtime: dict) -> None:
        _emit_process({"kind": "runtime", "runtime": runtime},
                      self.user_event_sink.display_messages if self.user_event_sink is not None else None)

    def bind_agent_controls(self, scheduler) -> None:
        with self.condition:
            self.agent_scheduler = scheduler
            pending = self.pending_run_stop
            self.pending_run_stop = None
        if pending is not None:
            self.control_run(pending)
        if self.closed:
            scheduler.cancel("Web host disconnected before runtime controls were bound")

    def control_run(self, value: dict) -> dict:
        execution_id = value.get("executionId")
        if value.get("action") != "cancel" or not isinstance(execution_id, str) or not execution_id:
            raise ValueError("Run control requires a registered execution identity and cancel action")
        with self.condition:
            scheduler = self.agent_scheduler
            if scheduler is None:
                # A stop during assembly must reach the scheduler before its
                # first admission, rather than being lost or spawning a model.
                self.pending_run_stop = dict(value)
                return {"status": "queued", "executionId": execution_id}
        if scheduler.reducer.run_id != execution_id:
            raise ValueError("Run control does not match the current runtime")
        if not scheduler.cancellation_event.is_set():
            scheduler.cancel("user stopped the run from the Web control surface")
        scheduler._dispatch_wake.set()
        if scheduler.on_activity:
            scheduler.on_activity()
        return {"status": "cancel_requested", "executionId": execution_id}

    def control_agent(self, value: dict) -> dict:
        scheduler = self.agent_scheduler
        task_id = value.get("taskId")
        if scheduler is None or scheduler.reducer.run_id != value.get("executionId"):
            raise ValueError("Agent control does not match the current runtime")
        snapshot = scheduler.reducer.snapshot()
        if (not isinstance(task_id, str) or snapshot.get("delegation_parents", {}).get(task_id) != "primary"
            or not (task_id in snapshot.get("agent_sessions", {}) or task_id.startswith("builder-"))
            or snapshot.get("canceled") or snapshot.get("paused")
            or scheduler.reducer.terminal()):
            raise ValueError("Agent control requires a current child in this active run")
        operation_id = value.get("operationId")
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
            raise ValueError("Agent cancellation requires a bounded operation id")
        result = scheduler.cancel_task(task_id=task_id, operation_id="web:" + operation_id,
            reason="user stopped this agent from the Web control surface")
        if scheduler.on_activity:
            scheduler.on_activity()
        return result

    def start(self) -> None:
        threading.Thread(target=self._read, daemon=True, name="web-inbox").start()

    def _read(self) -> None:
        try:
            for line in sys.stdin:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("kind") == "run_control":
                    try:
                        receipt = self.control_run(value)
                        self.publish_runtime({"category": "run_control", "message": json.dumps(receipt)})
                    except (ValueError, RuntimeError, KeyError) as error:
                        self.publish_runtime({"category": "run_control", "message": f"RunControlRejected: {error}"})
                    continue
                if isinstance(value, dict) and value.get("kind") == "user_input_control":
                    try:
                        receipt = self.control_user_input(value)
                        if value.get("requestId"):
                            _emit({"kind": "user_input_receipt", "requestId": value["requestId"], "runInput": receipt})
                    except (ValueError, RuntimeError, KeyError, OSError) as error:
                        if value.get("requestId"):
                            _emit({"kind": "user_input_receipt", "requestId": value["requestId"], "error": str(error)})
                        elif isinstance(value.get("input"), dict):
                            self.publish_user_input({**value["input"], "status": "held", "reason": str(error)})
                    continue
                if isinstance(value, dict) and value.get("kind") == "agent_control":
                    try:
                        receipt = self.control_agent(value)
                        self.publish_runtime({"category": "agent_control", "message": json.dumps(receipt, ensure_ascii=False)})
                    except (ValueError, RuntimeError, KeyError) as error:
                        self.publish_runtime({"category": "agent_control", "message": f"AgentControlRejected: {error}"})
                    continue
                if isinstance(value, dict) and value.get("kind") == "question_responses":
                    with self.condition:
                        for answer in value.get("answers", []):
                            if isinstance(answer, dict) and answer.get("requestId"):
                                self.responses[str(answer["requestId"])] = {**answer, "kind": "question_response"}
                        self.condition.notify_all()
                    continue
                if isinstance(value, dict) and value.get("requestId"):
                    with self.condition:
                        self.responses[str(value["requestId"])] = value
                        self.condition.notify_all()
        finally:
            with self.condition:
                self.closed = True
                self.condition.notify_all()
                scheduler = self.agent_scheduler
            if scheduler is not None:
                scheduler.cancel("Web host disconnected")

    def receive(self, request_id: str, *, cancelled=None, timeout=None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self.condition:
            while request_id not in self.responses and not self.closed:
                if cancelled is not None and cancelled():
                    return {}
                if deadline is not None and time.monotonic() >= deadline:
                    return {}
                self.condition.wait(timeout=0.05 if cancelled is not None or deadline is not None else None)
            return self.responses.pop(request_id, {})

    def ask_user(self, request: dict, context) -> str | list[str] | None:
        """Keep the original tool call alive; browser reconnects do not cancel it."""
        questions = request.get("questions", [request])
        group_id = uuid.uuid4().hex if "questions" in request else None
        requests = [(uuid.uuid4().hex, question) for question in questions]
        sink = self.user_event_sink
        transcript = sink.display_messages if sink is not None else None
        identity = {"executionId": context.run_id, "nodeId": context.node_id,
                    "attemptId": context.attempt_id, "toolCallId": context.tool_call_id,
                    "turnId": sink.turn_id if sink is not None else ""}
        canceled = lambda: context.cancellation_event is not None and context.cancellation_event.is_set()
        if canceled() or self.closed:
            return None
        group = {"questionGroupId": group_id, "questionCount": len(questions)} if group_id else {}
        for index, (request_id, question) in enumerate(requests):
            _emit_process({"kind": "question_requested", "requestId": request_id,
                           "question": question, **identity, **group,
                           **({"questionIndex": index + 1} if group_id else {})}, transcript)
        pending = dict(requests)
        answers = {}
        try:
            while pending and not canceled():
                with self.condition:
                    ready = [(key, self.responses.pop(key)) for key in pending if key in self.responses]
                    if not ready and not self.closed:
                        self.condition.wait(timeout=0.05)
                        continue
                if self.closed or canceled():
                    break
                for request_id, response in ready:
                    value = response.get("answer")
                    if response.get("kind") != "question_response" or not isinstance(value, str) or not value.strip() or len(value) > 12_000:
                        continue
                    answers[request_id] = value.strip()
                    pending.pop(request_id)
                    _emit_process({"kind": "question_resolved", "requestId": request_id,
                                   "resolution": "answered", "answer": answers[request_id], **identity, **group}, transcript)
            if pending:
                return None
            values = [answers[request_id] for request_id, _ in requests]
            return values if "questions" in request else values[0]
        finally:
            for request_id in pending:
                _emit_process({"kind": "question_resolved", "requestId": request_id,
                               "resolution": "canceled", "answer": None, **identity, **group}, transcript)

    def service_request(self, action: str, data: dict, context) -> dict:
        request_id = uuid.uuid4().hex
        canceled = lambda: context.cancellation_event is not None and context.cancellation_event.is_set()
        # Private effect request: preserve the already-approved argv exactly.
        # The host consumes this before public event retention/redaction; only
        # public service receipts/log views may be sanitized for display.
        with _OUTPUT_LOCK:
            sys.stdout.write(json.dumps({"kind": "service_request", "requestId": request_id,
                "action": action, "input": data}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        response = self.receive(request_id, cancelled=canceled)
        if canceled() or not response:
            _emit({"kind": "service_cancel", "requestId": request_id})
            if not response and not self.closed:
                response = self.receive(request_id, timeout=8)
            raise ValueError("开发服务操作已取消或后端连接已关闭；本次启动由后端清理。")
        if response.get("error"):
            raise ValueError(response["error"])
        _emit({"kind": "service_ack", "requestId": request_id})
        return response["result"]

    def checkpoint(self) -> dict[str, Any] | None:
        request_id = uuid.uuid4().hex
        _emit({"kind": "input_checkpoint", "requestId": request_id})
        return self.receive(request_id).get("input")


class WebEventSink:
    """Convert core turn events into the compact public web event schema."""

    include_stream_chunks = True
    include_message_lifecycle = True
    thread_safe_tool_events = True
    # Never forward raw hidden-reasoning deltas to the browser.
    include_thinking_chunks = True

    def __init__(self, *, defer_done: bool = False, turn_id: str = "", prompt: str = "") -> None:
        self._tool_started: dict[tuple[Any, ...], float] = {}
        self._thinking_announced = False
        self.execution_id = ""
        self.defer_done = defer_done
        self.turn_id = turn_id
        self.raw_tool_results: list[dict] = []
        self._command_results: dict[tuple[str, ...], dict] = {}
        self._stream_lock = threading.RLock()
        self._pending_stream: dict | None = None
        self._stream_timer: threading.Timer | None = None
        self._last_stream_emit = 0.0
        self.file_changes: TurnChangeTracker | None = None
        self.display_messages: list[dict] = [{"role": "user", "content": prompt}] if prompt else []

    def on_raw_tool_result(self, call, result, *, node_id="", attempt_id=""):
        command = {"commandResult": deepcopy(result.commandResult)} if result.commandResult is not None else {}
        if result.commandCheckReport is not None:
            command["commandCheckReport"] = deepcopy(result.commandCheckReport)
        if command:
            key = (self.execution_id, node_id, attempt_id, str(call.get("id", "")))
            with self._stream_lock:
                self._command_results[key] = command
        self.raw_tool_results.append({"nodeId": node_id, "toolUseId": str(call.get("id", "")),
                                      "attemptId": attempt_id, "executionId": self.execution_id,
                                      "toolName": call.get("toolName", ""), "input": deepcopy(call.get("input", {})),
                                      "output": str(result.output or ""), "isError": not result.ok, **command})

    def on_execution_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.flush_stream()
        self.execution_id = str(snapshot["executionId"])
        _emit({"kind": "execution_snapshot", "execution": snapshot})
        if self.file_changes is not None:
            self.file_changes.refresh()

    def close_pending_messages(self) -> None:
        self.flush_stream()
        for message in self.display_messages:
            if message.get("deliveryState") == "pending":
                self.on_event(TurnEvent(kind="assistant_status", message_id=message["messageId"],
                                       phase="final_answer", delivery_state="rejected",
                                       node_id=message.get("nodeId", ""), attempt_id=message.get("attemptId", "")))

    def flush_stream(self) -> None:
        with self._stream_lock:
            if self._stream_timer is not None:
                self._stream_timer.cancel()
                self._stream_timer = None
            if self._pending_stream is not None:
                payload, self._pending_stream = self._pending_stream, None
                _emit(payload)
                self._last_stream_emit = time.monotonic()

    def _emit_text(self, payload: dict) -> None:
        with self._stream_lock:
            if payload["kind"] != "assistant_stream":
                self.flush_stream()
                _emit(payload)
                return
            identity = {key: value for key, value in payload.items() if key != "content"}
            pending = self._pending_stream
            if pending is not None and identity != {key: value for key, value in pending.items() if key != "content"}:
                self.flush_stream()
                pending = None
            if pending is None:
                self._pending_stream = dict(payload)
            else:
                pending["content"] += payload["content"]
            if time.monotonic() - self._last_stream_emit >= 0.05 or len(self._pending_stream["content"]) >= 4096:
                self.flush_stream()
            elif self._stream_timer is None:
                self._stream_timer = threading.Timer(0.05, self.flush_stream)
                self._stream_timer.daemon = True
                self._stream_timer.start()

    def on_event(self, event: TurnEvent) -> None:
        text_identity = {key: value for key, value in {
            "messageId": event.message_id, "phase": event.phase,
            "deliveryState": event.delivery_state, "turnId": self.turn_id,
            "executionId": self.execution_id, "nodeId": event.node_id,
            "attemptId": event.attempt_id,
        }.items() if value}
        if event.kind in {"assistant", "assistant_start", "assistant_stream", "assistant_status"}:
            def same_owner(message):
                return all(message.get(key, "") == value for key, value in (
                    ("executionId", self.execution_id), ("nodeId", event.node_id), ("attemptId", event.attempt_id)))

            if event.replaces_message_id and event.replaces_message_id != event.message_id:
                self.display_messages[:] = [message for message in self.display_messages
                    if not (message.get("role") == "assistant" and message.get("phase") == "commentary"
                            and message.get("messageId") == event.replaces_message_id and same_owner(message))]
            message = next((item for item in self.display_messages
                            if event.message_id and item.get("role") == "assistant"
                            and item.get("messageId") == event.message_id and same_owner(item)), None)
            if (message is None and not event.node_id and not event.attempt_id
                    and event.kind in {"assistant", "assistant_status"} and event.phase == "final_answer"
                    and event.delivery_state in {"accepted", "rejected"}):
                # Older coordinators omitted ownership. A receipt can inherit
                # only one unambiguous primary draft, never another attempt.
                candidates = [item for item in self.display_messages if item.get("role") == "assistant"
                    and item.get("messageId") == event.message_id and item.get("executionId", "") == self.execution_id
                    and item.get("nodeId") in {"primary", "root"} and item.get("phase") == "final_answer"]
                if len(candidates) == 1:
                    message = candidates[0]
            if message is not None and message.get("deliveryState") == "accepted" and event.delivery_state != "accepted":
                return
            if message is None and event.kind != "assistant_status":
                message = {"role": "assistant", "content": ""}
                self.display_messages.append(message)
            if message is not None:
                message.update(text_identity)
                if event.kind == "assistant_stream":
                    message["content"] += event.content
                elif event.kind != "assistant_status":
                    message["content"] = event.content
            self._emit_text({"kind": event.kind, "step": event.step, "content": event.content, **text_identity,
                   **({"replacesMessageId": event.replaces_message_id} if event.replaces_message_id else {})})
            return
        self.flush_stream()
        identity = {"nodeId": event.node_id, "attemptId": event.attempt_id,
                    "toolCallId": event.tool_call_id, "executionId": self.execution_id}
        identity = {key: value for key, value in identity.items() if value}
        if event.kind == "tool_rejected":
            # An admission receipt has no tool_start: do not invent an
            # execution or silently lose the proposal from saved history.
            recovery = {"toolRecovery": deepcopy(event.tool_recovery)} if event.tool_recovery else {}
            self.display_messages.extend([
                {"role": "assistant_tool_call", "toolName": event.tool_name,
                 "toolUseId": event.tool_call_id, "input": deepcopy(event.tool_input), **identity},
                {"role": "tool_result", "toolName": event.tool_name, "toolUseId": event.tool_call_id,
                 "content": event.output, "isError": True, **recovery, **identity},
            ])
            _emit({"kind": "tool_rejected", "step": event.step, "toolName": event.tool_name,
                   "input": event.tool_input, "output": event.output, "isError": True,
                   "content": event.content, **recovery, **identity})
            return
        if event.kind == "tool_images" and event.images:
            images = [dict(image) for image in event.images]
            self.display_messages.append({"role": "tool_image", "toolName": event.tool_name,
                "toolUseId": event.tool_call_id, "images": images, **identity})
            _emit({"kind": "tool_images", "step": event.step, "toolName": event.tool_name,
                   "images": images, "turnId": self.turn_id, **identity})
            return
        if event.kind == "tool_recovery" and event.tool_call_id and event.tool_recovery is not None:
            for message in reversed(self.display_messages):
                if (message.get("role") in {"tool_result", "assistant_tool_call"}
                        and message.get("toolUseId") == event.tool_call_id
                        and message.get("nodeId", "") == event.node_id
                        and message.get("attemptId", "") == event.attempt_id
                        and message.get("executionId", "") == self.execution_id):
                    message["toolRecovery"] = deepcopy(event.tool_recovery)
                    break
            _emit({"kind": "tool_recovery", "step": event.step, "toolName": event.tool_name,
                   "toolRecovery": event.tool_recovery, **identity})
            return
        if event.kind == "tool_input" and event.tool_call_id and event.tool_input_status is not None:
            # Native follow-up metadata annotates an existing receipt; it never
            # rewrites the original result or changes whether it succeeded.
            for message in reversed(self.display_messages):
                if (message.get("role") == "tool_result" and message.get("toolUseId") == event.tool_call_id
                        and message.get("toolName") == event.tool_name
                        and ("schemaLoadedAtStep" not in event.tool_input_status
                             or message.get("toolInput", {}).get("step") == event.tool_input_status.get("step"))
                        and message.get("nodeId", "") == event.node_id
                        and message.get("attemptId", "") == event.attempt_id
                        and message.get("executionId", "") == self.execution_id):
                    message["toolInput"] = deepcopy(event.tool_input_status)
                    break
            _emit({"kind": "tool_input", "toolName": event.tool_name,
                   "toolInput": event.tool_input_status, **identity})
            return
        command = {}
        if event.kind == "tool_result":
            key = (self.execution_id, event.node_id, event.attempt_id, event.tool_call_id)
            with self._stream_lock:
                command = self._command_results.pop(key, {})
        if event.kind == "tool_start":
            self.display_messages.append({"role": "assistant_tool_call", "toolName": event.tool_name,
                                          "toolUseId": event.tool_call_id, "input": deepcopy(event.tool_input), **identity})
        elif event.kind == "tool_result":
            self.display_messages.append({"role": "tool_result", "toolName": event.tool_name,
                                          "toolUseId": event.tool_call_id, "content": event.output, "isError": event.is_error, **command, **identity})
        tool_key = (event.node_id, event.attempt_id,
                    event.tool_call_id or (event.step, event.tool_name))
        if event.kind == "model_status":
            _emit({"kind": "model_status", "modelState": event.content, **identity})
            return
        if event.kind == "tool_start":
            self._tool_started[tool_key] = time.monotonic()
            _emit(
                {
                    "kind": "tool_start",
                    "step": event.step,
                    "toolName": event.tool_name,
                    "input": event.tool_input,
                    **identity,
                }
            )
            return

        if event.kind == "tool_result":
            started = self._tool_started.pop(tool_key, None)
            duration_ms = round((time.monotonic() - started) * 1000) if started else None
            _emit(
                {
                    "kind": "tool_result",
                    "step": event.step,
                    "toolName": event.tool_name,
                    "output": event.output,
                    "isError": event.is_error,
                    "durationMs": duration_ms,
                    **command,
                    **identity,
                }
            )
            if self.file_changes is not None:
                self.file_changes.refresh()
            return

        if event.kind == "thinking":
            if not self._thinking_announced:
                self._thinking_announced = True
                self.display_messages.append({"role": "run_event", "event": {
                    "kind": "thinking_status", "content": "正在分析当前任务", **identity}})
                _emit(
                    {
                        "kind": "thinking_status",
                        "step": event.step,
                        "content": "正在分析当前任务",
                        **identity,
                    }
                )
            return

        if event.kind == "progress":
            payload = {"kind": "progress", "step": event.step, "content": event.content, **identity,
                       **({"progressStatus": event.progress_status} if event.progress_status is not None else {})}
            self.display_messages.append({"role": "run_event", "event": deepcopy(payload)})
            _emit(payload)
            return

        if event.kind == "runtime":
            runtime_event = asdict(event.runtime_event) if event.runtime_event else None
            self.display_messages.append({"role": "run_event", "event": {
                "kind": "runtime", "step": event.step, "content": event.content,
                "stopReason": event.stop_reason, "runtime": runtime_event, **identity}})
            _emit(
                {
                    "kind": "runtime",
                    "step": event.step,
                    "content": event.content,
                    "stopReason": event.stop_reason,
                    "runtime": runtime_event,
                    **identity,
                }
            )
            return

        if event.kind == "done":
            self.close_pending_messages()
            if self.defer_done:
                return
            _emit(
                {
                    "kind": "done",
                    "step": event.step,
                    "stopReason": event.stop_reason,
                }
            )


def _approval_prompt(request: dict[str, Any], *, workspace: str,
                     inbox: WorkerInbox | None = None,
                     transcript: list[dict] | None = None) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    _emit_process(
        {
            "kind": "approval_requested",
            "requestId": request_id,
            "request": public_approval_request(request, workspace),
        }, transcript,
    )
    while True:
        if inbox is not None:
            response = inbox.receive(request_id, cancelled=approval_cancelled)
        else:
            line = sys.stdin.readline()
            try:
                response = json.loads(line) if line else {}
            except json.JSONDecodeError:
                continue
        if not response:
            _emit_process({"kind": "approval_resolved", "requestId": request_id, "decision": "deny_once"}, transcript)
            return {"decision": "deny_once", "feedback": (
                "Task canceled while awaiting approval." if approval_cancelled() else "Web client disconnected.")}
        if response.get("kind") != "approval_response":
            continue
        if response.get("requestId") != request_id:
            continue
        decision = str(response.get("decision", "deny_once"))
        result = {"decision": decision}
        feedback = str(response.get("feedback", "")).strip()
        if feedback:
            result["feedback"] = feedback
        _emit_process(
            {
                "kind": "approval_resolved",
                "requestId": request_id,
                "decision": decision,
            }, transcript,
        )
        return result


def _run(workspace, session_id, prompt, execution_profile, web_run_id="", *args, **kwargs):
    from minicode.run_ownership import RuntimeRunOwnerLease
    from minicode.web_run_inputs import ledger_path, read_inputs
    if not web_run_id:
        return _run_owned(workspace, session_id, prompt, execution_profile, web_run_id, *args, **kwargs)
    # Covers startup/configuration as well as all queued native executions. A
    # restarted host may fence a not-yet-started worker while holding this lock.
    with RuntimeRunOwnerLease(ledger_path(session_id, web_run_id), run_id=web_run_id):
        receipt = read_inputs(session_id, web_run_id)
        if receipt and receipt["status"] in {"stopped", "failed", "completed"}:
            _emit({"kind": "error", "message": "任务已由恢复流程停止，请重新提交。"})
            return 3
        return _run_owned(workspace, session_id, prompt, execution_profile, web_run_id, *args, **kwargs)


def _run_owned(
    workspace: str,
    session_id: str,
    prompt: str,
    execution_profile: str,
    web_run_id: str = "",
    inbox: WorkerInbox | None = None,
    task_snapshot: dict[str, Any] | None = None,
    run_budget: dict[str, Any] | None = None,
    web_runtime_policy: dict[str, Any] | None = None,
    continuation: dict[str, str] | None = None,
    context_settings: dict[str, Any] | None = None,
    *,
    execution_location: ExecutionLocation | None = None,
    permission_mode: str = "bypass",
    initial_input: dict[str, Any] | None = None,
    _probe: WebRunProbe | None = None,
) -> int:
    if permission_mode not in {"default", "bypass"}:
        _emit({"kind": "error", "message": "权限模式只能是默认审批或完全访问。"})
        return 2
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
    session.binding = project_binding(str(root), session.binding)
    if session.binding.get("worktree_id"):
        from minicode.git_workspaces import GitWorktreeService
        GitWorktreeService().get(session.binding["worktree_id"], require_ready=True)
    turn_id = begin_turn(session, prompt)
    startup_process: list[dict] = []

    force_mock = os.environ.get("MINI_CODE_WEB_FORCE_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}
    rollout = None
    try:
        policy = WebRuntimePolicy.model_validate(web_runtime_policy or {})
        rollout = policy if policy.applies(execution_profile) else None
        if _probe is not None and (force_mock or os.environ.get("MINI_CODE_MODEL_MODE") == "mock"):
            raise ValueError("isolated observation cannot use demo mode")
        runtime = (deepcopy(_probe.runtime_snapshot) if _probe is not None else
                   load_runtime_config(root, **({"runtime_orchestration": "primary"} if rollout else {})))
    except Exception as exc:  # noqa: BLE001 - configuration errors are not demo runs
        if force_mock and _probe is None:
            # Preserve the existing explicitly selected demo; never choose it
            # automatically merely because real configuration is unavailable.
            runtime = {"model": "mock"}
            _emit_process({"kind": "progress", "content": "显式演示模式：未加载真实模型配置，本次不是实际模型执行。"}, startup_process)
        else:
            session.metadata.status = "failed"
            finish_turn(session, turn_id, status="failed", display_messages=[{"role": "user", "content": prompt}])
            save_session(session, force_full=True)
            _emit({"kind": "error", "message": f"配置加载失败（{type(exc).__name__}），未启动模型。请检查本地配置。"})
            return 2

    runtime = dict(runtime)
    runtime["_contextUsageSessionId"] = session.session_id
    runtime["executionProfile"] = execution_profile
    runtime["runtimeV2"] = True
    if inbox is not None:
        runtime["_bindAgentControls"] = inbox.bind_agent_controls
        if inbox.live_inputs:
            runtime["_bindUserControls"] = inbox.bind_user_controls
    if _probe is not None:
        runtime["_genericDelegationAllowed"] = _probe.allow_generic_delegation
    runtime["_sessionContextState"] = deepcopy(session.context_state)
    if session.binding.get("worktree_id") or session.binding.get("forked_from_id"):
        # Keep storage connections, but never reuse the parent's task/index identity.
        namespace = f"{session.binding['project_id']}:{session.binding.get('worktree_id') or session.session_id}"
        runtime["memoryWorkspaceId"] = namespace
        os.environ["MEMORY_WORKSPACE_ID"] = namespace
    if web_run_id:
        runtime["runtimeRunId"] = web_run_id
        runtime["webRunId"] = web_run_id

    budget = None
    try:
        if run_budget is not None:
            try:
                budget = RunBudget.model_validate(run_budget)
            except ValueError:
                raise ValueError("单次预算无效，请重新设置 Token 上限或运行时长。") from None
            # Resolve once at worker startup, not at each Agent retry or node.
            runtime = budget.apply(runtime)
        if rollout:
            runtime = rollout.apply(runtime)
        if continuation is not None:
            execution_location = "isolated"
        if execution_location is not None:
            runtime = apply_execution_location(runtime, execution_location)
        task = restore_task(task_snapshot, str(root)) if task_snapshot is not None else None
        if continuation is not None:
            from minicode.web_run_continuation import prepare_continuation, public_continuation
            prepared = prepare_continuation(session, continuation, task, run_budget=budget)
            if prepared["profile"] != execution_profile:
                raise ValueError("继续任务的模式或验收契约不匹配。")
            task = prepared["task"]
            if prepared["settings"] is not None:
                runtime.update(deepcopy(prepared["settings"]))
            runtime["_webCandidateContinuation"] = prepared["artifact"]
            runtime["_webContinuationLineage"] = public_continuation(prepared)
            runtime["taskRequiresWorkspaceWrite"] = True
        from minicode.entry_task import task_packet
        runtime["_webTaskSnapshot"] = task_packet(task) if task else None
        runtime["_webRunBudget"] = budget.request() if budget else None
        context_preference = (ConversationContext.model_validate(context_settings) if context_settings is not None
                              else load_context_settings(session_id))
        context_preference.validate_capacity(runtime)
        runtime = context_preference.apply(runtime)
    except (ValueError, OSError, RuntimeError) as error:
        session.metadata.status = "failed"
        finish_turn(session, turn_id, status="failed", display_messages=[{"role": "user", "content": prompt}])
        save_session(session, force_full=True)
        _emit({"kind": "error", "message": str(error)})
        return 2
    if execution_location is not None:
        _emit_process({"kind": "progress", "content": (
            f"执行位置：当前目录 {root}。主 Agent 直接写入；启用可写子 Agent 时按文件范围共享协作，最终验证仍须通过。"
            if execution_location == "project" else
            f"执行位置：隔离副本，目标目录 {root}。修改通过检查后写回；候选文件和实际路径可在文件面板查看。")}, startup_process)
    if task:
        _emit_process({"kind": "progress", "content": f"本轮使用已确认契约 {task.source_sha256}；验收项 {task.summary()['criteriaCount']} 个。仅本轮生效，结束后再发送下一条任务。"}, startup_process)
    if budget:
        _emit_process({"kind": "progress", "content": "本次提交明确不设累计 Token、费用和运行时限；仍记录实际用量，可随时停止。此选择仅本轮生效。" if budget.unlimited else "本次提交使用单次预算；本次执行的节点和自动重试共享限额，结束后再发送下一条任务。运行时长从 Worker 启动配置时计算。"}, startup_process)
    if continuation is not None:
        _emit_process({"kind": "progress", "content": "从保留的主任务修改继续，历史用量与失败记录保留；本轮额度单独生效，成员结果需重新核对，所有验收仍须通过。"}, startup_process)
    if rollout:
        token_limit = runtime.get("runtimeMaxTokens")
        _emit_process({"kind": "progress", "content": f"Web 试运行：Primary + Durable，模式 {execution_profile}，{'累计 Token 不限额' if token_limit is None else f'Token 上限 {token_limit}'}。本轮结束后再发送下一条任务；未修改全局设置。"}, startup_process)

    from minicode.extension_commands import expand_extension_command
    from minicode.skill_runtime import user_skill_request

    try:
        prompt = user_skill_request(prompt, runtime, preserve_text=True)
    except ValueError as error:
        session.metadata.status = "failed"
        finish_turn(session, turn_id, status="failed", display_messages=[{"role": "user", "content": prompt}])
        save_session(session, force_full=True)
        _emit({"kind": "error", "message": str(error)})
        return 2
    extension_command = None if runtime.get("_userSkillInvocation") else expand_extension_command(root, prompt)
    if extension_command is not None:
        prompt = extension_command.prompt

    tools_owner = create_default_tool_registry(str(root), runtime=runtime,
        service_gateway=inbox.service_request if inbox is not None else None)
    tools = (tools_owner.filtered_without_names({"task", "consult_agents"})
             if _probe is not None and not _probe.allow_generic_delegation else tools_owner)
    from minicode.tools.session_history_read import session_history_read_tool
    from minicode.tools.session_history_search import session_history_search_tool
    tools = tools.with_additional_tools([session_history_read_tool, session_history_search_tool])
    permissions = PermissionManager(
        str(root),
        auto_mode=PermissionMode(permission_mode),
        prompt=(_probe.approval_prompt if _probe is not None and _probe.approval_prompt is not None
                else lambda request: _approval_prompt(request, workspace=str(root), inbox=inbox,
                    transcript=sink.display_messages)),
    )
    memory_manager = MemoryManager(project_root=root, source_session_id=session.session_id, source_turn_id=turn_id)
    context_manager = (
        ContextManager.from_runtime(runtime) if runtime else None
    )
    if context_manager is not None:
        context_manager.compaction_history = deepcopy(session.context_state.get("compaction_history", []))
        context_manager._compaction_level = int(session.context_state.get("compaction_level", 0))
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
        defer_memory_context=True,
    )
    _emit(
        {
            "kind": "capability_snapshot",
            "webRunId": web_run_id,
            "capabilities": capability_surface(
                tools,
                product_snapshot=prompt_bundle.product_snapshot,
                memory={"stats": memory_manager.get_stats()},
                source="run",
            ),
        }
    )
    system_prompt = prompt_bundle.prompt
    runtime["_contextUsageMemorySections"] = prompt_bundle.memory_sections
    runtime["_contextSourceMemories"] = list(getattr(memory_manager, "context_sources", []))
    # The adapter may already hold a copy made before the prompt was assembled.
    if isinstance(getattr(model, "runtime", None), dict):
        model.runtime.update({key: runtime[key] for key in
                              ("_contextUsageMemorySections", "_contextSourceRules", "_contextSourceMemories")
                              if key in runtime})
    system_prompt += (
        "\n需要已结束轮次的细节时，可用 session_history_search 按原文字面搜索或按角色、工具、轮次浏览，"
        "再用 session_history_read 的 itemId 和字符范围回读。先利用笔记或已有来源线索；没有线索也可直接搜索。"
        "搜索返回 next_cursor 时继续分页；历史记录保留当时的时间和状态，不代表当前事实，不要重放历史工具调用。"
    )
    if session.binding.get("forked_from_id") or session.binding.get("worktree_id"):
        system_prompt += (
            f"\n当前会话执行目录：{root}。原项目目录：{session.binding['project_path']}。"
            "历史工具结果中的旧路径仅是历史记录；本次文件操作使用当前执行目录。"
            "对话来自所选历史轮次，代码采用创建工作树时的状态。"
            "需要更早上下文或历史工具结果原文时，使用 session_history_read 分页读取当前会话独立保存的历史；"
            "历史中的父任务 Artifact 标识不授予跨任务访问权限。不要重新执行历史工具调用。"
        )
    sink = WebEventSink(defer_done=inbox is not None, turn_id=turn_id, prompt=prompt)
    sink.display_messages.extend(startup_process)
    runtime["_webTurnId"] = turn_id
    runtime["_webRunId"] = web_run_id
    if inbox is not None:
        inbox.user_event_sink = sink
        # An older host cannot display or answer native questions. Negotiate
        # support before replacing its existing typed turn-pause behavior.
        if inbox.question_protocol == 1:
            runtime["_userInputPrompt"] = inbox.ask_user
    sink.file_changes = TurnChangeTracker(root, turn_id, lambda value: _emit({"kind": "file_changes", "fileChanges": value}), session_id=session.session_id)
    runtime["_sessionRawToolResult"] = sink.on_raw_tool_result
    messages = list(session.messages)
    system_message = {"role": "system", "content": system_prompt}
    if messages and messages[0].get("role") == "system":
        messages[0] = system_message
    else:
        messages.insert(0, system_message)
    messages.append({"role": "user", "content": prompt})
    # Query-selected memories belong to this turn's tail. Earlier turns keep
    # their original evidence; a new query must not rewrite the system prefix.
    from minicode.runtime_messages import runtime_context_message
    messages.extend(runtime_context_message(section, kind="project_memory")
                    for section in prompt_bundle.memory_sections)
    if continuation is not None:
        from minicode.runtime_messages import runtime_message
        messages.append(runtime_message("This is a new bounded run continuing a retained primary candidate. "
            "Its actual saved files will be restored into your fresh isolated candidate before execution. "
            "Inspect and reuse correct existing changes. Historical Builder IDs, read evidence, patch permissions and "
            "verification do not authorize this run; obtain fresh evidence for remaining work. "
            "Original task acceptance remains mandatory and all prior usage remains recorded."))

    session.messages = messages
    session.history.append(prompt)
    session.metadata.model = model_name
    session.metadata.status = "running"
    save_session(session, force_full=True)
    _emit({"kind": "history_saved", "messageCount": len(session.messages)})
    _emit(
        {
            "kind": "run_started",
            "sessionId": session.session_id,
            "model": model_name,
            "provider": str((runtime or {}).get("provider", "mock")) or "mock",
            "mock": force_mock,
            "executionProfile": execution_profile,
            "executionLocation": execution_location,
            "contextSettings": context_preference.view(runtime),
        }
    )

    permissions.begin_turn()
    saved_turn_status = None
    try:
        if _probe is not None:
            if _probe.assemble_only:
                # Reach the REAL Runtime assembly with zero model requests, so a
                # capability receipt describes objects that were actually built
                # (profile, reducer, scheduler, Worker, delegation and Builder
                # gateways) rather than a restated configuration value. The dispatch
                # loop is not entered: no Worker runs and no supplier is called.
                from minicode.unified_runtime import run_runtime_turn

                runtime["_runtimeAssembleOnly"] = True
                runtime["_runtimeBuilderCapabilityPrepared"] = True
                try:
                    with bind_task(runtime, task):
                        run_runtime_turn(
                            model=model, tools=tools, messages=messages, cwd=str(root),
                            permissions=permissions, session=session, runtime=runtime,
                            memory_manager=memory_manager, context_manager=context_manager,
                            store=store,
                        )
                    runtime["_runtimeAssemblyStatus"] = "prepared"
                except Exception as error:  # noqa: BLE001 - the refusal IS the receipt
                    runtime["_runtimeAssemblyStatus"] = f"refused ({type(error).__name__}: {error})"
                finally:
                    runtime.pop("_runtimeAssembleOnly", None)
                    runtime.pop("_runtimeBuilderCapabilityPrepared", None)
            if _probe.on_prepared is not None:
                with bind_task(runtime, task):
                    _probe.on_prepared(runtime=runtime, tools=tools, messages=messages,
                                       permissions=permissions, memory_manager=memory_manager)
            if _probe.prepare_only:
                # No Runtime delivery exists. A prepared session must never be
                # counted as a completed task or remembered as a success.
                session.metadata.status = "stopped"
                finish_turn(session, turn_id, status="stopped", display_messages=sink.display_messages,
                            file_changes=sink.file_changes.refresh(complete=True))
                save_session(session, force_full=True)
                _emit({"kind": "prepared", "modelRequests": 0, "completed": False})
                return 3
        current_input: dict[str, Any] | None = initial_input
        original_prompt = prompt
        while True:
            # A prior queued turn's success is not evidence for this turn.
            runtime.pop("lastRuntimeTrace", None)
            with bind_task(runtime, task):
                # The original trusted task packet retains its commands and
                # hash. A continuation also carries the validated human
                # amendments; task binding must not revert those requirements.
                if continuation is not None and prepared["settings"] is not None:
                    runtime.update({key: deepcopy(prepared["settings"][key])
                        for key in ("taskConstraints", "acceptanceCriteria", "expectedOutputs")
                        if key in prepared["settings"]})
                result_messages = run_agent_turn(
                    model=model, tools=tools, messages=messages, cwd=str(root),
                    permissions=permissions, session=session, store=store,
                    context_manager=context_manager, memory_manager=memory_manager,
                    runtime=runtime, system_prompt=system_prompt,
                    callbacks=sink,
                )
            session.messages = result_messages
            pass_trace = runtime.get("lastRuntimeTrace")
            if isinstance(pass_trace, dict) and pass_trace.get("state", {}).get("run_id") == runtime.get("runtimeRunId"):
                from minicode.web_run_continuation import CONTINUATION_SETTINGS, continuation_contract
                contract = continuation_contract(pass_trace["state"])
                effective_task = {key: deepcopy(runtime.get(key)) for key in CONTINUATION_SETTINGS}
                if task is not None:
                    effective_task.update(task.fields())
                for public, native in (("taskConstraints", "constraints"), ("acceptanceCriteria", "acceptance_criteria"), ("expectedOutputs", "expected_outputs")):
                    effective_task[public] = contract.get(native, [])
                pass_trace["web_continuation_settings"] = effective_task
                if task_snapshot is not None:
                    pass_trace["web_task_snapshot"] = deepcopy(task_snapshot)
                if continuation is not None:
                    pass_trace["web_continuation"] = runtime["_webContinuationLineage"]
            if budget and isinstance(pass_trace, dict) and pass_trace.get("state", {}).get("run_id") == runtime.get("runtimeRunId"):
                # _record attached this newly generated trace to the session.
                # Retain only the non-secret request for explicit future reuse.
                pass_trace["web_run_budget"] = budget.request()
            save_session(session, force_full=True)
            _emit({"kind": "history_saved", "messageCount": len(result_messages)})
            completion = entry_completion(pass_trace, expected_run_id=runtime.get("runtimeRunId"))
            pass_failed = not completion.completed
            from minicode.runtime_delivery import review_pending_reason
            review_pending = bool(review_pending_reason(pass_trace, expected_run_id=runtime.get("runtimeRunId")))
            session.metadata.review_pending = review_pending
            if not any(m.get("role") == "assistant" and m.get("phase") != "commentary"
                       and m.get("deliveryState") not in {"pending", "rejected"}
                       for m in sink.display_messages):
                final = next((m for m in reversed(result_messages) if m.get("role") == "assistant"), None)
                if final:
                    sink.display_messages.append(deepcopy(final))
            pass_canceled = isinstance(pass_trace, dict) and bool(pass_trace.get("state", {}).get("canceled"))
            if pass_failed and not pass_canceled and not review_pending:
                _emit_process({"kind": "error", "message": runtime_failure_message(
                    pass_trace, expected_run_id=runtime.get("runtimeRunId"))}, sink.display_messages)
            finish_turn(session, turn_id, status="stopped" if pass_canceled else "failed" if pass_failed else "completed",
                        display_messages=sink.display_messages,
                        raw_tool_results=sink.raw_tool_results,
                        file_changes=sink.file_changes.refresh(complete=True),
                        context_state=runtime.get("lastSessionContextState") or {
                            "compaction_history": deepcopy(context_manager.compaction_history),
                            "compaction_level": context_manager._compaction_level} if context_manager else {})
            saved_turn_status = "stopped" if pass_canceled else "failed" if pass_failed else "completed"
            runtime["_sessionContextState"] = deepcopy(session.context_state)
            _emit({"kind": "turn_saved", "turnId": turn_id})
            if current_input:
                _emit({"kind": "run_input", "runInput": {**current_input,
                       "status": "failed" if pass_failed else "completed"}})
            if pass_failed or inbox is None or task is not None or budget is not None or rollout is not None:
                break
            current_input = inbox.checkpoint()
            if current_input is None:
                break
            permissions.end_turn()
            permissions.begin_turn()
            new_prompt = str(current_input["text"])
            if current_input["kind"] == "supplement":
                new_prompt = ("继续检查并完成原任务，结合以下补充要求处理尚未满足的部分。\n"
                              f"原任务：{original_prompt}\n补充要求：{new_prompt}")
            else:
                original_prompt = new_prompt
            messages = [*result_messages, {"role": "user", "content": new_prompt}]
            turn_id = begin_turn(session, new_prompt)
            saved_turn_status = None
            sink = WebEventSink(defer_done=True, turn_id=turn_id, prompt=new_prompt)
            runtime["_webTurnId"] = turn_id
            inbox.user_event_sink = sink
            # A queued task owns a fresh human inbox; it cannot consume directives
            # addressed to the finished activation.
            with inbox.condition:
                inbox.user_controls = None
            sink.file_changes = TurnChangeTracker(root, turn_id, lambda value: _emit({"kind": "file_changes", "fileChanges": value}), session_id=session.session_id)
            runtime["_sessionRawToolResult"] = sink.on_raw_tool_result
            session.messages = messages
            session.history.append(new_prompt)
            save_session(session, force_full=True)
            execution_profile = str(current_input["executionProfile"])
            runtime["executionProfile"] = execution_profile
            runtime["runtimeRunId"] = uuid.uuid4().hex
            if current_input["kind"] == "next_task":
                context_preference = ConversationContext.model_validate(current_input.get("contextSettings") or {})
                context_preference.validate_capacity(runtime)
                runtime.update(context_preference.apply(runtime))
                if context_manager is not None:
                    context_manager.context_cap = runtime["contextWindow"]
                    context_manager.update_model(model_name)
            # Provider adapters capture a copy of runtime at construction.
            model = create_model_adapter(model=model_name, tools=tools, runtime=runtime, force_mock=force_mock)
            _emit({"kind": "run_input", "runInput": {**current_input, "status": "applied"}})
            _emit({"kind": "history_saved", "messageCount": len(messages)})
        session.messages = result_messages
        try:
            session.permissions_summary = {"items": permissions.get_summary()}
            session.skills = tools.get_skills()
            session.mcp_servers = tools.get_mcp_servers()
            fresh_product_snapshot = build_product_snapshot(root, runtime=runtime)
            session.instruction_layers = fresh_product_snapshot.get("instruction_layers", [])
            session.hook_status = fresh_product_snapshot.get("hook_status", {})
            session.delegated_tasks = fresh_product_snapshot.get("delegated_tasks", [])
            session.delegation_status = fresh_product_snapshot.get("delegation_status", {})
            session.extension_manifests = fresh_product_snapshot.get("extension_manifests", [])
            session.readiness_report = fresh_product_snapshot.get("readiness_report", {})
            session.memory_continuity = build_memory_continuity_snapshot(
                memory_manager,
                workspace=str(root),
            )
        except Exception as error:  # Optional product/memory display projection.
            message = "任务信息摘要更新失败，执行结果已保留：" + type(error).__name__
            _emit({"kind": "runtime", "content": message, "runtime": {"category": "storage_warning", "message": message}})
        trace = runtime.get("lastRuntimeTrace")
        if isinstance(trace, dict) and web_run_id:
            trace["web_run_id"] = web_run_id
        completion = entry_completion(trace, expected_run_id=runtime.get("runtimeRunId"))
        delivery_failed = not completion.completed
        run_canceled = isinstance(trace, dict) and bool(trace.get("state", {}).get("canceled"))
        session.metadata.status = "stopped" if run_canceled else "failed" if delivery_failed else "completed"
        save_session(session, force_full=True)
        if run_canceled:
            return 130
        if delivery_failed:
            return 1
        return 0
    except KeyboardInterrupt:
        session.metadata.status = "stopped"
        session.metadata.review_pending = False
        sink.close_pending_messages()
        finish_turn(session, turn_id, status="stopped", display_messages=sink.display_messages, raw_tool_results=sink.raw_tool_results,
                    file_changes=sink.file_changes.refresh(complete=True))
        save_session(session, force_full=True)
        return 130
    except Exception as exc:  # noqa: BLE001
        session.metadata.status = saved_turn_status or "failed"
        session.metadata.review_pending = False
        sink.close_pending_messages()
        _emit_process({"kind": "error", "message": f"{type(exc).__name__}: {exc}"}, sink.display_messages)
        if saved_turn_status is None:
            finish_turn(session, turn_id, status="failed", display_messages=sink.display_messages, raw_tool_results=sink.raw_tool_results,
                        file_changes=sink.file_changes.refresh(complete=True))
        save_session(session, force_full=True)
        return 1
    finally:
        permissions.end_turn()
        try:
            tools_owner.dispose()
        except Exception:  # noqa: BLE001, S110 - shutdown cleanup is best-effort.
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
    execution_profile = str((message or {}).get("executionProfile", "standard")).strip().lower()
    web_run_id = str((message or {}).get("runId", "")).strip()
    if not prompt:
        _emit({"kind": "error", "message": "请输入任务内容。"})
        raise SystemExit(2)
    if execution_profile not in {"standard", "deep", "ultra"}:
        _emit({"kind": "error", "message": "执行模式只能是 Standard、Deep 或 Ultra。"})
        raise SystemExit(2)
    # NumPy/native DLL initialization can hang on Windows when first imported
    # inside a scheduler thread. Load configured storage drivers on the main
    # thread before starting the inbox or any Worker executors.
    from minicode.web_launch import startup_report
    report = startup_report(args.workspace)
    if not report["ok"]:
        missing = ", ".join(name for name, ready in report["dependencies"].items() if not ready)
        _emit({"kind": "error", "message": f"后端缺少或无法加载依赖：{missing}。请通过项目 .venv 启动 MiniCode。"})
        raise SystemExit(2)
    protocol = (message or {}).get("inputProtocol")
    inbox = WorkerInbox(web_run_id=web_run_id, live_inputs=protocol == 2,
                        question_protocol=(message or {}).get("questionProtocol", 0)) if protocol in {1, 2} else None
    if inbox is not None:
        inbox.start()
    raise SystemExit(_run(args.workspace, args.session, prompt, execution_profile, web_run_id, inbox,
                         initial_input=(message or {}).get("initialInput"),
                         permission_mode=(message or {}).get("permissionMode", "default"),
                         task_snapshot=(message or {}).get("taskSnapshot"), run_budget=(message or {}).get("runBudget"),
                         web_runtime_policy=(message or {}).get("webRuntimePolicy"),
                         continuation=(message or {}).get("continuation"),
                         context_settings=(message or {}).get("contextSettings"),
                         # Old servers have no location field. Preserve their
                         # profile-owned behavior until the server is restarted.
                         execution_location=(message or {}).get("executionLocation")))


if __name__ == "__main__":
    main()
