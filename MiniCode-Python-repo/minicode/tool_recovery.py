"""Bounded native read retries and evidence-linked recovery diagnostics.

Only the executor's exact native search definitions and the public GET reader
use this retry loop. Neither error text nor extension-supplied flags authorize
replay. Ordinary permissions, cancellation and the outer deadline still apply.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import math
import random
import threading
import time

from minicode.tooling import ToolResult

MAX_READ_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 10.0


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None


def is_native_search(tool) -> bool:
    from minicode.tools.grep_files import grep_files_tool
    from minicode.tools.code_nav import find_references_tool
    return tool is grep_files_tool or tool is find_references_tool


def terminal_timeout(result: ToolResult) -> bool:
    return result.timedOut and not result.recoverableTimeout


def settled_command_result(tool, result, *, watchdog_expired=False, parent_canceled=False):
    """Preserve a joined native receipt, even when postprocessing hit the clock.

    This returns control to the existing Agent; it never starts a new process.
    Tool identity and termination facts, not output text, admit recovery.
    """
    from minicode.tools.run_command import run_command_tool
    from minicode.tools.test_runner import test_runner_tool
    discovery_canceled = (tool is test_runner_tool and isinstance(result, ToolResult)
        and result.commandExitCode is None and result.failureKind == "canceled")
    if (tool is not run_command_tool and tool is not test_runner_tool or not isinstance(result, ToolResult)
            or not result.commandExecutionSettled or (type(result.commandExitCode) is not int and not discovery_canceled)
            or result.ambiguousSideEffect or result.unsettledTool
            or result.awaitUser or result.runtimeYield or result.inputRejected or result.backgroundTask):
        return None
    if parent_canceled:
        return replace(result, ok=False, failureKind="canceled", recoverableTimeout=False,
                       output="Command canceled by parent Runtime; execution has settled.\n" + result.output)
    interrupted = result.timedOut or watchdog_expired and result.failureKind == "canceled"
    if not interrupted:
        return result
    guidance = (
        "Command timeout recovery: the foreground process and output readers have exited and process-tree "
        "termination succeeded. Continue this task from the retained output and existing files. "
        "Partial writes or external effects may remain: inspect the affected state before deciding what to run. "
        "Identify the stalled stage, repair the script or split the check, and set bounded waits with cleanup "
        "(for browser probes, bound screenshot waits and close the browser in finally). "
        "If another run is needed, explicitly propose the corrected command; nothing is automatically replayed. "
        "Do not treat partial PASS output as completion or skip required verification. "
        "Identical unchanged retries are blocked; corrected attempts remain bounded."
    )
    if discovery_canceled:
        guidance = ("Test discovery stopped before starting a process. Continue with a narrower test path "
                    "within the remaining task budget; no test has passed and nothing was automatically replayed.")
    prefix = "Command stopped at the tool deadline; cleanup is confirmed.\n" if not result.timedOut else ""
    return replace(result, ok=False, timedOut=True, recoverableTimeout=True, failureKind=None,
        recoveryGuidance=guidance, output=prefix + result.output + "\n\n" + guidance,
        toolRecovery={"status": "agent_action", "kind": "command_timeout"})


def retry_read(action, *, cancellation=None, notify=None, settled_search=False):
    """Retry a joined native read at most twice; retain every failed receipt.

    Waits happen inside the existing execution clock. Long Retry-After values
    are returned to the Agent instead of sleeping beyond the bounded window or
    retrying before the server allows it. No timers survive this invocation.
    """
    cancellation = cancellation if cancellation is not None else threading.Event()
    started = time.monotonic()
    attempts = []
    kind = "search_timeout" if settled_search else "transient_read"
    result = None
    for index in range(MAX_READ_RETRIES + 1):
        if cancellation.is_set():
            result = ToolResult(ok=False, failureKind="canceled", output="Canceled: parent Runtime canceled this tool call")
            break
        result = action()
        # Never honor flags from a replaced tool implementation.
        result = replace(result, recoverableTimeout=False)
        interrupted = (result.ambiguousSideEffect or result.awaitUser or result.waitKind
                       or result.runtimeYield or result.runtimeStopReason or result.backgroundTask
                       or result.inputRejected or result.inputValidationFailed
                       or result.failureKind in {"canceled", "policy_denied", "permission_denied"})
        eligible = not result.ok and not interrupted and (
            settled_search and result.timedOut or not result.timedOut and result.failureKind == "transient_error")
        if not eligible:
            break
        attempts.append({"attempt": index + 1, "output": result.output,
                         "timedOut": result.timedOut})
        if cancellation.is_set():
            result = ToolResult(ok=False, failureKind="canceled", output="Canceled: parent Runtime canceled this tool call")
            break
        result = replace(result, recoverableTimeout=bool(settled_search and result.timedOut))
        if index == MAX_READ_RETRIES:
            break
        delay = max((2 ** index) * random.uniform(0.8, 1.2), result.retryAfterSeconds or 0.0)
        if delay > MAX_RETRY_DELAY_SECONDS:
            break
        attempts[-1]["delayMs"] = round(delay * 1000)
        if notify:
            try:
                notify({"status": "retrying", "kind": kind, "attempt": index + 2,
                        "maxAttempts": MAX_READ_RETRIES + 1, "retryDelayMs": round(delay * 1000)})
            except Exception:  # Optional display updates cannot replace the native receipt.
                logging.getLogger(__name__).warning("Could not publish read retry progress")
        if cancellation.wait(delay):
            result = ToolResult(ok=False, failureKind="canceled", output="Canceled: parent Runtime canceled this tool call")
            break
    assert result is not None
    if not attempts:
        return result
    canceled = result.failureKind == "canceled"
    metadata = {"status": "recovered" if result.ok else "canceled" if canceled else "agent_action",
                "kind": kind, "attempts": attempts, "maxAttempts": MAX_READ_RETRIES + 1,
                "elapsedMs": round((time.monotonic() - started) * 1000)}
    if result.retryAfterSeconds is not None:
        metadata["retryAfterSeconds"] = result.retryAfterSeconds
    if not result.ok and not canceled:
        hint = ("The native search has exited; partial results are not complete evidence. "
                "Continue this task by narrowing the path/include filters or inspecting specific files. "
                "Do not repeat the identical broad search or omit required evidence."
                if settled_search else
                "Bounded automatic read retries did not recover this request. Continue with another authorized source "
                "or a corrected request. Preserve any required evidence gaps; do not claim success.")
        if result.retryAfterSeconds:
            hint += f" The service requested a wait of at least {result.retryAfterSeconds:g} seconds before retrying."
        result = replace(result, output=result.output + "\n\nRecovery guidance: " + hint)
    return replace(result, toolRecovery=metadata)


def _key(call):
    try:
        return hashlib.sha256(json.dumps([call["toolName"], call["input"]],
            sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError, RecursionError):
        return None


class ToolRecoveryTracker:
    """Advisory project lessons and strictly ordered same-operation links."""
    def __init__(self, project_root):
        from minicode.tool_recovery_memory import RecoveryMemory
        self.memory = RecoveryMemory(project_root)
        self.pending = []
        self.verification_refusals = []

    def observe(self, call, result, step, *, registry=None):
        from minicode.tool_recovery_memory import lesson_for
        key = _key(call)
        metadata = dict(result.toolRecovery or {})
        settled = not (terminal_timeout(result) or result.ambiguousSideEffect or result.awaitUser
                       or result.waitKind or result.runtimeYield or result.runtimeStopReason or result.backgroundTask)
        matches = [item for item in self.pending if key and item["key"] == key and item["step"] < step]
        if result.ok and settled and matches:
            metadata.update(status="recovered", kind=matches[-1]["kind"],
                resolves=[{"callId": item["callId"], "step": item["step"]} for item in matches])
            for rule in {item["kind"] for item in matches}:
                self.memory.record(rule, recovered=True)
            self.pending = [item for item in self.pending if item not in matches]
        rule = lesson_for(result)
        if rule:
            self.memory.record(rule, recovered=result.ok, observed=True)
        if not result.ok and settled and rule and not result.inputValidationFailed:
            metadata.update(status="agent_action", kind=rule)
            if key:
                self.pending.append({"key": key, "callId": call["id"], "step": step, "kind": rule})
                self.pending = self.pending[-64:]
        if registry is not None and settled:
            alternative = self._verification_alternative(call, result, step, registry)
            if alternative:
                metadata = alternative
        if metadata:
            metadata["step"] = step
            return metadata
        return None

    def _verification_alternative(self, call, result, step, registry):
        from minicode.tool_discovery import DISCOVER_TOOLS, ToolDiscovery
        from minicode.verification_requests import REQUEST_VERIFICATION, native_verification_request

        request_tool = registry.find(REQUEST_VERIFICATION)
        request = native_verification_request(request_tool)
        if request is None:
            return None
        handler = request_tool.run
        definition = registry.find(call["toolName"])
        if (call["toolName"] == DISCOVER_TOOLS and not result.ok and not result.inputValidationFailed
            and isinstance(getattr(getattr(definition, "run", None), "__self__", None), ToolDiscovery)
            and getattr(definition.run, "__func__", None) is ToolDiscovery._run
            and call.get("input") == {"names": ["run_command"]}):
            try:
                payload = json.loads(result.output)
            except (ValueError, TypeError):
                return None
            if payload.get("unavailable_reasons") != {"run_command": "effect_restricted"}:
                return None
            self.verification_refusals.append({"handler": handler, "callId": call["id"], "step": step})
            self.verification_refusals = self.verification_refusals[-8:]
            return {"status": "agent_action", "kind": "verification_alternative"}
        if (call["toolName"] != REQUEST_VERIFICATION or definition is not request_tool or not result.ok
            or request.unresolved or request.artifact is None or not request.receipt
            or request.receipt.get("all_criteria_covered") is not True):
            return None
        matches = [item for item in self.verification_refusals if item["handler"] is handler and item["step"] < step]
        if matches:
            self.verification_refusals = [item for item in self.verification_refusals if item not in matches]
            return {"status": "recovered", "kind": "verification_alternative",
                    "resolves": [{"callId": item["callId"], "step": item["step"]} for item in matches]}

    def context(self):
        return self.memory.context()
