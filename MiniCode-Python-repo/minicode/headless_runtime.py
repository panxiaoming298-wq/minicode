"""Per-invocation Headless controls and authoritative, str-compatible outcomes."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import time
from typing import Any


@dataclass(frozen=True)
class HeadlessRuntimeOptions:
    profile: str | None = None
    orchestration: str | None = None
    max_tokens: int | None = None
    deadline_seconds: float | None = None
    task_file: str | None = None

    def __post_init__(self) -> None:
        if self.profile is not None and (not isinstance(self.profile, str) or self.profile not in {"standard", "deep", "ultra"}):
            raise ValueError("profile must be standard, deep or ultra")
        if self.orchestration is not None and (not isinstance(self.orchestration, str) or self.orchestration not in {"legacy", "primary"}):
            raise ValueError("runtime must be legacy or primary")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 9_007_199_254_740_991
        ):
            raise ValueError("max-tokens must be a positive integer <= 9007199254740991")
        if self.deadline_seconds is not None and (
            type(self.deadline_seconds) not in {int, float}
            or not math.isfinite(self.deadline_seconds) or not 0 < self.deadline_seconds <= 86_400
        ):
            raise ValueError("deadline-seconds must be finite, greater than 0 and at most 86400")
        if self.task_file is not None and (
            not isinstance(self.task_file, str) or not self.task_file.strip()
            or len(self.task_file) > 4096 or "\x00" in self.task_file
        ):
            raise ValueError("task-file must be a non-blank local file path without NUL")

    def kwargs(self) -> dict[str, Any]:
        return {name: value for name in ("profile", "orchestration", "max_tokens", "deadline_seconds", "task_file")
                if (value := getattr(self, name)) is not None}

    def apply(self, runtime: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        from minicode.unified_runtime import _as_bool, _finite_deadline, _optional_positive_int

        if not self.kwargs():
            return runtime
        result = dict(runtime)
        if self.profile is not None:
            result["executionProfile"] = self.profile
        if self.orchestration is not None:
            result.update(runtimeV2=True, runtimeOrchestration=self.orchestration,
                          runtimeDurableConsultation=self.orchestration == "primary")
            if self.orchestration == "primary":
                result.setdefault("runtimeMaxRepairRounds", 1)
            else:
                result["runtimeMaxRepairRounds"] = 0
        if not _as_bool(result.get("runtimeV2", True)) and (
            self.max_tokens is not None or self.deadline_seconds is not None
        ):
            raise ValueError("budget controls require the unified Runtime; specify --runtime legacy or primary")
        if self.max_tokens is not None:
            result["runtimeMaxTokens"] = self.max_tokens
        timestamp = time.time() if now is None else now
        if self.deadline_seconds is not None:
            result["runtimeDeadline"] = timestamp + self.deadline_seconds
        if self.orchestration == "primary":
            if _optional_positive_int(result, "runtimeMaxTokens") is None:
                raise ValueError("primary trial requires --max-tokens or a saved finite runtimeMaxTokens limit")
            deadline = _finite_deadline(result.get("runtimeDeadline"))
            if deadline is None or deadline <= timestamp:
                raise ValueError("primary trial requires --deadline-seconds or a saved future runtimeDeadline")
        return result


def load_headless_runtime(cwd: str, options: HeadlessRuntimeOptions) -> dict[str, Any]:
    from minicode.config import load_runtime_config
    from minicode.headless_task import load_headless_task

    task = load_headless_task(options.task_file, cwd) if options.task_file is not None else None
    # Select the policy while loading so saved primary-only limits are retained.
    # No process environment or persisted settings are modified.
    arguments = {"runtime_orchestration": options.orchestration} if options.orchestration is not None else {}
    runtime = options.apply(load_runtime_config(cwd, **arguments))
    return task.apply(runtime) if task is not None else runtime


def runtime_preview(runtime: Mapping[str, Any], *, allow_edits: bool = False) -> dict[str, Any]:
    from minicode.unified_runtime import (
        ProfileResolver, _as_bool, _durable_consultation, _finite_deadline,
        _optional_positive_float, _optional_positive_int, _primary_orchestration, _repair_limit,
    )

    enabled = _as_bool(runtime.get("runtimeV2", True))
    primary = _primary_orchestration(runtime)
    preview = {
        "status": "preflight_only", "scope": "this_headless_run", "model_called": False,
        "model": runtime.get("model"), "executionProfile": ProfileResolver.requested_mode(runtime).value,
        "runtimeV2": enabled, "runtimeOrchestration": "primary" if primary else "legacy",
        "effectiveOrchestration": ("primary" if primary else "legacy") if enabled else "disabled",
        "runtimeDurableConsultation": _durable_consultation(runtime),
        "runtimeMaxRepairRounds": _repair_limit(runtime),
        "runtimeMaxTokens": _optional_positive_int(runtime, "runtimeMaxTokens"),
        "runtimeMaxCost": _optional_positive_float(runtime, "runtimeMaxCost"),
        "runtimeDeadline": _finite_deadline(runtime.get("runtimeDeadline")),
        "autoApproveRequested": allow_edits,
        "permissionsChanged": False, "settingsChanged": False,
        "limitation": "Configuration preview only: no provider, tool, memory or task-topology validation. Selecting a mode does not grant permissions.",
    }
    if runtime.get("_headlessTaskFile") is not None:
        preview["taskFile"] = dict(runtime["_headlessTaskFile"])
        preview["taskFileWarning"] = "A real run executes the selected file's trusted verification commands. Preview does not run them or approve Agent tool calls. Expected outputs are not a write allowlist."
    return preview


class HeadlessResponse(str):
    """Preserve the text API while carrying this call's authoritative exit status.

    This is per-result metadata, not a global last-run status or model-generated
    control marker. JSON/text consumers still receive the unchanged reply text.
    """
    exit_code: int
    exit_code_source: str

    def __new__(cls, text: str, exit_code: int, exit_code_source: str):
        if type(exit_code) is not int or exit_code not in {0, 1}:
            raise ValueError("Headless exit_code must be 0 or 1")
        result = super().__new__(cls, text)
        result.exit_code = exit_code
        result.exit_code_source = exit_code_source
        return result


def headless_response(text: str, runtime: Mapping[str, Any] | None, *, error: bool = False) -> HeadlessResponse:
    from minicode.headless import _headless_response_exit_code
    from minicode.runtime_delivery import entry_completion
    from minicode.unified_runtime import _as_bool

    if error:
        return HeadlessResponse(text, 1, "exception")
    runtime = runtime or {}
    if "lastRuntimeTrace" in runtime:
        trace = runtime["lastRuntimeTrace"]
        completion = entry_completion(trace, expected_run_id=runtime.get("runtimeRunId"))
        # Preserve the established diagnostic for an explicitly null receipt;
        # an absent receipt on an enabled Runtime is handled below.
        source = "invalid_runtime_trace" if trace is None else completion.reason
        return HeadlessResponse(text, 0 if completion.completed else 1, source)
    if "runtimeV2" in runtime and _as_bool(runtime["runtimeV2"]):
        return HeadlessResponse(text, 1, "missing_runtime_trace")
    # Compatibility for older direct callers without a unified Runtime result.
    return HeadlessResponse(text, _headless_response_exit_code(text), "legacy_text")


def response_exit_code(response: str) -> int:
    if isinstance(response, HeadlessResponse):
        return response.exit_code
    from minicode.headless import _headless_response_exit_code
    return _headless_response_exit_code(response)
