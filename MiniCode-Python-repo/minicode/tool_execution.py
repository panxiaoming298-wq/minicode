"""Tool invocation boundary: policy context, cancellation, timeout and result delivery."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from minicode.logging_config import get_logger
from minicode.permissions import bind_approval_wait
from minicode.runtime_cancellation import ToolApprovalCanceled
from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.state import increment_tool_calls, set_busy, set_idle
from minicode.tooling import ToolContext, ToolRegistry, ToolResult, steering_interruption
from minicode.turn_telemetry import measured

logger = get_logger("tool_execution")


def _tool_timeout_seconds(tool_definition, tool_input, tool_scheduler=None) -> float:
    """Respect native command deadlines without weakening explicit host caps."""
    from minicode.tools.run_command import COMMAND_TIMEOUT, run_command_tool
    from minicode.tools.test_runner import test_runner_tool
    from minicode.tools.task import native_preparation_timeout
    from minicode.candidate_checks import native_verification_timeout

    preparation_timeout = native_preparation_timeout(tool_definition, tool_input)
    verification_timeout = native_verification_timeout(tool_definition)
    timeout = preparation_timeout or verification_timeout or 120.0
    from minicode.tools.development_services import is_native_service_tool
    managed_service = is_native_service_tool(tool_definition, "start")
    if managed_service:
        try:
            timeout = float(tool_definition.validator(tool_input)["startup_timeout"]) + 20.0
        except (ValueError, TypeError, OverflowError):
            timeout = 50.0  # Argument rejection belongs to the native validator.
    native_command = tool_definition is run_command_tool or tool_definition is test_runner_tool
    if native_command and isinstance(tool_input, dict):
        try:
            command_timeout = tool_definition.validator(tool_input)["timeout"] or COMMAND_TIMEOUT
        except (TypeError, ValueError, OverflowError):
            command_timeout = COMMAND_TIMEOUT
        # Let the command kill and join its process/readers before the outer
        # watchdog declares its effects uncertain. Approval waits are separate.
        timeout = float(command_timeout) + 20.0
    configured = os.environ.get("MINICODE_TOOL_TIMEOUT")
    if configured is not None:
        try:
            cap = float(configured)
            if math.isfinite(cap):
                timeout = (min(timeout, max(0.0, cap)) if native_command or managed_service or preparation_timeout is not None or verification_timeout is not None
                           else max(0.0, cap))
        except ValueError:
            pass
    forced = getattr(tool_scheduler, "_force_tool_timeout", None)
    if forced is not None:
        try:
            cap = float(forced)
            if math.isfinite(cap):
                timeout = min(timeout, max(0.0, cap))
        except (TypeError, ValueError):
            pass
    return timeout


@dataclass
class _CommandTimeoutRecovery:
    write_generation: int
    timeout: int
    retries: int = 0


class ToolRetryGuard:
    """Bound transient replays and unchanged input-error loops in one turn.

    This never launches retries or grants permissions. Normal validation and
    policy checks still run on every admitted call. Successful observations
    clear the transient failure; changed arguments are a distinct operation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._remaining: dict[str, int] = {}
        self._invalid_inputs: dict[str, tuple[str, str, int]] = {}
        self._input_issues: dict[str, list[dict[str, Any]]] = {}
        self._input_reminders_sent: set[tuple[str, str]] = set()
        self._input_family_counts: dict[tuple, int] = {}
        self._input_repeat_reminders: dict[str, tuple] = {}
        self._consecutive_input_errors = 0
        self._write_generation = 0
        self._completed_checks: dict[str, int] = {}
        self._written_paths: dict[str, int] = {}
        self._command_timeouts: dict[str, _CommandTimeoutRecovery] = {}
        self._uncertain_commands: set[str] = set()
        self._validation_observations: dict[str, dict] = {}

    @staticmethod
    def _command_key(call, tool):
        from minicode.tools.run_command import run_command_tool, _normalize_command_input
        from minicode.tools.test_runner import test_runner_tool
        if tool is test_runner_tool:
            try:
                value = tool.validator(call["input"])
            except (ValueError, TypeError):
                return None
            return json.dumps(["test_runner", {k: v for k, v in value.items() if k != "timeout"}], sort_keys=True)
        if tool is not run_command_tool:
            return None
        value = call["input"]
        if (not isinstance(value, dict) or not isinstance(value.get("command"), str)
                or value.get("args") is not None and (not isinstance(value["args"], list)
                    or not all(isinstance(arg, str) for arg in value["args"]))
                or value.get("cwd") is not None and not isinstance(value["cwd"], str)):
            return None  # The native validator delivers the field-level error.
        try:
            command, args = _normalize_command_input(value)
        except ValueError:
            return None
        # Changing the timeout cannot reset the recovery budget for a command.
        return json.dumps([command, args, value.get("cwd")], sort_keys=True)

    def _script_repaired(self, call, cwd, generation):
        from minicode.tools.run_command import _normalize_command_input
        if call["toolName"] != "run_command":
            return False
        command, args = _normalize_command_input(call["input"])
        if (not cwd or Path(command).stem.lower() not in {"node", "python", "python3", "py"}
                or not args or args[0].startswith("-")
                or Path(args[0]).suffix.lower() not in {".js", ".mjs", ".cjs", ".py"}):
            return False
        root = Path(cwd).resolve()
        directory = root / (call["input"].get("cwd") or ".")
        script = (directory / args[0]).resolve()
        return (script.is_relative_to(root)
                and self._written_paths.get(os.path.normcase(str(script)), 0) > generation)

    @staticmethod
    def _local_check(call, tool) -> bool:
        from minicode.tools.run_command import run_command_tool
        from minicode.tools.test_runner import test_runner_tool
        import shlex
        from pathlib import Path
        if tool is test_runner_tool:
            return True
        if tool is not run_command_tool:
            return False
        value = call.get("input", {})
        command, arguments = value.get("command", ""), value.get("args", [])
        if not isinstance(command, str) or not isinstance(arguments, list):
            return False
        # Shell composition/redirection cannot identify a single local check.
        if any(mark in command for mark in (";", "&", "|", ">", "<", "$", "`", "\n", "\r")):
            return False
        try:
            parts = [command] if arguments else shlex.split(command)
        except ValueError:
            return False
        parts += arguments
        if not parts or not all(isinstance(part, str) for part in parts):
            return False
        executable, args = Path(parts[0]).stem.lower(), parts[1:]
        return ((executable == "node" and "--test" in args and not any(a in args for a in ("-e", "--eval", "-p", "--print")))
                or (executable in {"python", "python3", "py"} and args[:2] == ["-m", "pytest"])
                or executable == "pytest"
                or (executable in {"npm", "pnpm", "yarn"} and
                    (args[:1] in [["test"], ["build"]] or args[:2] in [["run", "test"], ["run", "build"]]))
                or (executable == "npx" and args[:2] == ["vite", "build"]))

    @staticmethod
    def _key(call: dict) -> str:
        # A parser's empty fallback is not the model's intended operation.
        arguments = (["rejected", call.get("rawArguments"), call.get("executionBlocked"), call.get("inputError")]
                     if call.get("inputError") or call.get("executionBlocked") else ["parsed", call["input"]])
        encoded = json.dumps([call["toolName"], arguments], sort_keys=True,
                             ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def reject(self, call: dict, tool, *, cwd=None) -> ToolResult | None:
        try:
            key = self._key(call)
        except (TypeError, ValueError) as error:
            return ToolResult(ok=False, inputRejected=True, failureKind="invalid_input",
                              output=f"NotExecuted: arguments must be finite JSON values: {error}")
        with self._lock:
            command_key = self._command_key(call, tool)
            recovery = self._command_timeouts.get(command_key)
            if command_key in self._uncertain_commands:
                return ToolResult(ok=False, inputRejected=True, failureKind="policy_denied",
                    output="NotExecuted: command cleanup remains unresolved; inspect its existing process and effects first.")
            if recovery is not None:
                from minicode.tools.run_command import COMMAND_TIMEOUT
                timeout = call["input"].get("timeout") or COMMAND_TIMEOUT
                from minicode.tools._command_preflight import is_file_search
                from minicode.tools.run_command import _normalize_command_input
                program, arguments = _normalize_command_input(call["input"])
                file_search = is_file_search(program, arguments)
                corrected = (type(timeout) is int and timeout > recovery.timeout and not file_search
                    or self._local_check(call, tool) and self._write_generation > recovery.write_generation
                    or self._script_repaired(call, cwd, recovery.write_generation))
                if recovery.retries < 2 and corrected:
                    # This is a new model proposal after correction, not an
                    # automatic replay. All permissions/ownership checks follow.
                    recovery.retries += 1
                    self._remaining.pop(key, None)
                    return None
                reason = ("two corrected command retries have been exhausted" if recovery.retries >= 2
                          else "the timed-out command has not been corrected")
                action = ("Narrow the search path/pattern or use bounded list_files discovery; increasing the timeout alone does not repair an oversized search. "
                          if file_search else "Repair the invoked script, fix the checked files, or increase an insufficient deadline. ")
                return ToolResult(ok=False, inputRejected=True, failureKind="policy_denied", output=(
                    f"NotExecuted: {reason}. Continue diagnosing from the retained output; "
                    + action +
                    "If retries are exhausted, split the check or report the remaining blocker. "
                    "No previous command is running or automatically replayed by this recovery path."))
            remaining = self._remaining.get(key)
            if remaining is None:
                return None
            completed_generation = self._completed_checks.get(key)
            if (completed_generation is not None and completed_generation < self._write_generation
                    and self._local_check(call, tool)):
                # A new explicit check after an observed write still goes through
                # normal approval. Never auto-repeat or clear uncertain effects.
                self._remaining.pop(key, None)
                self._completed_checks.pop(key, None)
                return None
            if tool.replay_policy is ReplayPolicy.SAFE and remaining > 0:
                self._remaining[key] = remaining - 1
                return None
        reason = ("the two retries for this transient failure are exhausted" if tool.replay_policy is ReplayPolicy.SAFE
                  else f"this tool's {tool.replay_policy.value} policy does not permit automatic replay")
        return ToolResult(ok=False, inputRejected=True, failureKind="policy_denied", output=(
            f"NotExecuted: {reason}. Inspect the previous receipt and existing operation state; "
            "use the supported reconciliation workflow or report the unresolved failure."))

    def observe(self, call: dict, result: ToolResult, tool=None, *, cwd=None) -> ToolResult:
        from minicode.agent_intelligence import ErrorClassifier, RecoveryStrategy

        key = self._key(call)
        with self._lock:
            command_key = self._command_key(call, tool)
            observation_key = command_key or key
            if result.inputValidationFailed and command_key is not None:
                self._validation_observations.pop(observation_key, None)
                self._validation_observations[observation_key] = {
                    "tool": call["toolName"], "operation": command_key[:800], "error": result.output[:700],
                    "issues": [{"path": issue.get("path", [])[:8],
                                "expected": str(issue.get("expected", ""))[:200],
                                "actual": str(issue.get("actual", ""))[:200]}
                               for issue in result.inputIssues[:4] if isinstance(issue, dict)]}
                while len(self._validation_observations) > 8:
                    self._validation_observations.pop(next(iter(self._validation_observations)))
            elif result.ok:
                self._validation_observations.pop(observation_key, None)
            if command_key is not None:
                if result.ambiguousSideEffect or result.unsettledTool or result.timedOut and not result.recoverableTimeout:
                    self._command_timeouts.pop(command_key, None)
                    self._uncertain_commands.add(command_key)
                elif result.recoverableTimeout and result.commandExecutionSettled:
                    from minicode.tools.run_command import COMMAND_TIMEOUT
                    timeout = call["input"].get("timeout") or COMMAND_TIMEOUT
                    recovery = self._command_timeouts.setdefault(command_key,
                        _CommandTimeoutRecovery(self._write_generation, timeout))
                    recovery.write_generation, recovery.timeout = self._write_generation, timeout
                elif result.ok:
                    self._command_timeouts.pop(command_key, None)
            if (result.ambiguousSideEffect or result.timedOut
                    or result.toolRecovery and result.toolRecovery.get("status") == "agent_action"):
                self._remaining[key] = 0
                self._completed_checks.pop(key, None)
            if result.ok and result.contextWriteRevision and tool is not None:
                from minicode.tools.write_file import write_file_tool
                from minicode.tools.edit_file import edit_file_tool
                from minicode.tools.patch_file import patch_file_tool
                if tool is write_file_tool or tool is edit_file_tool or tool is patch_file_tool:
                    self._write_generation += 1
                    if cwd and isinstance(call["input"].get("path"), str):
                        path = os.path.normcase(str((Path(cwd) / call["input"]["path"]).resolve()))
                        self._written_paths[path] = self._write_generation
            if (type(result.commandExitCode) is int and self._local_check(call, tool)
                    and not (result.ambiguousSideEffect or result.timedOut or result.awaitUser
                             or result.backgroundTask or result.inputRejected or result.runtimeStopReason)):
                self._completed_checks[key] = self._write_generation
            name = call["toolName"]
            if (not result.ok and result.inputValidationFailed
                    and not (result.awaitUser or result.waitKind or result.runtimeYield or result.runtimeStopReason
                             or result.ambiguousSideEffect or result.timedOut or result.backgroundTask)):
                self._consecutive_input_errors += 1
                # Revalidate each attempt: source availability, schema bindings
                # and note budgets can change. Never cache a rejection as truth.
                reason = result.output.partition("\nInput was:")[0]
                from minicode.tool_input_recovery import _digest, issue_fingerprints
                schema = _digest(tool.input_schema) if tool is not None else None
                families = issue_fingerprints(result.inputIssues) or (json.dumps([key, reason]),)
                counts = []
                for family in families:
                    identity = (name, schema, family)
                    self._input_family_counts[identity] = self._input_family_counts.get(identity, 0) + 1
                    counts.append(self._input_family_counts[identity])
                count = max(counts)
                while len(self._input_family_counts) > 128:
                    self._input_family_counts.pop(next(iter(self._input_family_counts)))
                self._invalid_inputs[name] = (key, reason, count)
                self._input_issues[name] = [
                    {"path": issue.get("path", [])[:8],
                     "expected": str(issue.get("expected", ""))[:300],
                     "actual": str(issue.get("actual", ""))[:300]}
                    for issue in result.inputIssues[:4] if isinstance(issue, dict)
                ]
                if self._consecutive_input_errors >= 8 or count >= 8:
                    hint = "Tool input recovery exhausted after 8 validation failures without progress on the rejected rule. Changed array positions, unrelated text or other tool calls do not repair this rule. Correct the reported fields. The task remains incomplete; existing work is retained."
                    return replace(result, inputRejected=True, runtimeStopReason="tool_input_exhausted",
                        inputCorrectionHint=hint, output=result.output + "\n\n" + hint)
                if count >= 3:
                    hint = (
                        f"RepeatedToolInputError: the same parameter rule failed validation {count} times. "
                        "This call was rejected before execution. Correct its fields, choose another tool, "
                        "or continue independent steps; do not repeat the unchanged call. Earlier work is retained.")
                    return replace(result, inputRejected=True,
                                   inputCorrectionHint="\n\n".join(filter(None, (result.inputCorrectionHint, hint))), output=result.output + "\n\n" + hint)
                if count == 2:
                    hint = ("The same parameter rule failed validation twice. Inspect the exact field error and tool schema, "
                            "then send corrected arguments. Unchanged arguments will remain rejected.")
                    return replace(result, inputCorrectionHint="\n\n".join(filter(None, (result.inputCorrectionHint, hint))), output=result.output + "\n\n" + hint)
                return result
            self._consecutive_input_errors = 0
            self._invalid_inputs.pop(name, None)
            self._input_issues.pop(name, None)
            self._input_family_counts = {k: v for k, v in self._input_family_counts.items() if k[0] != name}
            self._input_repeat_reminders.pop(name, None)
            if result.ok:
                self._remaining.pop(key, None)
            else:
                classified = ErrorClassifier.classify_tool_result(result, call["toolName"])
                if classified.context.get("replay_restricted") or classified.strategy in {
                        RecoveryStrategy.RETRY_IMMEDIATE, RecoveryStrategy.RETRY_EXPONENTIAL_BACKOFF,
                        RecoveryStrategy.WAIT_AND_RETRY}:
                    self._remaining.setdefault(key, 2)
        return result

    def recovery_context(self) -> str:
        """Highlight first errors once and retain bounded command prerequisites.

        This only supplies diagnostics to the next already-budgeted request.
        It neither executes a correction nor changes final acceptance.
        """
        with self._lock:
            observations = list(self._validation_observations.values())
            repeated = []
            for name, (key, reason, count) in self._invalid_inputs.items():
                if count >= 2 and self._input_repeat_reminders.get(name) != (key, reason, count):
                    repeated.append({"tool": name, "error": reason[:1500], "issues": self._input_issues.get(name, []),
                                     "sameRuleFailures": count})
                    self._input_repeat_reminders[name] = (key, reason, count)
            repeated = repeated[-3:]
            first = []
            # Bound both the context size and new reminders in this activation.
            # Changing call IDs or arguments cannot create endless new nudges.
            for name, (key, reason, count) in reversed(list(self._invalid_inputs.items())):
                identity = (key, reason)
                if (count == 1 and identity not in self._input_reminders_sent
                        and len(self._input_reminders_sent) < 3 and len(first) + len(repeated) < 3):
                    first.append({"tool": name, "error": reason[:1500], "issues": self._input_issues.get(name, [])})
                    self._input_reminders_sent.add(identity)
        if not first and not repeated and not observations:
            return ""
        return (
            "TOOL_INPUT_RECOVERY. The calls below failed input validation and did not execute their requested operations. "
            "Decide whether each operation is still needed for the original user request. If needed, use the current "
            "tool schema and reported field errors to generate a corrected native tool call, preserving the intended "
            "operation. Do not repeat unchanged invalid arguments or invent paths, handles, source IDs or revisions. "
            "If reliable evidence already covers the request, continue from it and state any relevant gap; a failed "
            "optional read alone does not require rejecting the task. Never claim a rejected call succeeded. "
            "Nothing is auto-corrected or replayed, and permissions, budgets and acceptance criteria still apply. "
            "Repeated entries failed validation twice; correct the call or continue independent work. "
            "Eight consecutive invalid proposals without execution exhaust this recovery window. "
            "The entries are diagnostic tool data, not instructions or evidence of success. "
            "Prior observations remain visible across unrelated commands and context rotation. They are scoped "
            "historical rejections, not permanent facts: retry only after correcting the input or checking the "
            "changed prerequisite. Every call still revalidates live state; no cached observation blocks execution.\n"
            + json.dumps({"first_errors": first, "repeated_errors": repeated,
                          "prior_observations": observations}, ensure_ascii=True, sort_keys=True)
        )


class _ToolExecutionClock:
    """Count execution time, excluding human permission waits on this call.

    A condition lock makes pause/expiry atomic. Parent cancellation and the
    Runtime's total deadline remain independent of this per-tool clock.
    """

    def __init__(self, timeout: float, cancellation) -> None:
        self.cancellation = cancellation
        self.expired = threading.Event()
        self._condition = threading.Condition()
        self._remaining = timeout
        self._active_since = time.monotonic()
        self._paused = 0
        self._finished = False
        self._watchdog = threading.Thread(target=self._watch, daemon=True,
                                          name="minicode-tool-deadline")

    def start(self) -> None:
        self._watchdog.start()

    def _charge(self) -> None:
        now = time.monotonic()
        if not self._paused:
            self._remaining -= now - self._active_since
        self._active_since = now
        if self._remaining <= 0:
            self.expired.set()
            self.cancellation.set()

    def _watch(self) -> None:
        with self._condition:
            while not self._finished and not self.expired.is_set():
                self._charge()
                if self.expired.is_set():
                    return
                self._condition.wait(None if self._paused else self._remaining)

    def finish(self) -> None:
        with self._condition:
            self._charge()
            self._finished = True
            self._condition.notify_all()

    @contextmanager
    def pause_for_approval(self):
        with self._condition:
            self._charge()
            if self.cancellation.is_set():
                raise ToolApprovalCanceled("Tool canceled before permission approval; no approval was applied")
            self._paused += 1
            self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._charge()
                self._paused -= 1
                self._condition.notify_all()
        # A late allow response cannot authorize work after a stop or deadline.
        if self.cancellation.is_set():
            raise ToolApprovalCanceled("Tool canceled while awaiting permission; no approval was applied")


class _LinkedCancellationEvent:
    """A per-call cancellation signal that also observes parent cancellation."""

    def __init__(self, parent: Any | None = None) -> None:
        self._parent = parent
        self._local = threading.Event()

    def set(self) -> None:
        self._local.set()

    def is_set(self) -> bool:
        parent_is_set = getattr(self._parent, "is_set", None)
        return self._local.is_set() or (
            callable(parent_is_set) and bool(parent_is_set())
        )

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._local.wait(0.05 if remaining is None else min(0.05, remaining))
        return True


@measured("tool")
def _execute_single_tool(
    call: dict,
    tools: ToolRegistry,
    cwd: str,
    permissions: Any | None,
    session: Any | None,
    runtime: dict | None,
    store: Any | None,
    step: int,
    on_tool_start: Callable[[str, dict], None] | None,
    on_tool_result: Callable[[str, str, bool], None] | None,
    tool_scheduler: Any | None = None,
    memory_preflight: Callable[[dict, ToolEffect], str | None] | None = None,
    memory_result_observer: Callable[[dict, ToolResult], ToolResult | None]
    | None = None,
    retry_guard: ToolRetryGuard | None = None,
    supplied_contracts: dict[str, str] | None = None,
    on_tool_recovery: Callable[[dict], None] | None = None,
    on_tool_images: Callable[[list[dict]], None] | None = None,
) -> ToolResult:
    """Execute a single tool call with hooks, state updates, and crash protection.

    Used both for serial execution and as a worker function for concurrent execution.
    When running concurrently (store/on_tool_start/on_tool_result are None),
    hooks and UI callbacks are deferred to the result processing phase.

    Includes a global exception safety net: any unexpected crash in the tool
    execution pipeline (hooks, state updates, etc.) is caught and converted
    to an error ToolResult, preventing the entire agent loop from crashing.
    """
    tool_name = call["toolName"]
    tool_input = call["input"]
    # An independently classified call that may not execute is never a request to
    # run the tool with its decoded prefix. This covers malformed arguments and,
    # separately, a batch whose transport/parse lost an event: the decoded prefix
    # of an incomplete response is evidence, not an instruction. UI notifications
    # are allowed; no preflight, permission callback, extension hook, scheduler or
    # tool is invoked on this path.
    blocked = call.get("executionBlocked")
    if call.get("inputError") or blocked:
        reason = (f"classification={blocked}" if blocked
                  else "invalid or incomplete JSON object")
        result = ToolResult(ok=False, inputRejected=True, output=(
            f"ModelToolArgumentsRejected: {reason}; no tool was executed. "
            "Supply complete arguments, or return findings supported by the existing evidence."
        ))
        from minicode.tool_argument_diagnostics import (
            INVALID_ARGUMENTS, INCOMPLETE_ARGUMENTS_AT_OUTPUT_CAP, rejected_argument_details,
        )
        # Transport/assembly faults remain distinct from model input mistakes.
        # No tool lookup, validator, permission callback or hook runs here.
        if blocked in {INVALID_ARGUMENTS, INCOMPLETE_ARGUMENTS_AT_OUTPUT_CAP} or not blocked and call.get("inputError"):
            details = rejected_argument_details(call)
            location = (f" at line {details['line']}, column {details['column']}" if "line" in details else "")
            hint = ("Tool input correction (guidance only): " + details["message"] + location + ". "
                    "Generate a new complete JSON object for the original intended operation using the current tool schema. "
                    "Use double-quoted keys and string values, valid escaping and JSON types; do not use bare values or trailing commas. "
                    "Do not execute or reuse a partially decoded prefix. Nothing was repaired or replayed.")
            if blocked == INCOMPLETE_ARGUMENTS_AT_OUTPUT_CAP:
                hint += " The response reached its output limit; reduce the proposed payload or split the work into complete, independently valid calls."
            result = replace(result, inputValidationFailed=True, failureKind="invalid_input",
                inputIssues=[{"path": [], "expected": "a complete JSON object", "actual": details["message"] + location}],
                inputCorrectionHint=hint, output=result.output + "\n\n" + hint)
            if retry_guard is not None:
                result = retry_guard.observe(call, result)
        if on_tool_start:
            on_tool_start(tool_name, tool_input)
        if on_tool_result:
            on_tool_result(tool_name, result.output, True)
        return result
    find_tool = getattr(tools, "find", None)
    tool_definition = find_tool(tool_name) if callable(find_tool) else None
    from minicode.tool_input_recovery import schema_required, correction_guidance
    if tool_definition is not None and schema_required(tool_definition, supplied_contracts):
        # A schema deliberately hidden while native user input is unresolved
        # is an interruption, not a parameter mistake or discovery failure.
        interrupted = steering_interruption(runtime, tool_name)
        if interrupted is not None:
            if on_tool_start:
                on_tool_start(tool_name, tool_input)
            if on_tool_result:
                on_tool_result(tool_name, interrupted.output, True)
            return interrupted
        # Loading a deferred definition is not an invalid argument or a failed
        # execution. Preserve the non-execution receipt, without spending the
        # argument-correction budget or replaying a call the model guessed.
        result = ToolResult(ok=False, inputRejected=True, inputSchemaRequired=True, output=(
            "ToolSchemaRequired: loading this tool's complete definition for the next model request. "
            "This proposal was not executed. Read the supplied parameter names, types and constraints, "
            "then generate a fresh call if the operation is still needed. MiniCode loads permitted schemas "
            "automatically; no discovery call is needed for this recovery. If it remains deferred, select "
            "its exact name with discover_tools using names only. Never replay the earlier proposal."))
        if on_tool_start:
            on_tool_start(tool_name, tool_input)
        if on_tool_result:
            on_tool_result(tool_name, result.output, True)
        return result
    if retry_guard is not None and tool_definition is not None:
        rejection = retry_guard.reject(call, tool_definition, cwd=cwd)
        if rejection is not None:
            if on_tool_start:
                on_tool_start(tool_name, tool_input)
            if on_tool_result:
                on_tool_result(tool_name, rejection.output, True)
            return rejection
    tool_effect = (
        tool_definition.effect if tool_definition is not None else ToolEffect.PRIVILEGED
    )
    # A version-bound read obligation is a real gate: writing a file whose current
    # content the model never observed is refused before the tool runs. A hint in
    # the prompt is not a control, so this is checked at the side-effect boundary.
    if tool_effect is ToolEffect.WORKSPACE_WRITE and isinstance(tool_input, dict):
        from minicode.candidate_read_obligations import ReadObligationUnmet, enforce_write

        target = tool_input.get("path")
        if isinstance(target, str) and target:
            try:
                enforce_write(target)
            except ReadObligationUnmet as unmet:
                result = ToolResult(ok=False, inputRejected=True, output=(
                    f"UnreadCandidateContent: {unmet}; no write was attempted. Read the "
                    "current revision of that file first."))
                if on_tool_start:
                    on_tool_start(tool_name, tool_input)
                if on_tool_result:
                    on_tool_result(tool_name, result.output, True)
                return result

    runtime_context = (runtime or {}).get("_runtimeContext")
    raw_allowed_effects = (runtime or {}).get("_allowedToolEffects")
    allowed_effects = None
    if raw_allowed_effects is not None:
        allowed_effects = frozenset(
            item if isinstance(item, ToolEffect) else ToolEffect(str(item))
            for item in raw_allowed_effects
        )
    cancellation_event = _LinkedCancellationEvent(
        (runtime or {}).get("_cancellationEvent")
    )
    tool_context = ToolContext(
        cwd=cwd,
        permissions=permissions,
        session=session,
        _runtime=runtime,
        runtime_context=runtime_context,
        allowed_effects=allowed_effects,
        cancellation_event=cancellation_event,
        node_id=(runtime or {}).get("_runtimeNodeId"),
        attempt_id=(runtime or {}).get("_runtimeAttemptId"),
        idempotency_key=(runtime or {}).get("_runtimeIdempotencyKey"),
        run_id=(runtime or {}).get("_runtimeActiveRunId"),
        tool_call_id=str(
            call.get("id") or call.get("toolCallId") or f"step-{step}:{tool_name}"
        ),
        base_revision=(runtime or {}).get("_runtimeBaseRevision"),
        raw_result_observer=(
            (lambda _tool_name, result: memory_result_observer(call, result))
            if memory_result_observer is not None
            else None
        ),
        recovery_observer=on_tool_recovery,
        image_observer=on_tool_images,
    )

    try:
        if memory_preflight is not None:
            blocked_reason = memory_preflight(call, tool_effect)
            if blocked_reason:
                result = ToolResult(ok=False, output=blocked_reason, failureKind="context_blocked", inputRejected=True)
                # Match other pre-execution refusals: serial calls must still
                # emit a paired receipt. Concurrent callers own their callbacks.
                if on_tool_start:
                    on_tool_start(tool_name, tool_input)
                if on_tool_result:
                    on_tool_result(tool_name, result.output, True)
                return result

        # Pre-tool hooks and UI (only for serial execution)
        if on_tool_start:
            on_tool_start(tool_name, tool_input)

        if store:
            store.set_state(set_busy(tool_name))

        # Execute the tool with a bounded wait.  A timed-out call receives its
        # own cancellation signal, while parent Runtime cancellation remains
        # visible through the linked event.
        TOOL_TIMEOUT = _tool_timeout_seconds(tool_definition, tool_input, tool_scheduler)
        outcome: dict[str, Any] = {}
        execution_clock = _ToolExecutionClock(TOOL_TIMEOUT, cancellation_event)

        def execute_tool() -> None:
            try:
                with bind_approval_wait(execution_clock):
                    from minicode.tool_recovery import is_native_search, retry_read
                    def invoke():
                        return tools.execute(tool_name, tool_input, tool_context)
                    outcome["result"] = (retry_read(invoke, cancellation=cancellation_event,
                        notify=on_tool_recovery, settled_search=True)
                        if is_native_search(tool_definition) else invoke())
            except BaseException as error:  # noqa: BLE001 - re-raised on the Worker thread.
                outcome["error"] = error
            finally:
                execution_clock.finish()

        execution_clock.start()
        if tool_effect is ToolEffect.READ_ONLY and (
            tool_definition is None or tool_definition.is_concurrency_safe
        ):
            completed = threading.Event()

            def execute_read_only_tool() -> None:
                try:
                    execute_tool()
                finally:
                    completed.set()

            tool_thread = threading.Thread(
                target=copy_context().run,
                args=(execute_read_only_tool,),
                name=f"minicode-tool-{tool_name}",
                daemon=True,
            )
            tool_thread.start()
            while (not completed.wait(0.05) and not execution_clock.expired.is_set()
                   and not cancellation_event.is_set()):
                pass
            if cancellation_event.is_set() and not execution_clock.expired.is_set():
                execution_clock.finish()
                result = ToolResult(ok=False, failureKind="canceled",
                    output=f"Tool '{tool_name}' canceled; " + (
                        "execution has exited" if completed.is_set() else "underlying read is still exiting"),
                    unsettledTool=not completed.is_set())
            elif execution_clock.expired.is_set():
                cancellation_event.set()
                result = ToolResult(
                    ok=False,
                    output=f"Tool '{tool_name}' timed out after {TOOL_TIMEOUT:g}s",
                    timedOut=True,
                    unsettledTool=not completed.is_set(),
                )
            elif "error" in outcome:
                error = outcome["error"]
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise error
                result = ToolResult(
                    ok=False,
                    output=f"Tool '{tool_name}' crashed ({type(error).__name__}): {error}",
                )
            else:
                result = outcome["result"]
        else:
            # Effectful tools and synchronous, non-concurrent read-only
            # consultations stay joined to the Worker thread. Never let
            # the parent resume writes while a child still reads its tree.
            execute_tool()
            from minicode.tools.task import is_settled_native_dispatch
            from minicode.tool_recovery import settled_command_result
            parent_cancel = (runtime or {}).get("_cancellationEvent")
            command_result = settled_command_result(tool_definition, outcome.get("result"),
                watchdog_expired=execution_clock.expired.is_set(),
                parent_canceled=bool(parent_cancel is not None and parent_cancel.is_set()))
            if command_result is not None:
                result = command_result
            elif execution_clock.expired.is_set() and is_settled_native_dispatch(tool_definition, outcome.get("result")):
                # Preparation cooperatively exited and every registration is
                # accounted for. Never replace its durable partial/full receipt
                # with a fabricated uncertainty, nor replay it automatically.
                result = outcome["result"]
            elif execution_clock.expired.is_set():
                uncertain = tool_effect is not ToolEffect.READ_ONLY
                result = ToolResult(
                    ok=False,
                    output=(
                        f"Tool '{tool_name}' exceeded its {TOOL_TIMEOUT:g}s timeout; "
                        + ("execution has now exited and its side effects are uncertain" if uncertain else "execution has now exited")
                        + ("\nOriginal tool result:\n" + outcome["result"].output if "result" in outcome else "")
                    ),
                    timedOut=True,
                    ambiguousSideEffect=uncertain,
                    unsettledTool=bool(getattr(outcome.get("result"), "unsettledTool", False)),
                    commandExitCode=getattr(outcome.get("result"), "commandExitCode", None),
                )
            elif "error" in outcome:
                error = outcome["error"]
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise error
                result = ToolResult(
                    ok=False,
                    output=f"Tool '{tool_name}' crashed ({type(error).__name__}): {error}",
                )
            else:
                result = outcome["result"]

        # Post-tool state updates (only for serial execution)
        from minicode.tool_recovery import is_native_search
        from minicode.tools.run_command import run_command_tool
        from minicode.tools.test_runner import test_runner_tool
        native_command_recovery = ((tool_definition is run_command_tool or tool_definition is test_runner_tool) and result.commandExecutionSettled
                                   and result.recoverableTimeout)
        if not native_command_recovery and (not is_native_search(tool_definition) or execution_clock.expired.is_set()):
            result = replace(result, recoverableTimeout=False)
        if tool_definition is not None:
            result = correction_guidance(tool_definition, tool_input, result)
        if retry_guard is not None:
            result = retry_guard.observe(call, result, tool_definition, cwd=cwd)
        if store:
            store.set_state(increment_tool_calls())
            store.set_state(set_idle())

        if on_tool_result:
            on_tool_result(tool_name, result.output, not result.ok)

        return result

    except (KeyboardInterrupt, SystemExit):
        # Always propagate these
        raise
    except Exception as exc:  # noqa: BLE001
        # Global safety net: catch ANY unexpected error in the tool execution
        # pipeline (hooks, state updates, permission checks, etc.) and convert
        # it to an error result. This prevents a single tool crash from
        # cascading into a full session failure.
        import traceback

        tb_excerpt = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)[-3:]
        ).strip()
        error_type = type(exc).__name__

        logger.error("Tool execution pipeline crashed (%s): %s", error_type, exc)

        # Ensure state is reset even on crash
        if store:
            try:
                store.set_state(set_idle())
            except Exception as reset_error:  # noqa: BLE001 - optional UI state must not replace the tool error.
                logger.warning("Could not reset tool UI state: %s", reset_error)

        return ToolResult(
            ok=False,
            output=f"[{error_type}] Tool execution pipeline crashed: {exc}\n"
            f"Traceback:\n{tb_excerpt}",
        )
