from __future__ import annotations

import concurrent.futures
import json
import re
import threading
import time
import uuid
from copy import deepcopy
from contextvars import copy_context
from pathlib import Path
from typing import Any, Callable, Literal

from minicode.agent_intelligence import ErrorClassifier, NudgeGenerator, ToolScheduler
from minicode.runtime_messages import is_user_input, runtime_message, runtime_context_kind, runtime_context_message
from minicode.runtime_feedback import (
    NUDGE_CONTINUE, NUDGE_AFTER_TOOL_RESULT, NUDGE_AFTER_EMPTY_RESPONSE,
    NUDGE_AFTER_EMPTY_NO_TOOLS, RESUME_AFTER_PAUSE, RESUME_AFTER_MAX_TOKENS,
    runtime_feedback_message,
)

# Intelligence integration
from minicode.agent_metrics import AgentMetricsCollector
from minicode.capability_registry import CapabilityDomain, get_registry
from minicode.circuit_breaker import CompactionCircuitBreaker
from minicode.config import describe_fallback_guidance, describe_provider_channel

# 上下文管理集成 (Claude Code-style + Engineering Cybernetics)
from minicode.context_compactor import (
    AutoCompactConfig,
    ContextCompactor,
)
from minicode.context_cybernetics import ContextCyberneticsOrchestrator
from minicode.context_manager import ContextManager, estimate_message_tokens
from minicode.cost_control import CostControlLoop

# 工程控制论集成
from minicode.cybernetic_orchestrator import CyberneticOrchestrator
from minicode.cybernetic_supervisor import save_supervisor_report
from minicode.decision_audit import DecisionOutcome, get_auditor
from minicode.feedforward_controller import FeedforwardController

# Hooks integration
from minicode.hooks import HookEvent, fire_hook_sync

# Work chain integration
from minicode.logging_config import get_logger
from minicode.memory import MemoryManager

# 记忆注入和模型选择控制
from minicode.memory_injector import MemoryInjectionSignal, MemoryInjector
from minicode.micro_compact import MicroCompactor
from minicode.model_execution import _model_next
from minicode.provider_errors import ProviderStreamError
from minicode.model_registry import ModelSelectionSignal, detect_provider
from minicode.permissions import PermissionManager

# 任务进度控制
from minicode.progress_controller import ProgressAction
from minicode.execution_progress import CONTEXT_KIND as EXECUTION_PROGRESS_CONTEXT, ExecutionProgress
from minicode.provider_usage import current_usage_observation, observe_request_context, observe_request_tool_result, observe_request_tool_message, runtime_result_is_preview
from minicode.worker_audit import record_worker_audit
from minicode.runtime_profiles import resolve_runtime_profile
from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.self_healing_engine import SelfHealingEngine

# 智能路由与自省 (Phase 3 导入)
from minicode.smart_router import TaskOutcome
from minicode.state import AppState, Store, increment_tool_calls, set_busy, set_idle

# 高级控制论模块
from minicode.state_observer import MeasurementVector
from minicode.task_graph import TaskGraph
from minicode.task_graph import TaskState as GraphTaskState
from minicode.task_object import TaskState
from minicode.tool_execution import (
    ToolRetryGuard,
    _execute_single_tool,
    _LinkedCancellationEvent,  # noqa: F401 - compatibility export.
)
from minicode.tooling import ToolContext, ToolRegistry, ToolResult
from minicode.tool_input_recovery import ToolInputRecovery, request_contracts
from minicode.tool_recovery import ToolRecoveryTracker, terminal_timeout
from minicode.task_checks import TaskCheckEvidence
from minicode.turn_context import (
    _build_layered_context,
    _build_work_chain_task,
    _extract_task_description,  # noqa: F401 - compatibility export.
)
from minicode.turn_control import _apply_control_signal
from minicode.turn_events import AgentTurnCallbacks, AgentTurnEventSink, TurnEvent
from minicode.turn_kernel import (
    TurnPreludeState,
    TurnRecurrentState,
    TurnVerificationState,
    build_stable_task_pack,
    build_turn_coda_summary,
    build_widening_transition_nudge,
    decide_assistant_turn,
    decide_truncated_model_output,
    decide_tool_turn,
    derive_turn_step_policy,
    finalize_work_chain_task,
    render_turn_policy_message,
)
from minicode.turn_telemetry import current_telemetry, measured_turn
from minicode.types import (
    AgentStep,
    ChatMessage,
    ModelAdapter,
    RuntimeEvent,
    RuntimeEventCategory,
)
from minicode.usage_ledger import BudgetUnavailable, ConsultationBudgetLimited, ModelUsageUnreconciled
from minicode.context_borrowing import bind_context_borrowing
from minicode.working_memory import get_working_memory, protect_context

logger = get_logger("agent_loop")

STABLE_TASK_STATE_MARKER = "[Stable task state]"
_NETWORK_RESEARCH_TOOL_NAMES = frozenset({"web_search", "web_fetch"})
_CURRENT_LOOKUP_MARKERS = (
    "近况",
    "最新",
    "新闻",
    "消息",
    "近期",
    "最近",
    "实时",
    "下一场",
    "下一次",
    "什么时候",
    "赛程",
    "日程",
    "today",
    "latest",
    "current",
    "recent",
    "news",
    "update",
    "next race",
    "next match",
    "next game",
    "upcoming",
    "schedule",
    "calendar",
)
_DEEP_RESEARCH_MARKERS = (
    "深入",
    "深度",
    "全面",
    "详尽",
    "详细调研",
    "完整调研",
    "系统调研",
    "多方核实",
    "deep research",
    "comprehensive",
    "exhaustive",
)
_SCHEDULE_LOOKUP_MARKERS = (
    "下一场",
    "下一次",
    "什么时候",
    "赛程",
    "日程",
    "next race",
    "next match",
    "next game",
    "upcoming",
    "schedule",
    "calendar",
)
_SCHEDULE_FACT_PATTERNS = (
    r"\b20\d{2}[-/.]\d{1,2}(?:[-/.]\d{1,2})?\b",
    r"(?<!\d)\d{1,2}[-/]\d{1,2}(?!\d)",
    r"\d{1,2}月\d{1,2}日",
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2}\b",
    r"\b\d{1,2}(?:\s*[-–]\s*\d{1,2})?\s+"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b",
    r"\b\d{1,2}:\d{2}\b",
)
_MODEL_FALLBACK_ERROR_HINTS = (
    "no available channel",
    "temporarily unavailable",
    "service unavailable",
    "please try again later",
    "capacity exceeded",
    "overloaded",
    "high demand",
    "503",
    "502",
    "500",
    "connection refused",
    "connection reset",
    "timed out",
    "timeout",
)


def _latest_user_request(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if is_user_input(message):
            return str(message.get("content", "") or "").strip()
    return ""


def _network_research_policy(
    messages: list[ChatMessage],
    runtime: dict[str, Any],
    *,
    profile_name: str,
) -> tuple[int, bool]:
    """Return the per-turn network-call ceiling and whether early evidence stop applies."""

    request = _latest_user_request(messages).casefold()
    is_current_lookup = any(marker in request for marker in _CURRENT_LOOKUP_MARKERS)
    is_deep_research = any(marker in request for marker in _DEEP_RESEARCH_MARKERS)
    simple_current_lookup = is_current_lookup and not is_deep_research

    configured_limit = runtime.get("networkToolLimit")
    if configured_limit is not None:
        try:
            return max(1, min(int(configured_limit), 50)), simple_current_lookup
        except (TypeError, ValueError):
            pass
    if simple_current_lookup:
        # Three query attempts plus source reading/recovery, rather than three
        # searches consuming every opportunity to verify the original pages.
        return 6, True
    return (12 if profile_name == "single-deep" else 8), False


def _fetch_has_lookup_evidence(
    request: str,
    output: str,
    candidate_urls: set[str],
) -> bool:
    """Only count fetched evidence when it came from a strong search result."""

    fetched_url_match = re.search(r"(?m)^URL:\s*(https?://\S+)", output)
    if not fetched_url_match or not candidate_urls:
        return False
    fetched_url = fetched_url_match.group(1).rstrip("/").casefold()
    if fetched_url not in candidate_urls:
        return False
    lowered_request = request.casefold()
    if not any(marker in lowered_request for marker in _SCHEDULE_LOOKUP_MARKERS):
        return True
    return any(
        re.search(pattern, output, flags=re.IGNORECASE)
        for pattern in _SCHEDULE_FACT_PATTERNS
    )


def _with_reasoning_content(
    message: dict[str, Any],
    reasoning_content: str,
) -> dict[str, Any]:
    """Attach provider reasoning only when it is present.

    Keeping the field optional preserves the historical message shape for
    providers that do not expose reasoning content.
    """

    if reasoning_content:
        message["reasoningContent"] = reasoning_content
    return message


def _final_answer_message(content: str, runtime: dict[str, Any]) -> dict[str, Any]:
    """Bind public answer prose to the attempt that produced it, not the handoff summary."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if runtime.get("_runtimeActiveRunId"):
        message["phase"] = "final_answer"
        message.update({key: runtime[source] for key, source in (
            ("executionId", "_runtimeActiveRunId"),
            ("nodeId", "_runtimeNodeId"),
            ("attemptId", "_runtimeAttemptId"),
        ) if runtime.get(source)})
    return message


def _assistant_retry_messages(
    *, content: str, reasoning_content: str, user_content: str | None,
    feedback_kind: Literal["continuation"] | None = None,
) -> list[dict[str, Any]]:
    """Keep an empty or partial completion before the retry instruction.

    This is protocol history, not visible progress or a successful answer. The
    kernel still owns retry credits and the provider ledger owns admission.
    """
    messages: list[dict[str, Any]] = []
    if content or reasoning_content:
        messages.append(_with_reasoning_content(
            {"role": "assistant", "content": content}, reasoning_content,
        ))
    if user_content:
        messages.append(runtime_feedback_message(user_content, kind=feedback_kind))
    return messages


def _tool_call_message(call: dict[str, Any], reasoning_content: str) -> dict[str, Any]:
    """Preserve rejected raw arguments without treating them as executable input."""
    return _with_reasoning_content({"role": "assistant_tool_call", "toolUseId": call["id"],
        "toolName": call["toolName"], "input": call["input"],
        **({key: call[key] for key in ("inputError", "rawArguments") if key in call}
           if call.get("inputError") else {})}, reasoning_content)


def _incomplete_tool_batch_reason(model_step: AgentStep) -> str | None:
    """The native reason this batch may not execute, or ``None`` when it is fine.

    Two independently verifiable situations are incomplete: the provider said it
    truncated (its own length/max_tokens termination), or the response the Runtime
    received is not provably whole (missing terminal marker, an event that could
    not be decoded, or a call that could not be assembled). Everything else is a
    complete proposal and must not enter the non-executing rejection path.
    """
    diagnostics = model_step.diagnostics
    if diagnostics is None:
        return None
    if diagnostics.stopReason in _INCOMPLETE_FINISH_REASONS:
        return "model_output_truncated"
    if (diagnostics.transportComplete is False or diagnostics.networkComplete is False
            or diagnostics.parseComplete is False or diagnostics.assemblyComplete is False):
        return "response_parse_incomplete" if diagnostics.lostEventCount else "transport_incomplete"
    return None


def _rejected_truncated_tool_messages(
    model_step: AgentStep, *, on_event: Callable[[TurnEvent], None] | None = None, step: int | None = None,
    reason: str = "model_output_truncated", detail: str = "provider truncated this tool batch",
) -> list[dict[str, Any]]:
    """Journal an incomplete proposal without invoking tools, hooks or approval.

    Keep raw arguments/IDs, including malformed ones, as failed evidence. This
    batch is terminal and is never sent back as an automatically resumed call.
    It accepts only a provider-declared truncation or a response whose own
    transport/parse/assembly evidence says it is not whole: both mean "we do not
    have the whole proposal". A complete proposal is refused here rather than
    being silently turned into a non-executed batch.
    """
    if model_step.type != "tool_calls":
        raise ValueError("only provider-truncated or incomplete tool responses can enter this rejection path")
    if _incomplete_tool_batch_reason(model_step) is None:
        raise ValueError(
            "only a provider-truncated or transport/parse-incomplete tool response may enter this "
            "rejection path; a complete proposal must follow the normal execution protocol")
    return _refused_model_tool_messages(model_step, on_event=on_event, step=step, reason=reason, detail=detail)


def _refused_model_tool_messages(
    model_step: AgentStep, *, on_event=None, step=None, reason: str, detail: str,
) -> list[dict[str, Any]]:
    """Pair an unexecuted proposal with receipts; preserve its true refusal reason."""
    if model_step.type != "tool_calls":
        raise ValueError("only tool proposals have unexecuted call receipts")
    messages = _assistant_retry_messages(content=model_step.content,
        reasoning_content=model_step.reasoningContent, user_content=None)
    messages.extend(_tool_call_message(call, "") for call in model_step.calls)
    for call in model_step.calls:
        output = f"NotExecuted: {detail}; no call in the batch was executed or submitted"
        recovery = ({"status": "interrupted", "kind": "human_steering", "step": step}
                    if reason == "human_steered" else None)
        messages.append({"role": "tool_result", "toolUseId": call["id"], "toolName": call["toolName"],
                         "content": output, "isError": True,
                         **({"toolRecovery": recovery} if recovery else {})})
        if on_event is not None:
            try:
                event = TurnEvent.tool_rejected(step=step, tool_name=call["toolName"], tool_input=call["input"],
                    output=output, tool_call_id=call["id"], reason=reason)
                if recovery:
                    from dataclasses import replace
                    event = replace(event, tool_recovery=recovery)
                on_event(event)
            except Exception:
                logger.debug("Tool refusal observer failed", exc_info=True)
    return messages


# Provider terminations that mean "the provider stopped early", verbatim.
_INCOMPLETE_FINISH_REASONS = frozenset({"length", "max_tokens"})

_MODEL_FALLBACK_BLOCK_HINTS = (
    "unauthorized",
    "forbidden",
    "invalid api key",
    "authentication",
    "bad request",
    "invalid_request",
    "validation",
    "tool schema",
    "context length",
)


def _upsert_stable_task_state_message(
    messages: list[ChatMessage],
    stable_text: str,
) -> list[ChatMessage]:
    from minicode.runtime_messages import runtime_context_kind, runtime_context_message

    filtered = [
        message
        for message in messages
        if not (
            runtime_context_kind(message) == "task_state"
            or (message.get("role") == "system"
                and str(message.get("content", "")).startswith(STABLE_TASK_STATE_MARKER))
        )
    ]
    filtered.append(runtime_context_message(
        f"{STABLE_TASK_STATE_MARKER}\n{stable_text}", kind="task_state"))
    return filtered


def _should_attempt_model_fallback(error_message: str) -> bool:
    normalized = error_message.lower()
    if any(marker in normalized for marker in _MODEL_FALLBACK_BLOCK_HINTS):
        return False
    return any(marker in normalized for marker in _MODEL_FALLBACK_ERROR_HINTS)


def _looks_like_provider_availability_error(error_message: str) -> bool:
    normalized = error_message.lower()
    return any(
        marker in normalized
        for marker in (
            "no available channel",
            "temporarily unavailable",
            "service unavailable",
            "please try again later",
            "capacity exceeded",
            "overloaded",
            "high demand",
            "503",
            "502",
            "500",
        )
    )


def _summarize_model_api_failure(
    *,
    error_type: str,
    error: Exception,
    active_model_id: str = "",
    fallback_errors: list[str] | None = None,
    runtime: dict[str, Any] | None = None,
) -> str:
    fallback_errors = fallback_errors or []
    from minicode.candidate_ownership import CandidateConflict
    from minicode.workspace_provider import WorkspaceConflict

    if isinstance(error, (CandidateConflict, WorkspaceConflict)):
        return f"Local workspace error ({error_type}): {error}"
    if fallback_errors:
        combined = " ".join(fallback_errors)
        if "no viable fallback models were available" in combined.lower() and any(
            _looks_like_provider_availability_error(item)
            for item in fallback_errors + [str(error)]
        ):
            runtime = runtime or {}
            guidance_model = (
                str(runtime.get("configuredModel", "")).strip()
                or str(runtime.get("model", "")).strip()
                or active_model_id
                or "the active model"
            )
            model_label = guidance_model or active_model_id or "the active model"
            provider = (
                detect_provider(guidance_model, runtime).value
                if guidance_model
                else "unknown"
            )
            channel = describe_provider_channel(runtime, provider)
            guidance = describe_fallback_guidance(
                runtime,
                provider_name=provider,
                current_model=guidance_model,
            )
            guidance_suffix = f" Next step: {guidance[0]}" if guidance else ""
            return (
                f"Provider availability failure: {model_label} failed and all viable fallback models were unavailable. "
                f"Remaining blocker is upstream provider/channel availability, not a local retry loop. "
                f"Active channel: {channel}. Last error ({error_type}): {error}{guidance_suffix}"
            )
    return f"Model API error ({error_type}): {error}"


def _extract_model_id_from_provider_error(error: Exception) -> str:
    message = str(error)
    match = re.search(r"model\s+([^\s]+)\s+under\s+group", message, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


def _infer_active_model_id(
    model: ModelAdapter,
    runtime: dict[str, Any] | None,
    error: Exception | None = None,
) -> str:
    explicit = str(getattr(model, "model_id", "") or "").strip()
    if explicit:
        return explicit
    runtime_model = str((runtime or {}).get("model", "") or "").strip()
    if runtime_model:
        return runtime_model
    if error is not None:
        return _extract_model_id_from_provider_error(error)
    return ""


def _is_empty_assistant_response(content: str) -> bool:
    return len(content.strip()) == 0


def _register_tool_capabilities(tools: ToolRegistry) -> None:
    """Register existing tools as capabilities in the registry."""
    registry = get_registry()
    if registry.list_all():
        return
    for tool_name in tools.list_all():
        try:
            from minicode.capability_registry import CapabilityMetadata, CapabilityScope

            tool_def = tools.find(tool_name)
            if not tool_def:
                continue
            domain = CapabilityDomain.UNKNOWN
            if "file" in tool_name or "write" in tool_name or "read" in tool_name:
                domain = CapabilityDomain.FILE
            elif "search" in tool_name or "grep" in tool_name:
                domain = CapabilityDomain.SEARCH
            elif "web" in tool_name or "http" in tool_name or "fetch" in tool_name:
                domain = CapabilityDomain.WEB
            elif "command" in tool_name or "run" in tool_name or "exec" in tool_name:
                domain = CapabilityDomain.EXECUTION
            elif "code" in tool_name or "diff" in tool_name or "review" in tool_name:
                domain = CapabilityDomain.CODE
            elif "memory" in tool_name:
                domain = CapabilityDomain.MEMORY
            scope = CapabilityScope.READONLY
            if any(
                k in tool_name for k in ("write", "modify", "edit", "delete", "create")
            ):
                scope = CapabilityScope.WRITE
            if any(k in tool_name for k in ("command", "exec", "run")):
                scope = CapabilityScope.DESTRUCTIVE
            if any(k in tool_name for k in ("web", "fetch", "http")):
                scope = CapabilityScope.EXTERNAL
            metadata = CapabilityMetadata(
                name=tool_name,
                domain=domain,
                scope=scope,
                description=tool_def.description or f"Tool: {tool_name}",
                tags=["tool", tool_name],
            )
            registry.register(
                metadata,
                lambda **kw: tools.execute(
                    tool_name, kw, ToolContext(cwd=str(Path.cwd()))
                ),
                None,
            )
        except Exception as e:
            logger.debug("Failed to register tool %s as capability: %s", tool_name, e)


def _format_diagnostics(
    stop_reason: str | None,
    block_types: list[str] | None,
    ignored_block_types: list[str] | None,
) -> str:
    parts: list[str] = []
    if stop_reason:
        parts.append(f"stop_reason={stop_reason}")
    if block_types:
        parts.append(f"blocks={','.join(block_types)}")
    if ignored_block_types:
        parts.append(f"ignored={','.join(ignored_block_types)}")
    return f" Diagnostics: {'; '.join(parts)}." if parts else ""


def _is_recoverable_thinking_stop(
    *, is_empty: bool, stop_reason: str | None, ignored_block_types: list[str] | None
) -> bool:
    if not is_empty:
        return False
    if stop_reason not in {"pause_turn", "max_tokens"}:
        return False
    return "thinking" in (ignored_block_types or [])


def _should_treat_assistant_as_progress(
    *, kind: str | None, content: str, saw_tool_result: bool
) -> bool:
    if kind == "progress":
        from minicode.provider_usage import native_verifier_has_terminal_decision
        return not native_verifier_has_terminal_decision(content)
    if kind == "final":
        return False
    if not saw_tool_result:
        return False
    return False


# ── Preemptive context guard (Claude Code-style blocking limit) ─────────────


def _is_at_blocking_limit(
    token_count: int,
    context_window: int,
    *,
    effective_window_ratio: float = 0.90,
    min_reserve_tokens: int = 3_000,
) -> bool:
    """Preemptively block API calls when context is nearly full.

    Inspired by Claude Code's `isAtBlockingLimit` — prevents 413 errors
    by refusing to send a request when the context window is too full.

    The effective window is model context_window * effective_window_ratio,
    minus min_reserve_tokens (reserved for the assistant response).

    Returns True if the request would likely trigger a 413.
    """
    effective_window = int(context_window * effective_window_ratio)
    blocking_limit = max(1, effective_window - min_reserve_tokens)
    return token_count >= blocking_limit


def _compute_effective_blocking_limit(
    context_window: int,
    *,
    effective_window_ratio: float = 0.90,
    min_reserve_tokens: int = 3_000,
) -> int:
    effective_window = int(context_window * effective_window_ratio)
    return max(1, effective_window - min_reserve_tokens)


def _try_compact_with_breaker(
    breaker: CompactionCircuitBreaker,
    compact_fn: Callable[[], tuple[list, bool]],
    current_messages: list,
    logger_fn: Callable[..., None],
) -> tuple[list, bool]:
    """Run compaction through the circuit breaker.

    Returns (messages, effective) — messages is the (possibly compacted)
    list, effective indicates whether compaction actually changed anything.
    """
    if not breaker.is_allowed():
        logger_fn("Compaction blocked by circuit breaker (consecutive failures)")
        return current_messages, False
    try:
        result_messages, effective = compact_fn()
        if effective:
            breaker.record_success()
        return result_messages, effective
    except Exception as exc:
        breaker.record_failure()
        bs = breaker.get_state()
        logger_fn(
            "Compaction failed (breaker=%d/%d): %s",
            bs.consecutive_failures,
            breaker.config.failure_threshold,
            exc,
        )
        return current_messages, False


@measured_turn
def run_agent_turn(
    *,
    model: ModelAdapter,
    tools: ToolRegistry,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager | None = None,
    session: Any | None = None,
    store: Store[AppState] | None = None,
    max_steps: int = 50,
    on_tool_start: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, str, bool], None] | None = None,
    on_assistant_message: Callable[[str], None] | None = None,
    on_progress_message: Callable[[str], None] | None = None,
    on_runtime_event: Callable[[RuntimeEvent], None] | None = None,
    on_assistant_stream_chunk: Callable[[str], None] | None = None,
    on_thinking_chunk: Callable[[str], None] | None = None,
    context_manager: ContextManager | None = None,
    memory_manager: MemoryManager | None = None,
    runtime: dict | None = None,
    metrics_collector: AgentMetricsCollector | None = None,
    system_prompt: str = "",
    project_context: str = "",
    enable_work_chain: bool = True,
    callbacks: AgentTurnCallbacks | AgentTurnEventSink | None = None,
    paged_memory_runtime: Any | None = None,
    recover_submission_input: Callable[[dict, str], str] | None = None,
) -> list[ChatMessage]:
    # Prelude: prepare per-turn state before we enter the recurrent think/act loop.
    current_messages = list(messages)
    task_check_evidence = TaskCheckEvidence()
    native_submission_reminded = False
    telemetry = current_telemetry()
    runtime = runtime or {}
    configured_runtime_model = (
        str(runtime.get("configuredModel", "")).strip()
        or str(runtime.get("model", "")).strip()
        or str(getattr(model, "model_id", "") or "").strip()
    )
    if configured_runtime_model:
        runtime.setdefault("configuredModel", configured_runtime_model)
    runtime_profile = resolve_runtime_profile(runtime, fallback_max_steps=max_steps)
    turn_state = TurnRecurrentState(
        max_steps=runtime_profile.max_steps,
        profile_name=runtime_profile.name,
        widen_after_step=runtime_profile.widen_after_step,
        empty_response_retry_limit=runtime_profile.empty_response_retry_limit,
        recoverable_thinking_retry_limit=runtime_profile.recoverable_thinking_retry_limit,
        verification_state=TurnVerificationState(
            strict=runtime_profile.strict_step_verification,
            requires_explicit_final=runtime_profile.strict_step_verification,
        ),
    )
    tool_retry_guard = ToolRetryGuard()
    execution_progress = ExecutionProgress(cwd=cwd)
    input_recovery = ToolInputRecovery()
    recovery_tracker = ToolRecoveryTracker(runtime.get("_recoveryProjectRoot") or cwd)
    supplied_contracts = None
    max_steps = runtime_profile.max_steps
    from minicode.skill_runtime import SkillRuntime, bind_skill_runtime
    skill_runtime = SkillRuntime(cwd=cwd, runtime=runtime, session=session, permissions=permissions, tools=tools,
                                 task_query=_latest_user_request(current_messages))
    runtime["_skillRuntime"] = skill_runtime
    from minicode.paged_memory.service import (
        ArtifactPersistenceRequired,
        ContextBudgetExceeded,
    )
    from minicode.paged_memory.context_utilization import ContextRecoveryRequired
    from minicode.paged_memory.history_journal import HistoryIntegrityError

    owns_paged_memory_runtime = False
    active_paged_memory = paged_memory_runtime
    paged_memory_shadow_disabled = False
    if active_paged_memory is None:
        from minicode.paged_memory.service import PagedMemoryRuntime

        from minicode.context_manager import resolve_context_limit

        memory_context_limit = resolve_context_limit(configured_runtime_model, runtime)
        if context_manager is not None:
            memory_context_limit = min(memory_context_limit, context_manager.context_window)
        active_paged_memory = PagedMemoryRuntime.from_runtime(
            runtime=runtime,
            cwd=cwd,
            messages=current_messages,
            session=session,
            model_name=configured_runtime_model,
            model_context_limit=memory_context_limit,
        )
        owns_paged_memory_runtime = active_paged_memory is not None

    if active_paged_memory is not None:
        # Reusing a memory owner in another activation never reuses a loan.
        active_paged_memory.context_borrowing.stop()

    def run_paged_memory_operation(
        operation: str, *args: Any, **kwargs: Any
    ) -> Any | None:
        """Keep shadow failures observational; keep enabled failures fail-closed."""
        nonlocal paged_memory_shadow_disabled
        if active_paged_memory is None or paged_memory_shadow_disabled:
            return None
        try:
            return getattr(active_paged_memory, operation)(*args, **kwargs)
        except Exception as error:
            if active_paged_memory.controls_context:
                raise
            paged_memory_shadow_disabled = True
            logger.warning(
                "Paged-memory shadow path disabled after %s failed: %s",
                operation,
                error,
            )
            return None

    if paged_memory_runtime is not None:
        run_paged_memory_operation("record_initial_messages", current_messages)
        run_paged_memory_operation("configure_task_contract", runtime or {}, current_messages)
    legacy_context_management_allowed = not (
        active_paged_memory is not None and active_paged_memory.controls_context
    )
    from minicode.runtime_memory import primary_memory_owned

    coordinator_owns_memory = primary_memory_owned(runtime)

    paged_tool_receipts: dict[str, tuple[Any, str]] = {}
    paged_tool_receipts_lock = threading.Lock()

    def observe_raw_tool_result(call: dict, result: ToolResult) -> ToolResult | None:
        """Persist exact bytes before ToolRegistry's legacy prompt truncation."""
        history_observer = (runtime or {}).get("_sessionRawToolResult")
        if callable(history_observer):
            history_observer(call, result, node_id=str((runtime or {}).get("_runtimeNodeId", "")),
                             attempt_id=str((runtime or {}).get("_runtimeAttemptId", "")))
        if active_paged_memory is None or paged_memory_shadow_disabled:
            return None
        try:
            captured = run_paged_memory_operation(
                "capture_tool_result",
                call=call,
                output=result.output or "",
                ok=result.ok,
                read_sha256=result.contextReadSha256 or "",
            )
        except ArtifactPersistenceRequired as error:
            replacement = ToolResult(
                ok=False,
                output=str(error),
                awaitUser=True,
                waitKind="input",
                waitReason="artifact persistence requires user intervention",
            )
            captured = run_paged_memory_operation(
                "capture_tool_result",
                call=call,
                output=replacement.output,
                ok=False,
                force_inline=True,
            )
            if captured is not None:
                with paged_tool_receipts_lock:
                    paged_tool_receipts[str(call.get("id", ""))] = captured
            return replacement
        if captured is not None:
            active_paged_memory.context_utilization.observe_result(call, captured[1])
            with paged_tool_receipts_lock:
                paged_tool_receipts[str(call.get("id", ""))] = captured
        return None

    def run_memory_preflight(call: dict, effect: ToolEffect) -> str | None:
        if active_paged_memory is None or paged_memory_shadow_disabled:
            return None
        pressure_block = active_paged_memory.context_utilization.block_tool(call)
        if pressure_block:
            return pressure_block
        high_risk = effect is not ToolEffect.READ_ONLY
        audit = run_paged_memory_operation(
            "preflight",
            step_id=active_paged_memory.current_step_id,
            high_risk=high_risk,
            idempotency_key=str(call.get("id", "")) if high_risk else "",
            expected_contract_hash=(
                active_paged_memory.last_packet.debug_metadata.get("required_contract_hash")
                if active_paged_memory.last_packet is not None else None
            ),
        )
        if audit is None:
            return None
        if audit.allowed:
            return None
        return (
            "[Memory preflight blocked] The operation was not executed because "
            "required authoritative context could not be proven complete. "
            f"coverage={audit.resolved_count}/{audit.required_count}; "
            f"missing={audit.missing_dependency_ids}; "
            f"unresolved_sources={audit.unresolved_source_ids}; "
            f"errors={audit.errors}"
        )

    network_tool_limit, stop_network_when_evidence_ready = _network_research_policy(
        current_messages,
        runtime,
        profile_name=runtime_profile.name,
    )
    from minicode.network_research import NetworkResearchBudget

    network_budget = NetworkResearchBudget(
        network_tool_limit,
        reserve_fetches=stop_network_when_evidence_ready and tools.find("web_fetch") is not None,
    )
    relevant_search_succeeded = False
    strong_relevant_search_succeeded = False
    search_headline_evidence_ready = False
    successful_network_fetches = 0
    evidence_network_fetches = 0
    strong_search_result_urls: set[str] = set()
    network_guard_active = False
    network_evidence_notified = False
    network_search_guard_active = False
    adapter_tool_restorations: list[tuple[Any, Any]] = []
    switcher_tools_original: Any | None = None
    memory_tools_added = False
    rerank_fallback_reported = False
    memory_tool_names: frozenset[str] = frozenset()

    def bind_native_memory_tools(definitions):
        context = (runtime or {}).get("_runtimeContext")
        gateway = getattr(context, "delegation_gateway", context)
        binder = getattr(gateway, "bind_coordinator_memory_tools", None)
        if callable(binder):
            binder((runtime or {}).get("_runtimeNodeId"),
                (runtime or {}).get("_runtimeAttemptId"), definitions)

    tool_discovery = None
    # Presentation is independent of orchestration: the default full catalog
    # must stay discoverable without flooding legacy requests with schemas.
    if (tools.list()
            and ToolEffect.READ_ONLY.value in (runtime or {}).get("_allowedToolEffects", [ToolEffect.READ_ONLY.value])):
        from minicode.tool_discovery import ToolDiscovery

        tool_discovery = ToolDiscovery()
        tools = tools.with_additional_tools([tool_discovery.tool()])

    def replace_adapter_tools(adapter: Any, replacement: ToolRegistry) -> None:
        if not hasattr(adapter, "tools"):
            return
        if not any(
            saved_adapter is adapter for saved_adapter, _ in adapter_tool_restorations
        ):
            adapter_tool_restorations.append((adapter, getattr(adapter, "tools")))
        try:
            adapter.tools = replacement
        except Exception:
            logger.debug("Could not update adapter tool view", exc_info=True)

    def replace_switcher_tools(replacement: ToolRegistry) -> None:
        nonlocal switcher_tools_original
        if model_switcher is None or not hasattr(model_switcher, "_tools"):
            return
        if switcher_tools_original is None:
            switcher_tools_original = model_switcher._tools
        model_switcher._tools = replacement

    # Keep the historical callback parameters as the source of truth for
    # existing callers, while exposing one Rust-shaped structured event sink.
    # The structured sink is deliberately isolated from the legacy callbacks:
    # a UI/telemetry consumer must not be able to break the agent turn merely
    # because its event handler failed.
    legacy_on_tool_start = on_tool_start
    legacy_on_tool_result = on_tool_result
    legacy_on_assistant_message = on_assistant_message
    legacy_on_progress_message = on_progress_message
    legacy_on_runtime_event = on_runtime_event
    legacy_on_assistant_stream_chunk = on_assistant_stream_chunk
    legacy_on_thinking_chunk = on_thinking_chunk

    def publish_turn_event(event: TurnEvent) -> None:
        if callbacks is None:
            return
        try:
            on_event = getattr(callbacks, "on_event", None)
            if callable(on_event):
                on_event(event)
                return

            # Also accept a Rust-style object that implements the four named
            # methods but has no aggregate ``on_event`` method.
            callback_name = {
                "tool_start": "on_tool_start",
                "tool_result": "on_tool_result",
                "assistant": "on_assistant_message",
                "progress": "on_progress_message",
                "runtime": "on_runtime_event",
                "assistant_stream": "on_assistant_stream_chunk",
                "thinking": "on_thinking_chunk",
            }.get(event.kind)
            handler = getattr(callbacks, callback_name, None) if callback_name else None
            if not callable(handler):
                return
            if event.kind == "tool_start":
                handler(event.tool_name, event.tool_input)
            elif event.kind == "tool_result":
                handler(event.tool_name, event.output, event.is_error)
            elif event.kind == "runtime" and event.runtime_event is not None:
                handler(event.runtime_event)
            else:
                handler(event.content)
        except Exception:  # noqa: BLE001
            logger.debug(
                "Structured agent-turn callback failed for event=%s",
                event.kind,
                exc_info=True,
            )

    # Tool callbacks run on each invocation's executor thread. Keep a stack
    # per thread so parallel same-name calls (and nested calls) cannot collide.
    tool_event_context = threading.local()
    live_concurrent_events = bool(getattr(callbacks, "thread_safe_tool_events", False))

    def execute_call(call, registry, call_cwd, call_permissions, call_session, call_runtime, *args, **kwargs):
        # Preserve the provider's call identity across live events and saved
        # messages. The thread-local binding also covers nested/concurrent calls.
        previous = getattr(tool_event_context, "next_call_id", "")
        tool_event_context.next_call_id = call["id"]
        try:
            invocation_step = turn_state.step
            execution_runtime = {**call_runtime, "_taskCheckEvidence": task_check_evidence, "_networkResearchAdmission":
                lambda name, call_id: network_budget.admit(name, f"{invocation_step}:{call_id}")}
            return _execute_single_tool(call, registry, call_cwd, call_permissions, call_session,
                execution_runtime, *args, **kwargs, supplied_contracts=supplied_contracts,
                on_tool_recovery=lambda metadata: publish_turn_event(TurnEvent(
                    kind="tool_recovery", step=turn_state.step, tool_name=call["toolName"],
                    tool_call_id=call["id"], tool_recovery={**metadata, "step": turn_state.step})),
                on_tool_images=lambda images: publish_turn_event(TurnEvent(
                    kind="tool_images", step=invocation_step, tool_name=call["toolName"],
                    tool_call_id=call["id"], images=tuple(images))))
        finally:
            tool_event_context.next_call_id = previous

    def on_tool_start(tool_name: str, tool_input: dict) -> None:
        calls = getattr(tool_event_context, "calls", [])
        call_id = getattr(tool_event_context, "next_call_id", "") or uuid.uuid4().hex
        tool_event_context.next_call_id = ""
        calls.append(call_id)
        tool_event_context.calls = calls
        publish_turn_event(
            TurnEvent.tool_started(
                step=turn_state.step or None,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_call_id=call_id,
            )
        )
        if legacy_on_tool_start:
            legacy_on_tool_start(tool_name, tool_input)

    def on_tool_result(tool_name: str, output: str, is_error: bool) -> None:
        calls = getattr(tool_event_context, "calls", [])
        call_id = calls.pop() if calls else uuid.uuid4().hex
        publish_turn_event(
            TurnEvent.tool_finished(
                step=turn_state.step or None,
                tool_name=tool_name,
                output=output,
                is_error=is_error,
                tool_call_id=call_id,
            )
        )
        if legacy_on_tool_result:
            legacy_on_tool_result(tool_name, output, is_error)

    def on_assistant_message(content: str, *, public: bool = False) -> None:
        publish_turn_event(
            TurnEvent.assistant_message(
                step=turn_state.step or None,
                content=content,
                public=public,
            )
        )
        if legacy_on_assistant_message:
            legacy_on_assistant_message(content)

    def on_progress_message(
        content: str, *, public: bool = False,
        progress_status: Literal["running", "complete", "failed", "stopped"] | None = None,
    ) -> None:
        publish_turn_event(
            TurnEvent.progress_message(
                step=turn_state.step or None,
                content=content,
                public=public,
                progress_status=progress_status,
            )
        )
        if legacy_on_progress_message:
            legacy_on_progress_message(content)

    def on_runtime_event(event: RuntimeEvent) -> None:
        publish_turn_event(
            TurnEvent.runtime_message(
                step=turn_state.step or event.step,
                event=event,
            )
        )
        if legacy_on_runtime_event:
            legacy_on_runtime_event(event)

    def on_assistant_stream_chunk(content: str) -> None:
        publish_turn_event(
            TurnEvent.assistant_stream_chunk(
                step=turn_state.step or None,
                content=content,
            )
        )
        if legacy_on_assistant_stream_chunk:
            legacy_on_assistant_stream_chunk(content)

    def on_thinking_chunk(content: str) -> None:
        publish_turn_event(
            TurnEvent.thinking_chunk(
                step=turn_state.step or None,
                content=content,
            )
        )
        if legacy_on_thinking_chunk:
            legacy_on_thinking_chunk(content)

    # Provider adapters use ``None`` to select their non-streaming protocol.
    # Do not turn an absent callback into a truthy no-op wrapper: that changes
    # the wire format for callers that never asked for streaming.
    if callbacks is None:
        if legacy_on_tool_start is None:
            on_tool_start = None
        if legacy_on_tool_result is None:
            on_tool_result = None
        if legacy_on_assistant_message is None:
            on_assistant_message = None
        if legacy_on_progress_message is None:
            on_progress_message = None
        if legacy_on_runtime_event is None:
            on_runtime_event = None
    # Structured callbacks observe stream/thinking chunks when a caller has
    # already enabled those legacy channels; merely subscribing to turn
    # events must not silently change a provider from non-streaming to
    # streaming mode.
    if legacy_on_assistant_stream_chunk is None and not bool(
        getattr(callbacks, "include_stream_chunks", False)
    ):
        on_assistant_stream_chunk = None
    if legacy_on_thinking_chunk is None and not bool(
        getattr(callbacks, "include_thinking_chunks", False)
    ):
        on_thinking_chunk = None

    def emit_runtime_event(
        *,
        category: RuntimeEventCategory,
        message: str,
        emit_progress: bool = True,
        stop_reason: str = "",
        widening_reason: str = "",
        evidence_summary: str = "",
    ) -> None:
        policy = turn_state.step_policy
        event = RuntimeEvent(
            category=category,
            message=message,
            step=turn_state.step or None,
            profile=runtime_profile.name,
            phase=policy.phase if policy is not None else "",
            verification_focus=(
                policy.verification_focus if policy is not None else ""
            ),
            stop_reason=stop_reason,
            widening_reason=widening_reason,
            evidence_summary=evidence_summary,
        )
        if on_runtime_event:
            on_runtime_event(event)
        if emit_progress and on_progress_message:
            on_progress_message(message)

    def on_provider_recovery(diagnostic: dict[str, Any]) -> None:
        record_worker_audit("provider_stream_recovery", step=turn_state.step, **diagnostic)
        status = diagnostic["status"]
        if status != "recovered":
            publish_turn_event(TurnEvent(kind="model_status", step=turn_state.step, content="interrupted"))
        if status == "retrying":
            message = (f"上游模型输出中断，正在重试（{diagnostic['retry']}/{diagnostic['max_retries']}）。"
                       "已完成的工具结果已保留。")
        elif status == "recovered":
            message = "模型请求已恢复，继续执行任务。"
        else:
            return  # The typed terminal error below supplies the stop notice.
        emit_runtime_event(category="recovery", message=message,
                           evidence_summary=json.dumps(diagnostic, ensure_ascii=False))
        if status == "retrying":
            publish_turn_event(TurnEvent(kind="model_status", step=turn_state.step, content="waiting"))

    tool_scheduler = ToolScheduler(metrics_collector=metrics_collector)

    prelude = TurnPreludeState(auditor=get_auditor() if enable_work_chain else None)

    # 工程控制论控制器初始化（通过 Orchestrator 统一管理）
    orch: CyberneticOrchestrator | None = None
    feedback_controller: Any = None
    feedforward_controller: Any = None
    stability_monitor: Any = None
    cybernetic_supervisor: Any = None

    adaptive_pid_tuner: Any = None
    state_observer: Any = None
    decoupling_controller: Any = None
    predictive_controller: Any = None
    self_healing_engine: Any = None
    progress_controller: Any = None
    memory_injection_ctrl: Any = None
    model_selection_ctrl: Any = None
    smart_router: Any = None
    reflection_engine: Any = None
    model_switcher: Any = None
    memory_injector: Any = None

    # These objects are created by the work-chain/context branches below but
    # are also consulted by the common prelude/coda paths.  Keep the disabled
    # mode a valid lightweight execution path instead of relying on branch
    # local assignment side effects.
    context_compactor: ContextCompactor | None = None
    context_cybernetics: ContextCyberneticsOrchestrator | None = None
    memory_mgr: MemoryManager | None = None
    micro_compactor: MicroCompactor | None = MicroCompactor()
    compaction_breaker: CompactionCircuitBreaker | None = CompactionCircuitBreaker()
    cost_control: CostControlLoop | None = None

    if enable_work_chain:
        prelude.task, prelude.task_metadata = _build_work_chain_task(current_messages)
        if prelude.task:
            prelude.task_graph = TaskGraph(name=f"turn-{prelude.task.id}")
            graph_task = prelude.task_graph.add_task(
                name=prelude.task.title or prelude.task.id,
                description=prelude.task.goal or prelude.task.description,
            )
            prelude.task_graph_id = graph_task.id
            slot = prelude.task_graph.assign_slot(graph_task.id, slot_name="turn")
            prelude.task_slot_key = f"{slot.slot_name}:{slot.task_id}"
            prelude.task_graph.start_task(prelude.task_slot_key)
        prelude.layered_context, prelude.context_builder = _build_layered_context(
            current_messages,
            system_prompt,
            project_context,
            prelude.task,
        )
        _register_tool_capabilities(tools)

        # 初始化所有工程控制论控制器（通过 Orchestrator 统一管理）
        orch = CyberneticOrchestrator()
        orch.initialize(model, tools, runtime)
        feedback_controller = orch.feedback
        cybernetic_supervisor = orch.cyber_supervisor
        stability_monitor = orch.stability
        adaptive_pid_tuner = orch.adaptive_tuner
        state_observer = orch.state_observer
        decoupling_controller = orch.decoupling
        predictive_controller = orch.predictive
        progress_controller = orch.progress
        memory_injection_ctrl = orch.memory_ctrl
        model_selection_ctrl = orch.model_ctrl
        smart_router = orch.smart_router
        reflection_engine = orch.reflection
        model_switcher = orch.model_switcher
        logger.info("CyberneticOrchestrator: %d controllers initialized", 15)
        if smart_router and prelude.task:
            try:
                current_model_id = model.model_id if hasattr(model, "model_id") else ""
                task_text = (
                    prelude.task.raw_input
                    if hasattr(prelude.task, "raw_input")
                    else str(current_messages[-1].get("content", ""))
                )
                routing, switch_result = smart_router.route_and_switch(
                    task_text,
                    current_model=current_model_id,
                )
                logger.info(
                    "SmartRouter: model=%s tier=%s cost=$%.4f reason=%s",
                    routing.selected_model,
                    routing.tier_name,
                    routing.estimated_cost,
                    routing.reasoning[:80],
                )
                # 如果路由推荐了不同模型且切换成功，更新 model 引用
                if switch_result and switch_result.success:
                    model = switch_result.adapter
                    logger.info(
                        "SmartRouter: switched model %s -> %s",
                        switch_result.old_model,
                        switch_result.new_model,
                    )
            except Exception:
                pass

        # 初始化前馈控制器（预判式优化）
        if prelude.task:
            feedforward_controller = FeedforwardController()
            preemptive_config = feedforward_controller.preconfigure(
                prelude.task.parsed_intent, prelude.task.raw_input
            )
            risk_assessment = feedforward_controller.assess_risks(
                prelude.task.parsed_intent, preemptive_config
            )
            logger.info(
                "Feedforward control: config=%s risk=%s",
                preemptive_config.recommended_model,
                risk_assessment.risk_level,
            )
            # Apply feedforward preemptive config to execution parameters
            if preemptive_config.confidence > 0.6 and turn_state.max_steps is not None:
                turn_state.max_steps = min(
                    turn_state.max_steps,
                    preemptive_config.max_turn_steps,
                )
                max_steps = turn_state.max_steps
                logger.info(
                    "Feedforward: max_steps=%d model=%s timeout=%.1fs",
                    preemptive_config.max_turn_steps,
                    preemptive_config.recommended_model,
                    preemptive_config.tool_timeout_seconds,
                )
            if risk_assessment.risk_level in ("high", "critical"):
                logger.warning(
                    "Feedforward risk assessment: level=%s probability=%.2f risks=%s",
                    risk_assessment.risk_level,
                    risk_assessment.estimated_failure_probability,
                    ", ".join(risk_assessment.identified_risks[:3]),
                )

        # 模型选择控制器：根据任务特征推荐模型
        if model_selection_ctrl and prelude.task:
            try:
                model_signal = ModelSelectionSignal(
                    task_complexity=getattr(prelude.task, "complexity", "moderate")
                    if hasattr(prelude.task, "complexity")
                    else "moderate",
                    budget_pressure=current_usage_observation()["budget_pressure"],
                    latency_pressure=min(1.0, telemetry.average("model") / 60.0),
                    recent_failures=0,
                    current_model=model.model_id if hasattr(model, "model_id") else "",
                )
                model_decision = model_selection_ctrl.decide(model_signal)
                logger.info(
                    "ModelSelectionController: model=%s score=%.2f effort=%s reasons=%s",
                    model_decision.model,
                    model_decision.score,
                    model_decision.reasoning_effort.value,
                    ", ".join(model_decision.reasons),
                )
            except Exception:
                pass

        # 初始化上下文管理器 (Claude Code-style + Engineering Cybernetics)
        # 必须在 SelfHealingEngine 之前初始化，因为自愈引擎需要委托压缩操作
        if context_manager and legacy_context_management_allowed:
            compact_config = AutoCompactConfig(
                threshold_ratio=0.85,
                circuit_breaker_limit=3,
                session_memory_enabled=True,
            )
            memory_mgr = (
                memory_manager
                if memory_manager is not None
                else MemoryManager(project_root=cwd)
            )
            if session is not None:
                memory_mgr.source_session_id = session.session_id
                memory_mgr.source_turn_id = str((runtime or {}).get("_webTurnId") or memory_mgr.source_turn_id)
            # 将 memory_mgr 注入 ReflectionEngine，使自省经验持久化
            if reflection_engine:
                reflection_engine.memory = memory_mgr
            # 初始化 MemoryInjector，将控制论决策落地为实际记忆注入
            # 同时创建 Reranker（使用真实 LLM 做记忆策展）
            memory_reranker = None
            try:
                from minicode.memory_reranker import MemoryReranker

                # Use the agent's model for reranking (lightweight prompt, ~500 tokens)
                memory_reranker = MemoryReranker(model_adapter=model)
            except Exception:
                pass
            memory_injector = MemoryInjector(
                memory_manager=memory_mgr,
                controller=memory_injection_ctrl,
                reranker=memory_reranker,
            )
            if orch and not coordinator_owns_memory:
                orch._last_model = model
                orch._workspace = cwd
                orch.wire_memory(memory_mgr)
                if orch.memory_pipeline is not None:
                    memory_injector = getattr(
                        orch.memory_pipeline, "_injector", memory_injector
                    )
            # 记忆注入控制器：根据上下文压力决定注入策略
            if memory_injection_ctrl:
                try:
                    inj_signal = MemoryInjectionSignal(
                        context_usage=context_manager.get_stats().usage_percentage
                        / 100.0,
                        retrieval_quality=0.5,
                        recent_failure=False,
                    )
                    inj_decision = memory_injection_ctrl.decide(
                        inj_signal,
                        base_max_memories=5,
                        base_min_relevance=0.3,
                        base_max_tokens=200,
                    )
                    logger.info(
                        "MemoryInjectionController: mode=%s max_mem=%d min_rel=%.2f max_tok=%d",
                        inj_decision.mode.value,
                        inj_decision.max_memories,
                        inj_decision.min_relevance,
                        inj_decision.max_tokens_per_memory,
                    )
                except Exception:
                    pass
            # 执行实际记忆注入：将相关记忆注入到系统 prompt 中
            if legacy_context_management_allowed and orch and prelude.task:
                try:
                    task_desc = (
                        prelude.task.raw_input
                        if hasattr(prelude.task, "raw_input")
                        else ""
                    )
                    current_messages = orch.inject_memories(task_desc, current_messages)
                except Exception:
                    pass
            elif legacy_context_management_allowed and memory_injector and prelude.task:
                try:
                    task_desc = (
                        prelude.task.raw_input
                        if hasattr(prelude.task, "raw_input")
                        else ""
                    )
                    injected = memory_injector.inject_for_task(task_desc)
                    if injected:
                        logger.info(
                            "MemoryInjector: injected %d memories (mode=%s)",
                            len(injected),
                            memory_injector._last_decision.mode.value
                            if memory_injector._last_decision
                            else "?",
                        )
                        # 将注入的记忆追加到系统 prompt
                        memory_context = "\n## Injected Memory\n" + "\n".join(
                            f"- {m.content[:200]}" for m in injected[:5]
                        )
                        for i, msg in enumerate(current_messages):
                            if msg.get("role") == "system":
                                current_messages[i] = {
                                    **msg,
                                    "content": msg["content"] + memory_context,
                                }
                                break
                except Exception:
                    pass
            context_compactor = ContextCompactor(
                context_window=context_manager.context_window,
                workspace=cwd,
                memory_manager=memory_mgr,
                estimate_fn=estimate_message_tokens,
                config=compact_config,
            )
            context_cybernetics = ContextCyberneticsOrchestrator(
                context_compactor,
                kp=2.0,
                ki=0.15,
                kd=0.3,
                pid_setpoint=0.70,
                base_threshold=0.85,
                safety_margin_turns=3,
                enabled=True,
            )
            if (
                prelude.task
                and hasattr(prelude.task, "parsed_intent")
                and prelude.task.parsed_intent
            ):
                context_cybernetics.set_intent(
                    str(prelude.task.parsed_intent.intent_type)
                )
            logger.info(
                "ContextCybernetics initialized: PID control loop + predictive guard"
            )
            if orch:
                orch.context_compactor = context_compactor
                orch.context_cybernetics = context_cybernetics

        # 初始化自愈引擎（接收 cybernetics 引用用于 CONTEXT_OVERFLOW 委托）
        if orch:
            orch.wire_healing(tool_scheduler, context_compactor)
            self_healing_engine = orch.healing
        else:
            self_healing_engine = SelfHealingEngine(
                orchestrator=context_cybernetics,
                tool_scheduler=tool_scheduler,
                compactor=context_compactor,
            )
        logger.info(
            "Self-healing engine initialized: automated recovery + compaction delegation"
        )

        # ── Micro-compaction + circuit breaker (CC-style layered defense) ─────
        logger.info(
            "Micro-compactor + circuit breaker initialized for layered context defense"
        )

        # 初始化成本控制闭环 (CostTracker → PID → ToolResultBudgetManager)
        cost_control = orch.cost_control if orch else None
        if cost_control is None:
            cost_control = CostControlLoop(
                target_cost_per_min=0.50,
                kp=1.5,
                ki=0.08,
                kd=0.2,
                enabled=True,
            )
        if orch:
            orch.cost_control = cost_control
        logger.info(
            "CostControlLoop initialized: BudgetPIDController for cost regulation"
        )

    # 检查上下文状态 + 运行 Claude Code-style 预请求优化管线
    if context_manager and legacy_context_management_allowed:
        context_manager.messages = current_messages
        stats = context_manager.get_stats()
        logger.info(
            "Context: %d tokens (%.0f%%), %d messages",
            stats.total_tokens,
            stats.usage_percentage,
            stats.messages_count,
        )

        # ── Layer 1: Micro-compaction (lightest defense) ───────────────────
        current_messages, mc_stats = micro_compactor.compact(current_messages)
        if mc_stats.reason != "no_action":
            context_manager.messages = current_messages
            logger.info(
                "MicroCompact: %s — %d → %d messages",
                mc_stats.reason,
                mc_stats.messages_before,
                mc_stats.messages_after,
            )

        # 运行控制论闭环优化管线 (Sense → Predict → Control → Act → Learn)
        if context_cybernetics:
            usage_observation = current_usage_observation()
            if cost_control and usage_observation["cost"] is not None:
                measured_cost = usage_observation["cost"]
                adj = cost_control.run(
                    cost_usd=measured_cost,
                    total_tokens=usage_observation["tokens"],
                    total_calls=usage_observation["model_calls"],
                )
                if (
                    context_compactor
                    and hasattr(context_compactor, "_tool_budget")
                    and context_compactor._tool_budget
                ):
                    cost_control.apply_to_budget_manager(context_compactor._tool_budget)
                elif adj and adj.budget_multiplier < 0.8:
                    logger.warning(
                        "CostControl: budget tightened (mult=%.2f reason=%s) but no compactor active",
                        adj.budget_multiplier,
                        adj.reason,
                    )

            if compaction_breaker.is_allowed():
                try:
                    cyber_messages, cyber_result, cyber_action = (
                        context_cybernetics.run_cycle(
                            current_messages,
                            error_rate=float(turn_state.tool_error_count)
                            / max(turn_state.step, 1)
                            if turn_state.step > 0
                            else 0.0,
                            avg_latency=telemetry.average("model"),
                            turn_id=turn_state.step,
                        )
                    )
                except Exception as exc:
                    compaction_breaker.record_failure()
                    logger.warning("Cybernetics compaction raised: %s", exc)
                    cyber_result = None
                if cyber_result and cyber_result.effective:
                    compaction_breaker.record_success()
                    current_messages = cyber_messages
                    context_manager.messages = current_messages
                    logger.info(
                        "Cybernetics[%s]: %s intensity=%.2f freed=%d tokens [%s]",
                        cyber_action.reason if cyber_action else "unknown",
                        cyber_result.strategy.value,
                        cyber_action.compaction_intensity if cyber_action else 0,
                        cyber_result.tokens_freed,
                        cyber_result.summary_text[:80]
                        if cyber_result.summary_text
                        else "",
                    )
            else:
                logger.warning("Cybernetics compaction blocked by circuit breaker")
        elif context_compactor:
            if compaction_breaker.is_allowed():
                try:
                    compaction_result = context_compactor.process_request(
                        current_messages
                    )
                except Exception as exc:
                    compaction_breaker.record_failure()
                    logger.warning("Compactor raised: %s", exc)
                    compaction_result = None
                if compaction_result and compaction_result.effective:
                    compaction_breaker.record_success()
                    current_messages = compaction_result.messages
                    context_manager.messages = current_messages
                    logger.info(
                        "ContextCompactor: %s freed %d tokens [%s]",
                        compaction_result.strategy.value,
                        compaction_result.tokens_freed,
                        compaction_result.summary_text[:80],
                    )
            else:
                logger.warning("Compactor blocked by circuit breaker")
        elif context_manager.should_auto_compact():
            if compaction_breaker.is_allowed():
                try:
                    logger.warning("Context near limit, auto-compacting...")
                    current_messages = getattr(
                        context_manager, "compact_messages", lambda: current_messages
                    )()
                    compaction_breaker.record_success()
                except Exception as exc:
                    compaction_breaker.record_failure()
                    logger.warning("Auto-compact failed: %s", exc)
                if on_assistant_message:
                    on_assistant_message(context_manager.get_context_summary())
            else:
                logger.warning("Auto-compact blocked by circuit breaker")

    try:
        # Recurrent kernel: repeated think/act/observe iterations over one turn.
        explicit_skill = runtime.pop("_userSkillInvocation", None)
        if explicit_skill is not None:
            call_id = "skill-user-" + uuid.uuid4().hex
            current_messages.append({"role": "assistant_tool_call", "toolUseId": call_id,
                "toolName": "load_skill", "input": explicit_skill})
            allowed = runtime.get("_allowedToolEffects")
            result = tools.execute("load_skill", explicit_skill, ToolContext(cwd=cwd,
                permissions=permissions, session=session, _runtime=runtime,
                allowed_effects=frozenset(ToolEffect(value) for value in allowed) if allowed is not None else None,
                node_id=runtime.get("_runtimeNodeId"), attempt_id=runtime.get("_runtimeAttemptId"),
                run_id=runtime.get("_runtimeActiveRunId"), tool_call_id=call_id, skill_user_invocation=True))
            current_messages.append({"role": "tool_result", "toolUseId": call_id,
                "toolName": "load_skill", "content": result.output, "isError": not result.ok})
            if not result.ok:
                turn_state.set_stop_reason("blocked")
                current_messages.append({"role": "assistant", "content": result.output})
                return current_messages
        while turn_state.has_remaining_steps():
            parent_cancellation = (runtime or {}).get("_cancellationEvent")
            if callable(getattr(parent_cancellation, "is_set", None)) and (
                parent_cancellation.is_set()
            ):
                turn_state.set_stop_reason("canceled")
                emit_runtime_event(
                    category="stop",
                    message="Parent Runtime cancellation requested",
                    emit_progress=False,
                    stop_reason="canceled",
                )
                return current_messages
            agent_boundary = runtime.get("_agentBoundary")
            if callable(agent_boundary):
                agent_boundary(current_messages, context_manager=context_manager)
            user_boundary = runtime.get("_userBoundary")
            if callable(user_boundary):
                user_boundary(current_messages, context_manager=context_manager)
            step = turn_state.begin_step()
            previous_policy = turn_state.step_policy
            current_policy = derive_turn_step_policy(
                turn_state, read_only=runtime.get("_allowedToolEffects") == ["read_only"],
            )
            policy_message = render_turn_policy_message(
                previous_policy=previous_policy,
                current_policy=current_policy,
            )
            if active_paged_memory is not None:
                run_paged_memory_operation(
                    "begin_step",
                    step,
                    user_input=_latest_user_request(current_messages),
                    phase=str(
                        getattr(current_policy, "phase", "EXECUTION")
                        or "EXECUTION"
                    ),
                )
            if policy_message:
                turn_state.set_progress_summary(policy_message)
                emit_runtime_event(category="phase", message=policy_message)
                logger.info("Turn policy update: %s", policy_message)
            if (
                current_policy.should_compact_aggressively
                and legacy_context_management_allowed
                and context_manager
                and context_manager.should_auto_compact()
                and compaction_breaker.is_allowed()
            ):
                try:
                    current_messages = getattr(
                        context_manager, "compact_messages", lambda: current_messages
                    )()
                    compaction_breaker.record_success()
                except Exception as exc:
                    compaction_breaker.record_failure()
                    logger.warning("Aggressive compaction failed: %s", exc)
                emit_runtime_event(
                    category="compaction",
                    message="Compacted context for the current runtime phase.",
                )
            protected_context = get_working_memory().get_protected_content()
            turn_state.stable_task_pack = build_stable_task_pack(
                task=prelude.task,
                task_metadata=prelude.task_metadata,
                protected_context=protected_context,
                task_graph=prelude.task_graph,
                task_slot_key=prelude.task_slot_key,
                latest_tool_result_summary=turn_state.latest_tool_result_summary,
                progress_state=turn_state.progress_state,
                verification_state=turn_state.verification_state,
                budget_signals=turn_state.budget_signals,
            )
            # A native paged Worker already carries its typed task state,
            # required contract and original sources. Do not add the legacy
            # intent classifier's "unknown/evidence-ready" summary as a second
            # system-level task authority. Preserve its path whenever protected
            # working context could contain additional requirements.
            native_worker_state = (
                active_paged_memory is not None and active_paged_memory.controls_context
                and active_paged_memory.current_mode.value != "INLINE"
                and isinstance(runtime.get("_memoryWorkerContract"), dict)
                and not protected_context
            )
            if turn_state.stable_task_pack and not native_worker_state:
                stable_text = turn_state.stable_task_pack.to_protected_text(result_is_preview=runtime_result_is_preview())
                current_messages = _upsert_stable_task_state_message(
                    current_messages,
                    stable_text,
                )
                if runtime_profile.name == "single-deep":
                    protect_context(
                        content=stable_text,
                        entry_type="active_task",
                        ttl_seconds=runtime_profile.working_memory_ttl_seconds,
                        importance=runtime_profile.working_memory_importance,
                    )
                if context_manager:
                    context_manager.messages = current_messages

            # Hook: agent turn started
            fire_hook_sync(HookEvent.AGENT_START, step=step, cwd=cwd)

            # 高级控制论闭环（每个 step 开始时执行）
            if enable_work_chain and orch:
                orch.step_start(
                    context_manager=context_manager,
                    step=step,
                    tool_error_count=turn_state.tool_error_count,
                    saw_tool_result=turn_state.saw_tool_result,
                )
            elif enable_work_chain:
                # 状态观测：通过可测量输出估计系统内部状态
                if state_observer:
                    measurement = MeasurementVector(
                        timestamp=time.time(),
                        response_time=telemetry.average("model"),
                        success_rate=1.0 - (turn_state.tool_error_count / max(step, 1)),
                        context_length=context_manager.get_stats().total_tokens
                        if context_manager
                        else 0,
                        error_count=turn_state.tool_error_count,
                        tool_calls=telemetry.calls.get("tool", 0),
                    )
                    observed_state = state_observer.update(measurement)

                    # 将 Kalman 估计值输入到控制器
                    if observed_state.confidence > 0.4:
                        if observed_state.internal_load > 0.8:
                            logger.info(
                                "StateObserver: high internal_load=%.2f, reduce concurrency",
                                observed_state.internal_load,
                            )
                        if observed_state.hidden_errors > 0.5 and self_healing_engine:
                            self_healing_engine.detect_and_heal(
                                {
                                    "error_rate": observed_state.hidden_errors * 5.0,
                                    "context_usage": observed_state.context_pressure,
                                }
                            )
                        if observed_state.system_degradation > 0.4:
                            logger.warning(
                                "StateObserver: system degradation=%.2f confidence=%.2f",
                                observed_state.system_degradation,
                                observed_state.confidence,
                            )

                # 预测控制：预测未来趋势并提前调整
                if predictive_controller:
                    if context_manager:
                        stats = context_manager.get_stats()
                        predictive_controller.update(
                            "context_usage", stats.usage_percentage / 100.0
                        )
                    predictive_controller.update(
                        "error_rate", turn_state.tool_error_count / max(step, 1)
                    )

                    if step > 2:
                        actions = predictive_controller.generate_predictive_actions()
                        if actions and actions[0].urgency > 0.7:
                            action = actions[0]
                            logger.info(
                                "Predictive action: %s urgency=%.2f horizon=%s",
                                action.recommended_action,
                                action.urgency,
                                getattr(action, "horizon", "unknown"),
                            )
                            # Execute predictive actions via dispatch
                            dispatch: dict[str, Callable[[], None]] = {
                                "trigger_compaction": lambda: (
                                    context_cybernetics.try_reactive_recover(
                                        current_messages, "predictive"
                                    )
                                    if context_cybernetics
                                    else None
                                ),
                                "enable_safe_mode": lambda: logger.info(
                                    "Predictive: safe_mode recommended (reduce concurrency, extend timeouts)"
                                ),
                                "reduce_concurrency": lambda: logger.info(
                                    "Predictive: reduce_concurrency recommended"
                                ),
                            }
                            handler = dispatch.get(action.recommended_action)
                            if handler:
                                try:
                                    handler()
                                except Exception as exc:
                                    logger.warning(
                                        "Predictive action %s failed: %s",
                                        action.recommended_action,
                                        exc,
                                    )
                            # Also run self-healing for corroboration
                            if self_healing_engine:
                                healing_actions = self_healing_engine.detect_and_heal(
                                    {
                                        "context_usage": stats.usage_percentage / 100.0
                                        if context_manager
                                        else 0.0,
                                        "error_rate": turn_state.tool_error_count
                                        / max(step, 1),
                                    }
                                )
                                if healing_actions:
                                    logger.info(
                                        "Self-healing: %s", healing_actions[0].strategy
                                    )

            if metrics_collector:
                metrics_collector.start_turn(step)

            next_step: AgentStep
            context_budget = None
            try:
                # ── Layer 0: Preemptive context guard (CC-style blocking limit)
                # Paged memory builds a bounded source-linked request below, so
                # the legacy full-history guard must not stop it first.
                if context_manager and not (
                    active_paged_memory is not None
                    and active_paged_memory.controls_context
                ):
                    cm_stats = context_manager.get_stats()
                    if _is_at_blocking_limit(
                        cm_stats.total_tokens,
                        context_manager.context_window,
                    ):
                        blocking_msg = (
                            f"Context near limit ({cm_stats.total_tokens} / "
                            f"{context_manager.context_window} tokens). "
                            "Use /compact manually, or reduce task scope."
                        )
                        logger.warning("Preemptive guard: %s", blocking_msg)
                        emit_runtime_event(
                            category="stop",
                            message=blocking_msg,
                            stop_reason="blocked",
                        )
                        if on_assistant_message:
                            on_assistant_message(blocking_msg)
                        current_messages.append(
                            {"role": "assistant", "content": blocking_msg}
                        )
                        return current_messages

                # An error exchange can exceed the recent-history budget.
                # Keep only the live, bounded correction state as owned Runtime
                # context; never rewrite the immutable failed tool receipt.
                current_messages = [message for message in current_messages
                                    if runtime_context_kind(message) not in {"tool_input_recovery", "tool_recovery_lessons", "shared_writer_coordination", "task_check_evidence", EXECUTION_PROGRESS_CONTEXT}]
                runtime_context = (runtime or {}).get("_runtimeContext")
                delegation = getattr(runtime_context, "delegation_gateway", runtime_context)
                coordination = getattr(delegation, "shared_write_coordination", None)
                shared_status = None
                if callable(coordination):
                    shared_status = coordination((runtime or {}).get("_runtimeNodeId"), (runtime or {}).get("_runtimeAttemptId"))
                    if shared_status is not None:
                        current_messages.append(runtime_context_message(
                            "Current native shared writer coordination (advisory state; tool guards still apply):\n"
                            + json.dumps(shared_status, ensure_ascii=False), kind="shared_writer_coordination"))
                progress_context, progress_notice = execution_progress.review(
                    step=step, read_only=runtime.get("_allowedToolEffects") == ["read_only"],
                    shared_status=shared_status,
                )
                if progress_context:
                    current_messages.append(runtime_context_message(progress_context, kind=EXECUTION_PROGRESS_CONTEXT))
                if progress_notice:
                    emit_runtime_event(category="progress", message=progress_notice)
                correction_context = tool_retry_guard.recovery_context()
                if correction_context:
                    current_messages.append(runtime_context_message(correction_context, kind="tool_input_recovery"))
                recovery_context = recovery_tracker.context()
                if recovery_context:
                    current_messages.append(runtime_context_message(recovery_context, kind="tool_recovery_lessons"))
                check_context = task_check_evidence.context()
                if check_context:
                    current_messages.append(runtime_context_message(check_context, kind="task_check_evidence"))
                if callable(runtime.get("_userContext")):
                    runtime["_userContext"](current_messages)
                from minicode.runtime_role_context import current_primary_instructions
                model_input_messages = current_primary_instructions(current_messages)
                paged_request_view = None
                if active_paged_memory is not None:
                    # Plan against this step's adapter/output recovery, not a
                    # fixed 4096-token memory reserve. Include the handoff tools
                    # before deciding whether the packet needs a handoff.
                    context_budget = None
                    if active_paged_memory.controls_context and callable(getattr(model, "context_request", None)):
                        from minicode.provider_usage import ModelContextBudget
                        from minicode.paged_memory.tools import create_paged_memory_tools
                        planning_tools = tools if memory_tools_added else tools.with_additional_tools(
                            create_paged_memory_tools(active_paged_memory))
                        if tool_discovery is not None:
                            planning_tools = tool_discovery.model_tools(planning_tools,
                                provider_runtime=getattr(model, "runtime", runtime),
                                parallel_closure=(runtime or {}).get("_parallelClosureReady"),
                                required_names=skill_runtime.required_tool_names())
                        if callable(runtime.get("_userModelTools")):
                            planning_tools = runtime["_userModelTools"](planning_tools)
                        context_budget = ModelContextBudget(model, planning_tools,
                            output_recovery=turn_state.output_recovery,
                            stream=bool(on_assistant_stream_chunk), skill_runtime=skill_runtime,
                            config=active_paged_memory.config)
                    combined_system_context = "\n\n".join(
                        str(message.get("content", ""))
                        for message in current_messages
                        if message.get("role") == "system"
                    )
                    prepared_memory = run_paged_memory_operation(
                        "prepare_model_messages",
                        current_messages,
                        step_id=active_paged_memory.current_step_id,
                        phase_boundary=bool(policy_message),
                        system_context=combined_system_context,
                        request_budget=context_budget,
                        bind_history=active_paged_memory.controls_context,
                    )
                    if prepared_memory is not None:
                        model_input_messages, memory_packet = prepared_memory
                        rerank = memory_packet.debug_metadata.get("rerank", {})
                        if rerank.get("status") == "fallback" and not rerank_fallback_reported:
                            rerank_fallback_reported = True
                            emit_runtime_event(category="guard", message="记忆重排不可用，已使用本地检索结果。",
                                               evidence_summary=json.dumps(rerank, ensure_ascii=False))
                        if active_paged_memory.controls_context:
                            current_messages = list(active_paged_memory.context_rotation.request_messages)
                        rotation = memory_packet.debug_metadata.get("context_rotation", {})
                        if (active_paged_memory.controls_context and rotation.get("epoch", 0)
                                and rotation.get("status") in {"history_mismatch", "invalid_source"}):
                            raise HistoryIntegrityError(
                                "saved_rotation_unverifiable: " + rotation.get("diagnostic", rotation["status"]),
                                event_id=rotation.get("source_event_id", ""),
                            )
                        model_input_messages = current_primary_instructions(model_input_messages)
                        recovered = memory_packet.debug_metadata.get("context_auto_rotation")
                        borrowing = memory_packet.debug_metadata.get("context_borrowing", {})
                        if borrowing.get("status") == "active" and borrowing.get("requests") == 0:
                            record_worker_audit("context_borrowing_started", step=step, **borrowing)
                            if on_progress_message:
                                on_progress_message("正常上下文请求在缩减后仍无法容纳，正在临时借用额外空间保存交接笔记。业务上限保持不变。")
                        elif borrowing.get("status") == "handoff" and borrowing.get("requests") == 0:
                            preparing_context = borrowing.get("trigger") == "context_preparation"
                            record_worker_audit("context_preparation_started" if preparing_context else "context_handoff_started",
                                step=step, **borrowing)
                            if on_progress_message:
                                on_progress_message("继承或继续的历史较长，正在整理本轮上下文；完成后自动继续任务。"
                                    if preparing_context else "完整业务请求无法容纳，正在原上下文上限内保存交接笔记。")
                        if recovered:
                            record_worker_audit("context_auto_rotation", step=step, receipt=recovered)
                            if on_progress_message:
                                protected = memory_packet.debug_metadata.get("context_utilization", {}).get("protection")
                                on_progress_message("已自动整理历史上下文，最新要求和新增消息已保留。"
                                                    + ("正在检查恢复后的上下文容量。" if borrowing.get("status") == "restoring" else
                                                       "新增内容仍较多，正在继续恢复。" if protected else "继续执行任务。"),
                                                    progress_status="complete")
                        if active_paged_memory.controls_context:
                            from minicode.paged_memory.request_view import PagedMemoryRequestView
                            paged_request_view = PagedMemoryRequestView.build(
                                active_paged_memory.context_builder, memory_packet,
                                current_messages, model_input_messages,
                            )
                            if memory_packet.memory_mode.value != "INLINE":
                                if not memory_tools_added:
                                    from minicode.paged_memory.tools import (
                                        create_paged_memory_tools,
                                    )

                                    memory_tool_definitions = create_paged_memory_tools(
                                        active_paged_memory
                                    )
                                    memory_tool_names = frozenset(
                                        definition.name
                                        for definition in memory_tool_definitions
                                    )
                                    tools = tools.with_additional_tools(
                                        memory_tool_definitions
                                    )
                                    bind_native_memory_tools(memory_tool_definitions)
                                    memory_tools_added = True
                            elif memory_tools_added:
                                tools = tools.filtered_without_names(memory_tool_names)
                                bind_native_memory_tools([])
                                memory_tools_added = False
                            # Model switches can replace the active adapter between
                            # iterations, so refresh its non-owning tool view too.
                            replace_adapter_tools(model, tools)
                            replace_switcher_tools(tools)
                # Provider schema visibility is distinct from the executor's
                # complete scoped registry. Check the same schemas we send.
                model_tools = (tool_discovery.model_tools(tools, provider_runtime=getattr(model, "runtime", runtime),
                    parallel_closure=(runtime or {}).get("_parallelClosureReady"),
                    required_names=skill_runtime.required_tool_names())
                               if tool_discovery is not None else tools)
                if callable(runtime.get("_userModelTools")):
                    model_tools = runtime["_userModelTools"](model_tools)
                if active_paged_memory is not None and active_paged_memory.context_borrowing.handoff_only:
                    model_tools = active_paged_memory.context_borrowing.filter_tools(model_tools)
                supplied_contracts = request_contracts(model_tools)
                if active_paged_memory is not None and active_paged_memory.controls_context:
                    run_paged_memory_operation(
                        "enforce_provider_budget", model_input_messages,
                        tool_definitions=model_tools.list(),
                    )
                publish_turn_event(TurnEvent(kind="model_status", step=step, content="waiting"))
                record_worker_audit("model_input_ready", step=step, canonical_messages=current_messages,
                    input_messages=model_input_messages)
                model_received = False
                try:
                    observe_request_context(model_input_messages, tools, enabled=True,
                        stages_enabled=not (active_paged_memory is not None and active_paged_memory.controls_context),
                        paged_memory_view=paged_request_view, canonical_messages=current_messages)
                    with bind_context_borrowing(active_paged_memory.context_borrowing if active_paged_memory is not None else None), bind_skill_runtime(skill_runtime):
                        correction_started = time.monotonic()
                        was_recovering = input_recovery.recovering
                        correction_usage = None
                        try:
                            next_step = _model_next(
                                model,
                                model_input_messages,
                                on_stream_chunk=on_assistant_stream_chunk,
                                on_thinking_chunk=on_thinking_chunk,
                                store=store,
                                tools=model_tools,
                                output_recovery=turn_state.output_recovery,
                                on_provider_recovery=on_provider_recovery,
                            )
                            model_received = True
                            correction_usage = next_step.usage
                            task_check_evidence.model_observed(getattr(next_step, "imageInputIds", []))
                            for call_id, name, feedback in input_recovery.schemas_supplied(supplied_contracts, step):
                                # Annotate only the original, scoped diagnostic.
                                # Loading a schema never changes its execution receipt.
                                for message in reversed(current_messages):
                                    if (message.get("role") == "tool_result" and message.get("toolUseId") == call_id
                                            and message.get("toolName") == name
                                            and message.get("toolInput", {}).get("step") == feedback["step"]):
                                        message["toolInput"] = deepcopy(feedback)
                                        break
                                record_worker_audit("tool_schema_loaded", step=step, tool_call_id=call_id, tool_name=name,
                                                    schema_step=feedback["step"])
                                publish_turn_event(TurnEvent(kind="tool_input", step=feedback["step"], tool_name=name,
                                    tool_call_id=call_id, tool_input_status=deepcopy(feedback)))
                        finally:
                            input_recovery.model_observed(correction_usage,
                                (time.monotonic() - correction_started) * 1000, was_recovering)
                            if was_recovering:
                                record_worker_audit("tool_input_recovery_request", step=step, **input_recovery.metrics())
                finally:
                    publish_turn_event(TurnEvent(kind="model_status", step=step,
                        content="received" if model_received else "interrupted"))
                if active_paged_memory is not None:
                    run_paged_memory_operation(
                        "record_model_step", next_step, sequence_no=step
                    )
                    restored_borrowing = (active_paged_memory.last_packet.debug_metadata.get("context_borrowing", {})
                        if active_paged_memory.last_packet is not None else {})
                    if restored_borrowing.get("status") == "restored":
                        record_worker_audit("context_borrowing_restored", step=step, **restored_borrowing)
                        if on_progress_message:
                            on_progress_message("上下文已准备好，完整历史和最新要求已保留，继续执行任务。"
                                if restored_borrowing.get("trigger") == "context_preparation" else
                                "交接已完成，额外上下文借用已结束，继续按原上下文上限执行。"
                                if restored_borrowing.get("borrowed") else "交接已完成，继续按原上下文上限执行。",
                                progress_status="complete")
                    if active_paged_memory.context_borrowing.recovering and not next_step.calls:
                        if next_step.content:
                            current_messages.append(_with_reasoning_content(
                                {"role": "assistant", "content": next_step.content}, next_step.reasoningContent))
                        current_messages.append(runtime_feedback_message(
                            "Native handoff recovery is still pending. This response cannot complete business work. "
                            "Use memory_note_update with concise verified findings and next_steps; "
                            "the runtime must commit rotation and restore normal context admission first.", kind="continuation"))
                        continue
            except KeyboardInterrupt:
                raise  # Let Ctrl-C propagate
            except BudgetUnavailable as error:
                if (active_paged_memory is not None
                        and active_paged_memory.context_borrowing.recover(error, context_budget)):
                    record_worker_audit("context_handoff_recovery", step=step,
                        **active_paged_memory.context_borrowing.diagnostic())
                    if on_progress_message:
                        on_progress_message("完整请求因上下文容量不足被拦住，正在恢复交接。业务上限保持不变。")
                    continue
                if isinstance(error, ModelUsageUnreconciled):
                    message = f"Runtime model usage requires reconciliation: {error}"
                    stop_reason = "model_usage_unreconciled"
                else:
                    message = f"Runtime model budget stopped execution: {error}"
                    stop_reason = "consultation_budget_limited" if isinstance(error, ConsultationBudgetLimited) else "budget_exhausted"
                turn_state.set_stop_reason(stop_reason)
                emit_runtime_event(category="stop", message=message, stop_reason=stop_reason)
                current_messages.append({"role": "assistant", "content": message})
                return current_messages
            except (ContextRecoveryRequired, HistoryIntegrityError) as error:
                message = str(error)
                if isinstance(error, HistoryIntegrityError):
                    message = ("原始历史校验未通过，已在调用模型前停止。"
                               "需要恢复报出的原始事件或检查点，重复整理和重复执行工具不能修复它。\n" + message)
                turn_state.set_stop_reason("blocked")
                emit_runtime_event(category="stop", message=message, emit_progress=False, stop_reason="blocked")
                if on_assistant_message:
                    on_assistant_message(message)
                current_messages.append({"role": "assistant", "content": message})
                if metrics_collector:
                    metrics_collector.end_turn(total_tokens=0)
                return current_messages
            except ContextBudgetExceeded as error:
                if (active_paged_memory is not None
                        and active_paged_memory.context_borrowing.recover(error, context_budget)):
                    continue
                fallback = (
                    "Context budget exceeded before the model call "
                    f"({error.estimated_tokens} / {error.input_limit} input tokens). "
                    "No required source was discarded; reduce the task/input scope or "
                    "increase the configured context window."
                )
                logger.warning("Paged-memory hard budget guard: %s", error)
                turn_state.set_stop_reason("blocked")
                emit_runtime_event(
                    category="stop",
                    message=fallback,
                    emit_progress=False,
                    stop_reason="blocked",
                )
                if on_assistant_message:
                    on_assistant_message(fallback)
                current_messages.append({"role": "assistant", "content": fallback})
                if metrics_collector:
                    metrics_collector.end_turn(total_tokens=0)
                return current_messages
            except ProviderStreamError as error:
                message = str(error)
                turn_state.set_stop_reason("provider_error")
                notice = (("上游模型输出中断，自动重试未恢复，执行已暂停。已有进度已保留。"
                           if error.retries else "上游模型输出中断，执行已暂停。请稍后从已有进度继续。")
                          if error.retryable else "模型服务返回错误，执行已暂停。请检查模型和服务配置。")
                emit_runtime_event(category="stop", message=notice, stop_reason="provider_error",
                                   evidence_summary=message)
                current_messages.append({"role": "assistant", "content": message})
                return current_messages
            except ConnectionError as error:
                fallback = f"Network error (connection failed or dropped): {error}"
                logger.error("Model API connection error: %s", error)
                turn_state.set_stop_reason("blocked")
                emit_runtime_event(
                    category="stop",
                    message=fallback,
                    emit_progress=False,
                    stop_reason="blocked",
                )
                if on_assistant_message:
                    on_assistant_message(fallback)
                current_messages.append({"role": "assistant", "content": fallback})
                if metrics_collector:
                    metrics_collector.end_turn(total_tokens=0)
                return current_messages
            except TimeoutError as error:
                fallback = f"Model API timeout: {error}"
                logger.error("Model API timeout: %s", error)
                turn_state.set_stop_reason("blocked")
                emit_runtime_event(
                    category="stop",
                    message=fallback,
                    emit_progress=False,
                    stop_reason="blocked",
                )
                if on_assistant_message:
                    on_assistant_message(fallback)
                current_messages.append({"role": "assistant", "content": fallback})
                if metrics_collector:
                    metrics_collector.end_turn(total_tokens=0)
                return current_messages
            except Exception as error:
                # Catch-all for unexpected errors (rate limit, auth, server 5xx, etc.)
                from minicode.provider_usage import ProviderBalanceUnavailable, provider_balance_blocked
                if isinstance(error, ProviderBalanceUnavailable) or provider_balance_blocked():
                    # The last complete tool batch was checkpointed at the loop
                    # boundary. Do not retry, switch models or replay that batch.
                    message = "模型账户余额不足，执行已暂停，已有修改和子代理上下文已保留。补充余额后发送继续，可核对进度并续接原子代理。"
                    turn_state.set_stop_reason("provider_balance")
                    emit_runtime_event(category="stop", message=message, emit_progress=False,
                                       stop_reason="provider_balance")
                    current_messages.append({"role": "assistant", "content": message})
                    if on_assistant_message:
                        on_assistant_message(message)
                    return current_messages
                error_type = type(error).__name__
                active_model_id = _infer_active_model_id(model, runtime, error)
                fallback = _summarize_model_api_failure(
                    error_type=error_type,
                    error=error,
                    active_model_id=active_model_id,
                    runtime=runtime,
                )
                logger.error("%s", fallback)

                # Reactive Compact: 控制论恢复路径
                error_str = str(error).lower()
                needs_recovery = "prompt" in error_str and (
                    "too long" in error_str or "exceeds" in error_str
                )
                if (
                    legacy_context_management_allowed
                    and context_cybernetics
                    and needs_recovery
                ):
                    recovered_messages, recovery_result = (
                        context_cybernetics.try_reactive_recover(
                            current_messages, error_str
                        )
                    )
                    if recovery_result and recovery_result.effective:
                        current_messages = recovered_messages
                        if context_manager:
                            context_manager.messages = current_messages
                        logger.info(
                            "Cybernetics Reactive recovered: freed %d tokens",
                            recovery_result.tokens_freed,
                        )
                        continue
                elif (
                    legacy_context_management_allowed
                    and context_compactor
                    and needs_recovery
                ):
                    recovery_result = context_compactor.reactive_recover(
                        current_messages, error_str
                    )
                    if recovery_result and recovery_result.effective:
                        current_messages = recovery_result.messages
                        if context_manager:
                            context_manager.messages = current_messages
                        logger.info(
                            "Reactive Compact recovered: freed %d tokens",
                            recovery_result.tokens_freed,
                        )
                        continue

                # ModelSwitcher: 尝试切换到备用模型并重试
                if (
                    model_switcher
                    and "rate" not in error_str
                    and _should_attempt_model_fallback(error_str)
                ):
                    try:
                        if hasattr(model_switcher, "sync_current_model"):
                            model_switcher.sync_current_model(
                                active_model_id, adapter=model
                            )
                        if hasattr(model_switcher, "record_runtime_failure"):
                            model_switcher.record_runtime_failure(active_model_id)
                        if runtime is not None:
                            runtime["recentFailures"] = (
                                int(runtime.get("recentFailures", 0) or 0) + 1
                            )
                        switch_result = model_switcher.switch_to(
                            "",  # Let switcher pick fallback
                            reason=f"{error_type}: {error_str[:80]}",
                        )
                        if switch_result.success and switch_result.adapter is not None:
                            model = switch_result.adapter
                            fallback_message = (
                                f"Model fallback: switched from {switch_result.old_model} "
                                f"to {switch_result.new_model} after {error_type}."
                            )
                            logger.info(
                                "ModelSwitcher: switched to %s, retrying with new adapter",
                                switch_result.new_model,
                            )
                            emit_runtime_event(
                                category="recovery",
                                message=fallback_message,
                            )
                            continue
                        fallback = _summarize_model_api_failure(
                            error_type=error_type,
                            error=error,
                            active_model_id=active_model_id,
                            fallback_errors=switch_result.errors,
                            runtime=runtime,
                        )
                    except Exception:
                        pass

                if on_assistant_message:
                    on_assistant_message(fallback)
                turn_state.set_stop_reason("blocked")
                emit_runtime_event(
                    category="stop",
                    message=fallback,
                    emit_progress=False,
                    stop_reason="blocked",
                )
                current_messages.append({"role": "assistant", "content": fallback})
                if metrics_collector:
                    metrics_collector.end_turn(total_tokens=0)
                return current_messages

            if next_step.type == "tool_calls":
                # Transport integrity outranks everything else: bytes we never
                # received cannot prove a complete tool proposal, so no call in
                # this batch may reach a tool, hook, approval or journal entry.
                # A transport that ended on its own terminal marker but lost an
                # event is exactly as unusable: the remainder of that event is
                # unknown, so the assembled proposal is refused the same way.
                if next_step.diagnostics and next_step.diagnostics.transportComplete is False:
                    diagnostics = next_step.diagnostics
                    current_messages.extend(_rejected_truncated_tool_messages(
                        next_step, on_event=publish_turn_event, step=step,
                        reason=("response_parse_incomplete" if diagnostics.lostEventCount
                                else "transport_incomplete"),
                        detail=(diagnostics.rawResponseIncompleteReason
                                or "the response ended before the provider's terminal marker")))
                    turn_state.set_stop_reason("transport_incomplete")
                    message = ("The provider response is not provably complete"
                               + (f" ({diagnostics.lostEventCount} event(s) could not be decoded)"
                                  if diagnostics.lostEventCount else "")
                               + "; no tool call from it was executed. The remainder of that "
                                 "response is unknown.")
                    emit_runtime_event(category="stop", message=message, emit_progress=False,
                                       stop_reason="transport_incomplete")
                    current_messages.append({"role": "assistant", "content": message})
                    if on_assistant_message:
                        on_assistant_message(message)
                    return current_messages
                truncated = decide_truncated_model_output(turn_state=turn_state, tool_batch=True,
                    stop_reason=next_step.diagnostics.stopReason if next_step.diagnostics else None)
                if truncated is not None:
                    # This precedes paged tool capture, execution, approval and
                    # native submission. No valid-looking prefix may escape.
                    current_messages.extend(_rejected_truncated_tool_messages(
                        next_step, on_event=publish_turn_event, step=step))
                    if truncated.kind == "retry":
                        # Continue in this activation; never restart earlier tools.
                        message = truncated.user_content or (
                            "Provider truncated this tool batch; no calls were executed. "
                            "Continue once with fewer and smaller tool calls.")
                        status = truncated.assistant_content or message
                        emit_runtime_event(category="recovery", message=status,
                                           stop_reason="model_output_truncated")
                        current_messages.append(runtime_feedback_message(message, kind=truncated.feedback_kind))
                        if on_assistant_message:
                            on_assistant_message(status)
                        continue
                    turn_state.set_stop_reason("model_output_truncated")
                    message = truncated.assistant_content or "Model tool output was truncated; no calls executed."
                    emit_runtime_event(category="stop", message=message, emit_progress=False,
                                       stop_reason="model_output_truncated")
                    current_messages.append({"role": "assistant", "content": message})
                    if on_assistant_message:
                        on_assistant_message(message)
                    return current_messages

            # A provider response was generated from the preceding context.
            # Refuse its unstarted calls before inserting newly arrived human
            # context; keep every tool proposal paired with an honest refusal.
            if callable(runtime.get("_userBoundary")):
                boundary = runtime["_userBoundary"](current_messages, context_manager=context_manager,
                    response=next_step, on_event=publish_turn_event, step=step)
                if boundary["consumed"]:
                    continue

            if next_step.type == "assistant":
                is_empty = _is_empty_assistant_response(next_step.content)
                diagnostics = next_step.diagnostics
                assistant_decision = decide_assistant_turn(
                    turn_state=turn_state,
                    step_content=next_step.content,
                    step_kind=getattr(next_step, "kind", None),
                    stop_reason=diagnostics.stopReason if diagnostics else None,
                    block_types=diagnostics.blockTypes if diagnostics else None,
                    ignored_block_types=diagnostics.ignoredBlockTypes
                    if diagnostics
                    else None,
                    is_empty=is_empty,
                    treat_as_progress=(
                        not is_empty
                        and _should_treat_assistant_as_progress(
                            kind=getattr(next_step, "kind", None),
                            content=next_step.content,
                            saw_tool_result=turn_state.saw_tool_result,
                        )
                    ),
                    is_recoverable_thinking_stop=_is_recoverable_thinking_stop(
                        is_empty=is_empty,
                        stop_reason=diagnostics.stopReason if diagnostics else None,
                        ignored_block_types=diagnostics.ignoredBlockTypes
                        if diagnostics
                        else None,
                    ),
                    format_diagnostics=_format_diagnostics,
                    nudge_continue=NUDGE_CONTINUE,
                    nudge_after_tool_result=NUDGE_AFTER_TOOL_RESULT,
                    resume_after_pause=RESUME_AFTER_PAUSE,
                    resume_after_max_tokens=RESUME_AFTER_MAX_TOKENS,
                    nudge_after_empty_response=NUDGE_AFTER_EMPTY_RESPONSE,
                    nudge_after_empty_no_tools=NUDGE_AFTER_EMPTY_NO_TOOLS,
                    step_policy=turn_state.step_policy,
                    transport_complete=(diagnostics is None or (
                        diagnostics.transportComplete and diagnostics.networkComplete
                        and diagnostics.parseComplete and diagnostics.assemblyComplete
                        and not diagnostics.lostEventCount)),
                )

                record_worker_audit("assistant_decision", step=step, decision_kind=assistant_decision.kind,
                    stop_reason=assistant_decision.stop_reason,
                    runtime_event_category=assistant_decision.runtime_event_category,
                    assistant_content=assistant_decision.assistant_content,
                    user_content=assistant_decision.user_content)

                if assistant_decision.kind == "progress":
                    if assistant_decision.assistant_content:
                        turn_state.set_progress_summary(
                            assistant_decision.assistant_content
                        )
                        if assistant_decision.runtime_event_category is not None:
                            emit_runtime_event(
                                category=assistant_decision.runtime_event_category,
                                message=assistant_decision.assistant_content,
                                evidence_summary=(
                                    turn_state.verification_state.evidence_summary
                                    or turn_state.latest_tool_result_summary
                                ),
                            )
                        elif on_progress_message:
                            on_progress_message(assistant_decision.assistant_content, public=True)
                        current_messages.append(
                            _with_reasoning_content(
                                {
                                    "role": "assistant_progress",
                                    "content": assistant_decision.assistant_content,
                                },
                                next_step.reasoningContent,
                            )
                        )
                    if assistant_decision.user_content:
                        current_messages.append(
                            runtime_feedback_message(assistant_decision.user_content, kind=assistant_decision.feedback_kind)
                        )
                    continue

                if assistant_decision.kind == "retry":
                    if assistant_decision.runtime_event_category is not None:
                        emit_runtime_event(category=assistant_decision.runtime_event_category,
                            message=assistant_decision.assistant_content or "正在恢复本次不完整的输出。")
                    current_messages.extend(_assistant_retry_messages(
                        content=next_step.content,
                        reasoning_content=next_step.reasoningContent,
                        user_content=assistant_decision.user_content,
                        feedback_kind=assistant_decision.feedback_kind,
                    ))
                    continue

                if assistant_decision.kind == "fallback":
                    if assistant_decision.stop_reason == "widen_needed":
                        transitioned = turn_state.activate_widening(
                            extra_steps=runtime_profile.widening_step_bonus,
                        )
                        if transitioned:
                            widening_message = (
                                assistant_decision.assistant_content
                                or "Depth stalled; switching to widened mode."
                            )
                            if turn_state.widening_trigger_reason:
                                widening_message += (
                                    " Escalation trigger: "
                                    f"{turn_state.widening_trigger_reason}."
                                )
                            turn_state.set_progress_summary(
                                "runtime widened after the narrow path stalled"
                            )
                            emit_runtime_event(
                                category="widening",
                                message=widening_message,
                                widening_reason=turn_state.widening_trigger_reason,
                                evidence_summary=turn_state.widening_trigger_evidence,
                            )
                            current_messages.append(
                                _with_reasoning_content(
                                    {
                                        "role": "assistant_progress",
                                        "content": widening_message,
                                    },
                                    next_step.reasoningContent,
                                )
                            )
                            current_messages.append(
                                runtime_message(
                                    build_widening_transition_nudge(
                                        turn_state.latest_tool_result_summary,
                                        widening_reason=turn_state.widening_trigger_reason,
                                        widening_evidence_summary=turn_state.widening_trigger_evidence,
                                    ), kind="feedback",
                                )
                            )
                            continue
                    if assistant_decision.stop_reason:
                        turn_state.set_stop_reason(assistant_decision.stop_reason)
                        emit_runtime_event(
                            category="stop",
                            message=(
                                assistant_decision.assistant_content
                                or "Turn stopped without a final answer."
                            ),
                            emit_progress=False,
                            stop_reason=assistant_decision.stop_reason,
                            evidence_summary=(
                                turn_state.verification_state.evidence_summary
                                or turn_state.latest_tool_result_summary
                            ),
                        )
                    if assistant_decision.assistant_content and on_assistant_message:
                        on_assistant_message(assistant_decision.assistant_content)
                    if assistant_decision.assistant_content:
                        current_messages.append(
                            _with_reasoning_content(
                                {
                                    "role": "assistant",
                                    "content": assistant_decision.assistant_content,
                                },
                                next_step.reasoningContent,
                            )
                        )
                    return current_messages

                if (assistant_decision.kind == "final" and not native_submission_reminded
                    and not runtime.get("_directPrimarySubmission")
                    and runtime.get("runtimeOrchestration") == "primary"
                    and runtime.get("_runtimeNodeId") == "primary"
                    and tools.find("submit_candidate") is not None):
                    # Text is not a candidate capability. Allow one protocol
                    # correction within the same turn/step/token/deadline limits;
                    # no side effect is replayed or accepted by this reminder.
                    native_submission_reminded = True
                    current_messages.append(_with_reasoning_content(
                        {"role": "assistant", "content": next_step.content}, next_step.reasoningContent))
                    notice = ("Your text did not submit the candidate. Use the API native function-call channel for submit_candidate "
                              "with a summary argument; do not write XML, Markdown, a JSON example or a tool name in assistant text. "
                              "If the changes are ready, call submit_candidate "
                              "to request the registered checks and controlled integration. If blocked, state the blocker "
                              "or use an available input tool; do not claim delivery. This is the only submission-protocol "
                              "reminder and uses the existing budget. No candidate has been accepted or integrated.")
                    current_messages.append(runtime_message(notice))
                    record_worker_audit("native_submission_reminder", step=step, content=notice,
                                        grants_authority=False, maximum_per_activation=1)
                    continue

                if assistant_decision.kind == "final" and callable(runtime.get("_agentBoundary")):
                    proposed = [*current_messages, _with_reasoning_content(
                        {"role": "assistant", "content": assistant_decision.assistant_content or next_step.content}, next_step.reasoningContent)]
                    boundary = runtime["_agentBoundary"](proposed, context_manager=context_manager, final=True)
                    if boundary["consumed"]:
                        current_messages = proposed
                        continue
                    if boundary.get("waiting"):
                        current_messages = proposed
                        turn_state.set_stop_reason("waiting_children")
                        return current_messages
                if assistant_decision.kind == "final" and callable(runtime.get("_userBoundary")):
                    boundary = runtime["_userBoundary"](current_messages, context_manager=context_manager, final=True)
                    if boundary["consumed"]:
                        continue
                if assistant_decision.stop_reason:
                    turn_state.set_stop_reason(assistant_decision.stop_reason)
                    emit_runtime_event(
                        category="stop",
                        message=assistant_decision.assistant_content
                        or "Turn completed.",
                        emit_progress=False,
                        stop_reason=assistant_decision.stop_reason,
                        evidence_summary=(
                            turn_state.verification_state.evidence_summary
                            or turn_state.latest_tool_result_summary
                        ),
                    )
                if model_switcher and hasattr(model_switcher, "clear_runtime_failures"):
                    model_switcher.clear_runtime_failures()
                if assistant_decision.assistant_content:
                    turn_state.set_progress_summary("assistant finalized the turn")
                    if on_assistant_message:
                        on_assistant_message(assistant_decision.assistant_content, public=True)
                    current_messages.append(
                        _with_reasoning_content(
                            _final_answer_message(assistant_decision.assistant_content, runtime),
                            next_step.reasoningContent,
                        )
                    )
                if (
                    assistant_decision.protect_final_answer
                    and assistant_decision.assistant_content
                ):
                    protect_context(
                        content=assistant_decision.assistant_content[:500],
                        entry_type="key_decision",
                        ttl_seconds=runtime_profile.working_memory_ttl_seconds,
                        importance=runtime_profile.working_memory_importance,
                    )
                return current_messages

            if next_step.content:
                role = (
                    "assistant_progress"
                    if next_step.contentKind == "progress"
                    else "assistant"
                )
                if role == "assistant_progress":
                    turn_state.set_progress_summary(next_step.content)
                    if on_progress_message:
                        on_progress_message(next_step.content, public=True)
                    current_messages.append(
                        _with_reasoning_content(
                            {"role": role, "content": next_step.content},
                            next_step.reasoningContent,
                        )
                    )
                    current_messages.append(
                        runtime_feedback_message(NUDGE_CONTINUE, kind="continuation")
                    )
                else:
                    turn_state.set_progress_summary(next_step.content)
                    if on_assistant_message:
                        on_assistant_message(next_step.content, public=True)
                    current_messages.append(
                        _with_reasoning_content(
                            (_final_answer_message(next_step.content, runtime)
                             if next_step.contentKind == "final" else {"role": role, "content": next_step.content}),
                            next_step.reasoningContent,
                        )
                    )

            if (
                not next_step.calls
                and next_step.content
                and next_step.contentKind != "progress"
            ):
                if callable(runtime.get("_agentBoundary")):
                    boundary = runtime["_agentBoundary"](current_messages, context_manager=context_manager, final=True)
                    if boundary["consumed"]:
                        continue
                    if boundary.get("waiting"):
                        turn_state.set_stop_reason("waiting_children")
                        return current_messages
                if callable(runtime.get("_userBoundary")):
                    boundary = runtime["_userBoundary"](current_messages, context_manager=context_manager, final=True)
                    if boundary["consumed"]:
                        continue
                turn_state.set_stop_reason("done")
                emit_runtime_event(
                    category="stop",
                    message=next_step.content,
                    emit_progress=False,
                    stop_reason="done",
                    evidence_summary=(
                        turn_state.verification_state.evidence_summary
                        or turn_state.latest_tool_result_summary
                    ),
                )
                return current_messages

            # --- Concurrent tool execution ---
            # Classify calls into concurrent-safe (read-only) vs serial (writes/commands)
            calls = next_step.calls
            if active_paged_memory is not None:
                active_paged_memory.context_utilization.begin_tool_batch(calls, tools)
                for call in calls:
                    run_paged_memory_operation("record_tool_call", call)
            _results: list[tuple[dict, ToolResult]] = []
            submission_name = next((call.get("toolName") for call in calls
                                    if call.get("toolName") in {"submit_candidate", "submit_builder_candidate", "submit_consultation"}), None)
            submission_batch = submission_name is not None

            if submission_batch:
                from minicode.candidate_submission import execute_submission_batch

                def execute_ordered_call(call):
                    if metrics_collector:
                        metrics_collector.start_tool(call["toolName"])
                    result = execute_call(
                        call, tools, cwd, permissions, session, runtime, store, step,
                        on_tool_start, on_tool_result, tool_scheduler, run_memory_preflight, observe_raw_tool_result,
                        retry_guard=tool_retry_guard,
                    )
                    if metrics_collector:
                        metrics_collector.end_tool(success=result.ok, error=result.output if not result.ok else "")
                    return result

                _results = execute_submission_batch(calls, tools, execute_ordered_call, submission_name=submission_name,
                    on_event=publish_turn_event, step=step, recover_input=recover_submission_input)
            elif len(calls) <= 1:
                # Single call — no benefit from concurrency, run directly
                call = calls[0]
                if metrics_collector:
                    metrics_collector.start_tool(call["toolName"])
                result = execute_call(
                    call,
                    tools,
                    cwd,
                    permissions,
                    session,
                    runtime,
                    store,
                    step,
                    on_tool_start,
                    on_tool_result,
                    tool_scheduler,
                    run_memory_preflight,
                    observe_raw_tool_result,
                    retry_guard=tool_retry_guard,
                )
                if metrics_collector:
                    metrics_collector.end_tool(
                        success=result.ok,
                        error=result.output if not result.ok else "",
                    )
                _results.append((call, result))
            else:
                # Multiple calls — use ToolScheduler for intelligent partitioning
                concurrent_calls, serial_calls = tool_scheduler.schedule_calls(
                    calls, tools
                )

                _results.clear()  # Reuse outer declaration

                # Phase 1: Run all concurrent-safe tools in parallel
                if concurrent_calls:
                    max_workers = tool_scheduler.get_recommended_max_workers(
                        concurrent_calls,
                        error_rate=turn_state.tool_error_count / max(step, 1),
                        avg_latency=telemetry.average("tool"),
                        recent_failures=turn_state.tool_error_count,
                    )
                    # Apply cybernetic concurrency cap if FeedbackController reduced parallelism
                    force_cap = getattr(tool_scheduler, "_force_max_workers", None)
                    if force_cap:
                        max_workers = min(max_workers, force_cap)
                    if tool_scheduler.last_decision:
                        logger.info(
                            "ToolSchedulerController: workers=%d multiplier=%.2f cooldown=%.2fs [%s]",
                            max_workers,
                            tool_scheduler.last_decision.concurrency_multiplier,
                            tool_scheduler.last_decision.cooldown_seconds,
                            ", ".join(tool_scheduler.last_decision.reasons or []),
                        )
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=max_workers,
                        thread_name_prefix="mc-tool",
                    ) as pool:
                        future_to_call = {
                            pool.submit(
                                copy_context().run,
                                execute_call,
                                call,
                                tools,
                                cwd,
                                permissions,
                                session,
                                runtime,
                                None,
                                step,
                                (lambda name, value, call_id=call["id"]: publish_turn_event(
                                    TurnEvent.tool_started(step=step, tool_name=name,
                                                           tool_input=value, tool_call_id=call_id)
                                )) if live_concurrent_events else None,
                                None,
                                tool_scheduler,
                                run_memory_preflight,
                                observe_raw_tool_result,
                                retry_guard=tool_retry_guard,
                                # No UI callbacks during concurrent phase
                            ): call
                            for call in concurrent_calls
                        }
                        for future in concurrent.futures.as_completed(future_to_call):
                            call = future_to_call[future]
                            try:
                                result = future.result()
                            except Exception as exc:
                                result = ToolResult(
                                    ok=False,
                                    output=f"Concurrent execution error: {exc}",
                                )
                            _results.append((call, result))
                            if live_concurrent_events:
                                publish_turn_event(TurnEvent.tool_finished(
                                    step=step, tool_name=call["toolName"], output=result.output,
                                    is_error=not result.ok, tool_call_id=call["id"],
                                ))

                # Phase 2: Run serial tools sequentially (in original order)
                if serial_calls:
                    serial_blocked = any(result.timedOut or result.ambiguousSideEffect or result.awaitUser
                                         or result.runtimeYield or result.runtimeStopReason for _, result in _results)
                    for call in serial_calls:
                        if serial_blocked:
                            result = ToolResult(ok=False, inputRejected=True, output=(
                                "NotExecuted: an earlier call in this batch timed out, has unresolved effects, "
                                "or ended execution; inspect its receipt before proceeding."))
                            _results.append((call, result))
                            publish_turn_event(TurnEvent.tool_rejected(step=step, tool_name=call["toolName"],
                                tool_input=call.get("input"), output=result.output,
                                tool_call_id=call["id"], reason="prior_call_incomplete"))
                            continue
                        if metrics_collector:
                            metrics_collector.start_tool(call["toolName"])
                        result = execute_call(
                            call,
                            tools,
                            cwd,
                            permissions,
                            session,
                            runtime,
                            store,
                            step,
                            on_tool_start,
                            on_tool_result,
                            tool_scheduler,
                            run_memory_preflight,
                            observe_raw_tool_result,
                            retry_guard=tool_retry_guard,
                        )
                        if metrics_collector:
                            metrics_collector.end_tool(
                                success=result.ok,
                                error=result.output if not result.ok else "",
                            )
                        _results.append((call, result))
                        # A serial wait/yield/terminal result ends this batch.
                        if (result.awaitUser or result.runtimeYield or result.runtimeStopReason
                                or result.timedOut or result.ambiguousSideEffect):
                            serial_blocked = True

            # Process all results and build messages (preserve original call order)
            call_order = {call["id"]: idx for idx, call in enumerate(calls)}
            _results.sort(key=lambda pair: call_order.get(pair[0]["id"], 999))
            if tool_discovery is not None:
                tool_discovery.require_schemas(call["toolName"] for call, result in _results
                                               if result.inputSchemaRequired or result.inputValidationFailed)

            # Preserve the provider's parallel tool-call shape: all assistant
            # tool calls belong to one assistant turn and must precede every
            # tool result.  This is required by OpenAI-compatible providers
            # such as DeepSeek and also lets the Anthropic adapter group the
            # tool_use blocks into a single assistant message.
            for call, _ in _results:
                current_messages.append(_tool_call_message(call, next_step.reasoningContent))

            paged_step_success = True
            submission_terminal = None
            for call, result in _results:
                paged_result_output: str | None = None
                with paged_tool_receipts_lock:
                    captured_result = paged_tool_receipts.pop(
                        str(call.get("id", "")), None
                    )
                if captured_result is not None:
                    _, persisted_result_output = captured_result
                    if (
                        active_paged_memory is not None
                        and not paged_memory_shadow_disabled
                        and active_paged_memory.controls_context
                    ):
                        paged_result_output = persisted_result_output
                elif active_paged_memory is not None:
                    try:
                        captured_result = run_paged_memory_operation(
                            "capture_tool_result",
                            call=call,
                            output=result.output,
                            ok=result.ok,
                            read_sha256=result.contextReadSha256 or "",
                        )
                        if captured_result is not None:
                            _, persisted_result_output = captured_result
                        else:
                            persisted_result_output = result.output
                        if (
                            not paged_memory_shadow_disabled
                            and active_paged_memory.controls_context
                        ):
                            paged_result_output = persisted_result_output
                    except ArtifactPersistenceRequired as error:
                        # The tool may have executed, but its large receipt is not
                        # durably available.  Never trim it and pretend success.
                        result = ToolResult(
                            ok=False,
                            output=str(error),
                            awaitUser=True,
                            waitKind="input",
                            waitReason=(
                                "artifact persistence requires user intervention"
                            ),
                        )
                        captured_error = run_paged_memory_operation(
                            "capture_tool_result",
                            call=call,
                            output=result.output,
                            ok=False,
                            force_inline=True,
                        )
                        if captured_error is not None:
                            _, paged_result_output = captured_error
                # Paged memory may already have captured the original receipt
                # before the executor added its repeated-input guidance. Keep
                # that immutable source while delivering the current guidance.
                if (paged_result_output is not None and result.inputCorrectionHint
                        and not paged_result_output.endswith(result.inputCorrectionHint)):
                    paged_result_output += "\n\n" + result.inputCorrectionHint
                if (paged_result_output is not None and result.recoveryGuidance
                        and result.recoveryGuidance not in paged_result_output):
                    paged_result_output += "\n\n" + result.recoveryGuidance
                if not result.ok:
                    paged_step_success = False
                observe_request_tool_result(call, result, tools)
                # Fire hooks and UI callbacks for concurrent calls (deferred)
                tool_def = tools.find(call["toolName"])
                is_concurrent = (
                    tool_def
                    and tool_def.is_read_only
                    and tool_def.is_concurrency_safe
                    and len(calls) > 1
                    and not submission_batch
                )

                if is_concurrent:
                    # Deferred UI callbacks for concurrent tools
                    if live_concurrent_events:
                        if legacy_on_tool_start:
                            legacy_on_tool_start(call["toolName"], call["input"])
                    elif on_tool_start:
                        tool_event_context.next_call_id = call["id"]
                        on_tool_start(call["toolName"], call["input"])
                    if store:
                        store.set_state(set_busy(call["toolName"]))
                        store.set_state(increment_tool_calls())
                        store.set_state(set_idle())
                    # Hook: pre-tool-use (fire after the fact for concurrent tools)
                    fire_hook_sync(
                        HookEvent.PRE_TOOL_USE,
                        tool_name=call["toolName"],
                        tool_input=call["input"],
                        step=step,
                    )

                # Hook: post-tool-use
                fire_hook_sync(
                    HookEvent.POST_TOOL_USE,
                    tool_name=call["toolName"],
                    tool_output=result.output,
                    is_error=not result.ok,
                    step=step,
                )

                if is_concurrent:
                    if live_concurrent_events:
                        if legacy_on_tool_result:
                            legacy_on_tool_result(call["toolName"], result.output, not result.ok)
                    elif on_tool_result:
                        on_tool_result(call["toolName"], result.output, not result.ok)

                tool_summary = f"{call['toolName']}: {result.output[:200]}"
                unsafe_effect = bool(
                    tool_def is not None
                    and tool_def.effect is not ToolEffect.READ_ONLY
                    and tool_def.replay_policy
                    in {
                        ReplayPolicy.NEVER_AUTO_RETRY,
                        ReplayPolicy.VERIFY_BEFORE_RETRY,
                    }
                    and not result.awaitUser
                    and not result.inputRejected
                    and not result.inputValidationFailed
                )
                prior_write_count = turn_state.successful_write_observation_count
                turn_state.record_tool_result(
                    result.ok,
                    summary=tool_summary,
                    interrupted_before_execution=bool(result.inputRejected and
                        (result.inputSchemaRequired or result.failureKind == "human_steering")),
                    wait_kind=result.waitKind,
                    timed_out=terminal_timeout(result),
                    ambiguous_side_effect=result.ambiguousSideEffect,
                    unsafe_effect=unsafe_effect,
                    unsettled_tool=result.unsettledTool,
                    write_revision=result.contextWriteRevision,
                    workspace_write=bool(tool_def is not None
                        and tool_def.effect is ToolEffect.WORKSPACE_WRITE
                        and not (result.awaitUser or result.inputRejected or result.runtimeYield
                                 or result.runtimeStopReason or result.backgroundTask
                                 or result.verificationFailed or result.failureKind)),
                )
                execution_progress.observe(call, result, step=step,
                    changed=turn_state.successful_write_observation_count > prior_write_count)
                if (call["toolName"] in _NETWORK_RESEARCH_TOOL_NAMES
                        and not result.inputRejected and not result.inputValidationFailed):
                    if call["toolName"] == "web_search" and result.ok:
                        relevance_match = re.search(
                            r"(?m)^RELEVANCE_FILTER:\s*(strong|moderate|passed)\s*$",
                            result.output,
                        )
                        if relevance_match:
                            relevant_search_succeeded = True
                            if relevance_match.group(1) == "strong":
                                strong_relevant_search_succeeded = True
                                strong_search_result_urls.update(
                                    url.rstrip("/").casefold()
                                    for url in re.findall(
                                        r"(?m)^\s*URL:\s*(https?://\S+)",
                                        result.output,
                                    )
                                )
                            if (
                                "EVIDENCE_READY: cross-source dated news results"
                                in result.output
                            ):
                                search_headline_evidence_ready = True
                    elif call["toolName"] == "web_fetch" and result.ok:
                        successful_network_fetches += 1
                        if _fetch_has_lookup_evidence(
                            _latest_user_request(current_messages),
                            result.output,
                            strong_search_result_urls,
                        ):
                            evidence_network_fetches += 1
                tool_decision = decide_tool_turn(
                    tool_name=call["toolName"],
                    result_output=result.output,
                    await_user=result.awaitUser,
                    runtime_stop_reason=result.runtimeStopReason,
                    result_ok=result.ok,
                    ambiguous_side_effect=result.ambiguousSideEffect,
                )
                record_worker_audit("tool_decision", step=step, tool_call_id=call["id"],
                    decision_kind=tool_decision.kind, stop_reason=tool_decision.stop_reason,
                    assistant_content=tool_decision.assistant_content)
                if tool_decision.progress_summary:
                    turn_state.set_progress_summary(tool_decision.progress_summary)
                if not result.ok and (not result.inputRejected or result.failureKind in {"dependency_pending", "context_blocked"}):
                    # Use ErrorClassifier for intelligent error handling
                    classified = ErrorClassifier.classify_tool_result(
                        result, tool_name=call["toolName"],
                        replay_policy=tool_def.replay_policy if tool_def is not None else None,
                    )
                    # Total failures in the turn are not retries of this operation.
                    nudge = NudgeGenerator.generate(classified)
                    # Append nudge to tool result content for model context
                    result_output = (
                        (paged_result_output or result.output)
                        + "\n\n[System note: "
                        + nudge
                        + "]"
                    )
                else:
                    result_output = paged_result_output or result.output
                    # Increased nudge frequency: provide steering even on success
                    if result.ok and getattr(tool_scheduler, "_force_nudge_frequency", False):
                        success_nudge = (
                            f"Tool '{call['toolName']}' succeeded. "
                            "The system is under stability pressure — prefer smaller, "
                            "incremental steps and verify each result before proceeding."
                        )
                        result_output = (
                            (paged_result_output or result.output)
                            + "\n\n[System note: "
                            + success_nudge
                            + "]"
                        )

                # Record conflicts between concurrent tools if both failed
                if not result.ok and not result.inputSchemaRequired and result.failureKind != "human_steering" and len(calls) > 1:
                    for other_call, other_result in _results:
                        if other_call["id"] == call["id"]:
                            continue
                        if not other_result.ok and not other_result.inputSchemaRequired and other_result.failureKind != "human_steering":
                            tool_scheduler.record_conflict(
                                call["toolName"], other_call["toolName"]
                            )

                # ReadDedup: 去重相同文件的重复读取，节省上下文空间
                if (
                    context_compactor
                    and result.ok
                    and call.get("toolName") == "read_file"
                ):
                    file_path = call.get("input", {}).get("path", "")
                    if file_path:
                        dedup_mgr = context_compactor.read_dedup
                        if dedup_mgr.should_dedup(file_path, result_output):
                            result_output = dedup_mgr.get_stub(file_path)
                            logger.debug(
                                "ReadDedup replaced content for %s (stub)", file_path
                            )
                        dedup_mgr.register_read(
                            file_path, result_output, len(current_messages)
                        )

                current_messages.append(
                    {
                        "role": "tool_result",
                        "toolUseId": call["id"],
                        "toolName": call["toolName"],
                        "content": result_output,
                        "isError": not result.ok,
                    }
                )
                input_metadata = input_recovery.observe(call, result, step, tool=tools.find(call["toolName"]))
                evidence_tool = tools.find(call["toolName"])
                task_check_evidence.observe_tool(call, result,
                    effect=evidence_tool.effect if evidence_tool else ToolEffect.PRIVILEGED)
                if result.ok and call["toolName"] == "view_image" and result.imageInputIds:
                    current_messages[-1]["imageInputIds"] = list(result.imageInputIds)
                recovery_metadata = recovery_tracker.observe(call, result, step, registry=tools)
                if recovery_metadata:
                    current_messages[-1]["toolRecovery"] = deepcopy(recovery_metadata)
                    record_worker_audit("tool_recovery", tool_call_id=call["id"], recovery=recovery_metadata)
                    publish_turn_event(TurnEvent(kind="tool_recovery", step=step, tool_name=call["toolName"],
                        tool_call_id=call["id"], tool_recovery=deepcopy(recovery_metadata)))
                record_worker_audit("tool_input_recovery", tool_call_id=call["id"], **input_metadata)
                if input_metadata["status"] in {"invalid", "schema_required"} or input_metadata["correctsCallIds"]:
                    # Keep full cumulative metrics in the audit, not repeated in
                    # every model message or ordinary successful browser event.
                    feedback = {key: value for key, value in input_metadata.items() if key != "metrics"}
                    current_messages[-1]["toolInput"] = feedback
                    publish_turn_event(TurnEvent(kind="tool_input", step=step, tool_name=call["toolName"],
                        tool_call_id=call["id"], tool_input_status=deepcopy(feedback)))
                observe_request_tool_message(current_messages[-1])
                if len(calls) > 1 and (terminal_timeout(result) or tool_decision.kind in {"await_user", "stop"}):
                    # Retain every NotExecuted receipt before ending the turn;
                    # no pending call disappears behind an earlier wait/error.
                    if submission_terminal is None:
                        submission_terminal = (result, tool_decision)
                    continue
                if terminal_timeout(result):
                    turn_state.set_stop_reason("tool_timeout")
                    timeout_message = result.output
                    emit_runtime_event(
                        category="stop",
                        message=timeout_message,
                        emit_progress=False,
                        stop_reason="tool_timeout",
                        evidence_summary=turn_state.latest_tool_result_summary,
                    )
                    current_messages.append(
                        {"role": "assistant", "content": timeout_message}
                    )
                    if metrics_collector:
                        metrics_collector.end_turn(total_tokens=0)
                    if active_paged_memory is not None:
                        run_paged_memory_operation("finalize_step", success=False)
                    return current_messages
                if tool_decision.kind in {"await_user", "stop"}:
                    if tool_decision.stop_reason:
                        turn_state.set_stop_reason(tool_decision.stop_reason)
                        emit_runtime_event(
                            category="stop",
                            message=tool_decision.assistant_content or result_output,
                            emit_progress=False,
                            stop_reason=tool_decision.stop_reason,
                            evidence_summary=turn_state.latest_tool_result_summary,
                        )
                    if tool_decision.assistant_content and on_assistant_message:
                        on_assistant_message(tool_decision.assistant_content)
                    current_messages.append(
                        {
                            "role": "assistant",
                            "content": tool_decision.assistant_content or result_output,
                        }
                    )
                    if metrics_collector:
                        metrics_collector.end_turn(total_tokens=0)
                    if active_paged_memory is not None:
                        run_paged_memory_operation("finalize_step", success=False)
                    return current_messages

            if active_paged_memory is not None:
                run_paged_memory_operation("finalize_step", success=paged_step_success)

            if submission_terminal is not None:
                result, decision = submission_terminal
                reason = "tool_timeout" if terminal_timeout(result) else decision.stop_reason
                content = result.output if terminal_timeout(result) else decision.assistant_content or result.output
                turn_state.set_stop_reason(reason)
                emit_runtime_event(category="stop", message=content, emit_progress=False,
                                   stop_reason=reason, evidence_summary=turn_state.latest_tool_result_summary)
                current_messages.append({"role": "assistant", "content": content})
                if metrics_collector:
                    metrics_collector.end_turn(total_tokens=0)
                return current_messages

            if any(result.runtimeYield for _, result in _results):
                # All executed tool receipts are now in the checkpoint. This
                # internal suspension is not an approval or a user question.
                turn_state.set_stop_reason("waiting_children")
                return current_messages

            network_evidence_ready = (
                stop_network_when_evidence_ready
                and relevant_search_succeeded
                and (
                    (search_headline_evidence_ready and tools.find("web_fetch") is None)
                    or (
                        strong_relevant_search_succeeded
                        and evidence_network_fetches >= 1
                    )
                    or successful_network_fetches >= 2
                )
            )
            network_limit_reached = network_budget.count >= network_tool_limit
            if not network_guard_active and network_limit_reached:
                network_guard_active = True
                guard_message = (
                    f"联网检索已达到本次任务上限（{network_tool_limit} 次）；"
                    "停止继续搜索，正在用现有可靠结果作答。"
                )
                guard_reason = "the per-turn network research limit was reached"
                network_budget.stop(guard_reason)
                turn_state.set_progress_summary(guard_message)
                emit_runtime_event(
                    category="guard",
                    message=guard_message,
                    emit_progress=False,
                )
                current_messages.append(
                    {
                        "role": "system",
                        "content": (
                            "NETWORK_RESEARCH_STOP. Do not call web_search or web_fetch again: "
                            f"{guard_reason}. Answer the user's original request now in the user's language. "
                            "Discard irrelevant or stale results, prefer the strongest dated sources already "
                            "collected, and name source/date when useful. If the evidence is insufficient, say "
                            "that plainly. Do not expose raw XML, tool mechanics, or another progress-only update."
                        ),
                    }
                )
            elif not network_guard_active and network_evidence_ready and not network_evidence_notified:
                # Search relevance and HTTP success do not prove that a page
                # answers the request. Advise synthesis; retain the remaining
                # budget for stale pages, login walls and source verification.
                network_evidence_notified = True
                emit_runtime_event(
                    category="guard",
                    message="已取得相关网页证据；正在核对是否足以回答，必要时继续读取原文。",
                    emit_progress=False,
                )
                current_messages.append({
                    "role": "system",
                    "content": (
                        "NETWORK_RESEARCH_EVIDENCE. Related sources have been collected. "
                        "If they answer the request, synthesize a dated, sourced answer now. "
                        "Search snippets, stale pages and login walls are not verified current evidence. "
                        "If important facts remain unverified, web_fetch remains available within the "
                        "remaining turn budget. Do not claim a missing tool or disabled network access."
                    ),
                })
            if (not network_guard_active and not network_search_guard_active
                    and network_budget.search_limit < network_tool_limit
                    and network_budget.search_limit_reached):
                network_search_guard_active = True
                current_messages.append({
                    "role": "system",
                    "content": (
                        "NETWORK_RESEARCH_VERIFY. The search allocation has been used; "
                        "do not call web_search again. web_fetch is still available: "
                        "read the most relevant original source URLs to verify the search snippets, "
                        "including their actual publication dates. If a page fails, try another collected source. "
                        "The tools remain registered and network access has not been disabled."
                    ),
                })

            # 工具执行完成后的控制论反馈
            if enable_work_chain:
                # 多变量解耦：消除工具间的耦合影响
                if decoupling_controller:
                    decoupling_controller.record_measurement(
                        {
                            "token_usage_to_latency": (
                                context_manager.get_stats().usage_percentage / 100.0
                                if context_manager
                                else 0.0,
                                telemetry.average("model") / 60.0,
                            ),
                            "context_pressure_to_errors": (
                                context_manager.get_stats().usage_percentage / 100.0
                                if context_manager
                                else 0.0,
                                turn_state.tool_error_count / max(step, 1),
                            ),
                        }
                    )
                    decoupling_controller.compute_decoupling_matrix()

                if orch:
                    step_summary = orch.step_end(
                        tool_scheduler=tool_scheduler,
                        context_manager=context_manager,
                        step=step,
                        tool_error_count=turn_state.tool_error_count,
                        saw_tool_result=turn_state.saw_tool_result,
                        max_steps=turn_state.max_steps,
                        progress_signal=execution_progress.signal(step=step, max_steps=turn_state.max_steps),
                    )
                    turn_state.max_steps = _apply_control_signal(
                        control_signal=step_summary.get("control_signal"),
                        system_state=step_summary.get("system_state"),
                        max_steps=turn_state.max_steps,
                        tool_scheduler=tool_scheduler,
                        context_compactor=context_compactor,
                        model_switcher=model_switcher,
                        feedback_controller=feedback_controller,
                    )
                else:
                    # 自愈检测：检测并修复故障
                    if self_healing_engine:
                        metrics_for_healing = {
                            "error_rate": turn_state.tool_error_count / max(step, 1),
                            "context_usage": context_manager.get_stats().usage_percentage
                            / 100.0
                            if context_manager
                            else 0.0,
                            "oscillation_index": feedback_controller._compute_oscillation()
                            if feedback_controller
                            else 0.0,
                        }
                        healing_actions = self_healing_engine.detect_and_heal(
                            metrics_for_healing
                        )
                        if healing_actions:
                            logger.info(
                                "Self-healing triggered: %s",
                                healing_actions[0].strategy,
                            )

                    # 进度控制：检测任务是否卡住或完成
                    if progress_controller:
                        progress_signal = execution_progress.signal(step=step, max_steps=turn_state.max_steps)
                        progress_decision = progress_controller.decide(progress_signal)
                        if progress_decision.action in (
                            ProgressAction.STOP,
                            ProgressAction.REQUEST_CONFIRMATION,
                        ):
                            logger.warning(
                                "ProgressController: action=%s health=%.2f stall=%.2f reasons=%s",
                                progress_decision.action.value,
                                progress_decision.health_score,
                                progress_decision.stall_score,
                                ", ".join(progress_decision.reasons),
                            )

            # Tool execution completed for this step; ask the model for the next turn
            # instead of falling through to the max-step fallback.
            if metrics_collector:
                total_tokens = (
                    sum(estimate_message_tokens(m) for m in current_messages)
                    if context_manager
                    else 0
                )
                metrics_collector.end_turn(total_tokens=total_tokens)
            continue

        fallback = "Reached the maximum tool step limit for this turn."
        turn_state.set_stop_reason("max_steps")
        emit_runtime_event(
            category="stop",
            message=fallback,
            emit_progress=False,
            stop_reason="max_steps",
            evidence_summary=(
                turn_state.verification_state.evidence_summary
                or turn_state.latest_tool_result_summary
            ),
        )
        if on_assistant_message:
            on_assistant_message(fallback)
        current_messages.append({"role": "assistant", "content": fallback})
        return current_messages
    finally:
        paged_memory_finalization_error: Exception | None = None
        if turn_state.stop_reason == "canceled":
            skill_runtime.lifecycle.release("canceled")
        if active_paged_memory is not None:
            try:
                if (
                    not paged_memory_shadow_disabled
                    and not active_paged_memory.step_finalized
                ):
                    run_paged_memory_operation(
                        "finalize_step",
                        success=turn_state.final_task_state is TaskState.COMPLETED,
                    )
            except Exception as error:
                logger.error(
                    "Paged memory checkpoint/finalization failed; state was not advanced: %s",
                    error,
                )
                if active_paged_memory.controls_context:
                    paged_memory_finalization_error = error
            finally:
                active_paged_memory.context_borrowing.stop()
                if owns_paged_memory_runtime:
                    active_paged_memory.close()
        for adapter, original_tools in reversed(adapter_tool_restorations):
            try:
                adapter.tools = original_tools
            except Exception:
                logger.debug("Could not restore adapter tool view", exc_info=True)
        if (
            switcher_tools_original is not None
            and model_switcher is not None
            and hasattr(model_switcher, "_tools")
        ):
            model_switcher._tools = switcher_tools_original
        # Emit the terminal snapshot before coda bookkeeping so every return
        # path (including an exception) produces exactly one structured Done
        # event with the messages accumulated by the loop.
        publish_turn_event(
            TurnEvent.completed(
                step=turn_state.step or None,
                messages=current_messages,
                stop_reason=str(turn_state.stop_reason or ""),
                tool_error_count=turn_state.tool_error_count,
                wait_kind=turn_state.wait_kind,
                timed_out=turn_state.timed_out,
                ambiguous_side_effect=turn_state.ambiguous_side_effect,
                unsafe_effect_observed=turn_state.unsafe_effect_observed,
                unsettled_tool=turn_state.unsettled_tool,
            )
        )
        # Coda: finalize metrics, work-chain bookkeeping, and control summaries.
        fire_hook_sync(
            HookEvent.AGENT_STOP,
            step=turn_state.step,
            tool_errors=turn_state.tool_error_count,
        )
        step = turn_state.step
        if metrics_collector and metrics_collector._current_turn is not None:
            total_tokens = (
                sum(estimate_message_tokens(m) for m in current_messages)
                if context_manager
                else 0
            )
            metrics_collector.end_turn(total_tokens=total_tokens)

        context_usage = 0.0
        if context_manager:
            try:
                context_usage = context_manager.get_stats().usage_percentage / 100.0
            except Exception:
                context_usage = 0.0
        coda_summary = build_turn_coda_summary(
            turn_state=turn_state,
            context_usage=context_usage,
        )

        if enable_work_chain and prelude.task:
            finalize_work_chain_task(
                task=prelude.task,
                auditor=prelude.auditor,
                coda_summary=coda_summary,
                success_outcome=DecisionOutcome.SUCCESS,
                failure_outcome=DecisionOutcome.FAILURE,
            )

            if prelude.task_graph and prelude.task_slot_key:
                try:
                    if coda_summary.task_state is TaskState.COMPLETED:
                        prelude.task_graph.complete_task(
                            prelude.task_slot_key,
                            result=prelude.task.result_summary,
                        )
                    elif coda_summary.task_state is TaskState.PAUSED:
                        slot = prelude.task_graph.slots.get(prelude.task_slot_key)
                        if slot is not None:
                            slot.state = GraphTaskState.QUEUED
                            slot.result = prelude.task.result_summary
                            prelude.task_graph.updated_at = time.time()
                    else:
                        prelude.task_graph.fail_task(
                            prelude.task_slot_key,
                            prelude.task.result_summary,
                        )
                except Exception:
                    logger.debug("TaskGraph finalization skipped", exc_info=True)

            logger.info(
                "Work chain completed: task=%s state=%s stop_reason=%s steps=%d errors=%d",
                prelude.task.id,
                prelude.task.state.value,
                coda_summary.stop_reason,
                turn_state.step,
                turn_state.tool_error_count,
            )

            # 任务后自省：提取经验教训
            if orch and prelude.task and not coordinator_owns_memory:
                try:
                    execution_trace: list[dict[str, Any]] = [
                        {"type": "tool_call", "count": turn_state.step},
                        {
                            "type": "error",
                            "count": turn_state.tool_error_count,
                            "content": f"{turn_state.tool_error_count} errors",
                        }
                        if turn_state.tool_error_count > 0
                        else {},
                        {"type": "assistant", "steps": turn_state.step},
                    ]
                    orch.reflect_on_task(
                        task_description=(
                            prelude.task.raw_input
                            if hasattr(prelude.task, "raw_input")
                            else str(prelude.task.id)
                        ),
                        step=turn_state.step,
                        tool_error_count=turn_state.tool_error_count,
                        execution_trace=execution_trace,
                    )
                except Exception:
                    pass
            elif reflection_engine and prelude.task and not coordinator_owns_memory:
                try:
                    _trace: list[dict[str, Any]] = [
                        {"type": "tool_call", "count": turn_state.step},
                        {
                            "type": "error",
                            "count": turn_state.tool_error_count,
                            "content": f"{turn_state.tool_error_count} errors",
                        }
                        if turn_state.tool_error_count > 0
                        else {},
                        {"type": "assistant", "steps": turn_state.step},
                    ]
                    reflection = reflection_engine.reflect(
                        task_description=(
                            prelude.task.raw_input
                            if hasattr(prelude.task, "raw_input")
                            else str(prelude.task.id)
                        ),
                        execution_trace=execution_trace,
                    )
                    logger.info(
                        "AgentReflection: success=%s confidence=%.2f lessons=%d improvements=%d",
                        reflection.success,
                        reflection.confidence,
                        len(reflection.lessons_learned),
                        len(reflection.suggested_improvements),
                    )
                except Exception:
                    pass

            # Persist outcome feedback for the actual injected memories.
            if memory_injector and hasattr(memory_injector, "_cached_result") and not coordinator_owns_memory:
                try:
                    injected_ids = [
                        mem.id for mem in memory_injector._cached_result
                        if getattr(mem, "id", "")
                    ]
                    if memory_mgr and injected_ids:
                        memory_mgr.record_access(
                            injected_ids, usage_delta=2 if coda_summary.success else -1,
                        )
                except Exception as exc:
                    logger.warning("Memory outcome feedback persistence failed: %s", exc)

            # 路由反馈学习：记录任务结果以优化未来路由
            if smart_router and prelude.task and current_usage_observation()["cost"] is not None:
                try:
                    outcome = TaskOutcome(
                        task_text=(
                            prelude.task.raw_input
                            if hasattr(prelude.task, "raw_input")
                            else str(prelude.task.id)
                        ),
                        assigned_model=(
                            model.model_id if hasattr(model, "model_id") else "unknown"
                        ),
                        success=coda_summary.success,
                        duration_ms=telemetry.elapsed_seconds * 1000.0,
                        cost_usd=current_usage_observation()["cost"],
                        tool_errors=turn_state.tool_error_count,
                        model_switches=model_switcher.switch_count()
                        if model_switcher
                        else 0,
                    )
                    smart_router.learner().record_outcome(outcome)
                except Exception:
                    pass

        # 控制论反馈：记录模式有效性
        if enable_work_chain and feedback_controller and prelude.task:
            pattern_id = f"{prelude.task_metadata.get('intent_type', 'unknown')}_{prelude.task.id}"
            feedback_controller.record_pattern_effectiveness(
                pattern_id, coda_summary.success
            )

        # 稳定性监测：记录快照
        if stability_monitor:
            from minicode.stability_monitor import MetricSnapshot

            snapshot = MetricSnapshot(
                timestamp=time.time(),
                error_rate=float(turn_state.tool_error_count) / max(turn_state.step, 1),
                avg_latency=telemetry.average("model"),
                context_usage=context_manager.get_stats().usage_percentage
                if context_manager
                else 0.0,
                active_tasks=1,
            )
            stability_monitor.record_snapshot(snapshot)
            if context_cybernetics:
                stability_monitor.feed_orchestrator(context_cybernetics)

        # 高级控制论：最终状态报告
        if enable_work_chain:
            # 状态观测器报告
            if state_observer:
                state_summary = state_observer.get_state_summary()
                logger.info("State observer summary: %s", state_summary)

            # 预测控制器报告
            if predictive_controller:
                pred_summary = predictive_controller.get_prediction_summary()
                logger.info(
                    "Prediction summary: accuracy=%s", pred_summary.get("accuracy", {})
                )

            # 自愈引擎统计
            if self_healing_engine:
                healing_stats = self_healing_engine.get_healing_statistics()
                logger.info("Self-healing stats: %s", healing_stats)

            # 多变量解耦状态
            if decoupling_controller:
                coupling_status = decoupling_controller.get_coupling_status()
                logger.info(
                    "Coupling status: strong=%s",
                    coupling_status.get("strong_couplings", []),
                )

        # 上下文管理管线统计 (Claude Code-style + Cybernetics)
        if context_compactor:
            compactor_stats = context_compactor.get_stats()
            logger.info(
                "ContextCompactor: passes=%d persisted=%d dedup=%d "
                "microcompact=%d boundaries=%d circuit=%s",
                compactor_stats["total_passes"],
                compactor_stats["tool_results_persisted"],
                compactor_stats["read_dedup_entries"],
                compactor_stats["microcompact_tokens_cleared"],
                compactor_stats["auto_compact_boundaries"],
                "TRIPPED" if compactor_stats["circuit_breaker_tripped"] else "OK",
            )
        # 控制论闭环统计 (Engineering Cybernetics)
        if context_cybernetics:
            cyber_stats = context_cybernetics.get_stats()
            logger.info(
                "Cybernetics: cycles=%d usage=%.1f%% pid_out=%.2f "
                "predict_overflow=%s urgency=%.2f threshold=%.2f feedback_eff=%.0f%%",
                cyber_stats["cycles_executed"],
                (cyber_stats["sensor"]["current_usage"] or 0) * 100,
                cyber_stats["pid"]["last_output"] or 0,
                cyber_stats["predictor"]["turns_until_overflow"],
                cyber_stats["predictor"]["urgency"] or 0,
                cyber_stats["threshold"]["effective_threshold"] or 0,
                (cyber_stats["feedback"]["effectiveness_rate"] or 0) * 100,
            )
        # 成本控制闭环统计 (BudgetPIDController)
        if cost_control:
            cc_stats = cost_control.get_stats()
            adj = cc_stats.get("adjustment")
            logger.info(
                "CostControl: cycles=%d cost/min=$%.4f pid_out=%.2f "
                "budget_mult=%.2f threshold_mult=%.2f [%s]",
                cc_stats["cycles_executed"],
                cc_stats["sensor"]["cost_per_min"],
                cc_stats["pid"]["last_output"] or 1.0,
                adj["budget_mult"] if adj else 1.0,
                adj["threshold_mult"] if adj else 1.0,
                adj["reason"] if adj else "none",
            )
        # 双层 PID 闭环: Cybernetics → FeedbackController
        if context_cybernetics and feedback_controller:
            system_state = context_cybernetics.to_system_state()
            control_signal = feedback_controller.observe(system_state)
            if control_signal.force_compaction and context_cybernetics.enabled:
                logger.info(
                    "Dual-PID: FeedbackController force_compaction=True, "
                    "stability=%.2f performance=%.2f",
                    system_state.stability_score(),
                    system_state.performance_score(),
                )
            # Apply outer-loop ControlSignal to runtime parameters
            if control_signal.confidence > 0.6:
                if (
                    control_signal.limit_max_steps
                    and max_steps is not None
                    and control_signal.limit_max_steps < max_steps
                ):
                    logger.info(
                        "FeedbackController: limiting max_steps %d → %d",
                        max_steps,
                        control_signal.limit_max_steps,
                    )
                    max_steps = control_signal.limit_max_steps
                if control_signal.adjust_token_budget != 1.0:
                    if (
                        context_compactor
                        and hasattr(context_compactor, "_tool_budget")
                        and context_compactor._tool_budget
                    ):
                        new_budget = max(
                            1000,
                            int(
                                context_compactor._tool_budget.budget_per_message
                                * control_signal.adjust_token_budget
                            ),
                        )
                        context_compactor._tool_budget.budget_per_message = new_budget
                        logger.info(
                            "FeedbackController: token budget adjusted to %d (mult=%.2f)",
                            new_budget,
                            control_signal.adjust_token_budget,
                        )
                if control_signal.reduce_parallelism:
                    # Cap tool concurrency at 2
                    if not hasattr(tool_scheduler, "_force_max_workers"):
                        tool_scheduler._force_max_workers = 2
                    logger.info(
                        "FeedbackController: reduce_parallelism → max_workers=2 "
                        "(oscillation=%.2f)",
                        control_signal.oscillation_index,
                    )
                if control_signal.adjust_concurrency != 0:
                    cap = max(1, 4 + control_signal.adjust_concurrency)
                    tool_scheduler._force_max_workers = cap
                    logger.info(
                        "FeedbackController: adjust_concurrency=%+d → max_workers=%d",
                        control_signal.adjust_concurrency,
                        cap,
                    )
                if control_signal.increase_model_level:
                    logger.info(
                        "FeedbackController: model upgrade recommended (errors=%.2f perf=%.2f)",
                        system_state.error_frequency,
                        system_state.performance_score(),
                    )
                    if model_switcher:
                        if hasattr(model_switcher, "_pending_upgrade"):
                            model_switcher._pending_upgrade = True  # type: ignore[attr-defined]
                if control_signal.decrease_model_level:
                    logger.info(
                        "FeedbackController: model downgrade recommended (efficiency=%.2f)",
                        system_state.token_efficiency,
                    )
                if control_signal.suggest_memory_persistence:
                    logger.info("FeedbackController: persisting working memory")
                    if context_compactor and hasattr(context_compactor, "_tool_budget"):
                        try:
                            context_compactor._tool_budget.flush()
                        except Exception:
                            pass
                if control_signal.recommend_skill_update:
                    logger.info(
                        "FeedbackController: skill update recommended (pattern=%.2f)",
                        system_state.pattern_reuse_rate,
                    )
                    if not hasattr(tool_scheduler, "_pending_skill_update"):
                        tool_scheduler._pending_skill_update = True

                if control_signal.reduce_tool_timeout:
                    new_timeout = max(5.0, control_signal.reduce_tool_timeout)
                    if hasattr(tool_scheduler, "_force_tool_timeout"):
                        tool_scheduler._force_tool_timeout = new_timeout  # type: ignore[attr-defined]
                    logger.info(
                        "FeedbackController: tool timeout reduced to %.1fs",
                        new_timeout,
                    )
                elif hasattr(tool_scheduler, "_force_tool_timeout"):
                    if hasattr(tool_scheduler, "_force_tool_timeout"):
                        del tool_scheduler._force_tool_timeout

                if control_signal.increase_nudge_frequency:
                    tool_scheduler._force_nudge_frequency = True  # type: ignore[attr-defined]
                    logger.info(
                        "FeedbackController: nudge frequency increased (stability=%.2f)",
                        system_state.stability_score(),
                    )
                elif hasattr(tool_scheduler, "_force_nudge_frequency"):
                    if hasattr(tool_scheduler, "_force_nudge_frequency"):
                        del tool_scheduler._force_nudge_frequency

                if control_signal.promote_pattern:
                    feedback_controller.record_pattern_effectiveness(
                        control_signal.promote_pattern, True
                    )
                    logger.info(
                        "FeedbackController: pattern promoted '%s'",
                        control_signal.promote_pattern,
                    )

                if (
                    control_signal.force_compaction
                    and legacy_context_management_allowed
                    and context_compactor
                ):
                    try:
                        compacted = (
                            context_compactor.compact_messages()
                            if hasattr(context_compactor, "compact_messages")
                            else False
                        )
                        logger.info(
                            "FeedbackController: forced compaction (%d messages)",
                            len(compacted) if compacted else 0,
                        )
                    except Exception as exc:
                        logger.warning(
                            "FeedbackController: forced compaction failed: %s", exc
                        )

            # 自适应PID调参：每20轮自动调节内外环PID参数
            if (
                adaptive_pid_tuner
                and step > 0
                and step % 20 == 0
                and feedback_controller
            ):
                try:
                    stability_error = 1.0 - system_state.stability_score()
                    perf_score = system_state.performance_score()
                    tuned = adaptive_pid_tuner.tune(
                        stability_error, dt=1.0, performance_score=perf_score
                    )
                    if tuned and adaptive_pid_tuner._performance_history:
                        recent_perf = adaptive_pid_tuner._performance_history[-5:]
                        avg_perf = sum(recent_perf) / len(recent_perf)
                        if context_cybernetics:
                            cp = context_cybernetics.pid
                            cp.kp = tuned.kp
                            cp.ki = tuned.ki
                            cp.kd = tuned.kd
                            logger.info(
                                "AdaptivePIDTuner: context PID tuned kp=%.3f ki=%.3f kd=%.3f "
                                "method=%s perf=%.2f",
                                tuned.kp,
                                tuned.ki,
                                tuned.kd,
                                adaptive_pid_tuner._active_method.value
                                if hasattr(adaptive_pid_tuner, "_active_method")
                                else "unknown",
                                avg_perf,
                            )
                except Exception:
                    pass  # 调参失败不能拖垮主循环

        # 总监督层: 汇总局部控制器输出为统一风险视图
        try:
            if cybernetic_supervisor:
                supervisor_snapshots = []
                if context_cybernetics:
                    supervisor_snapshots.append(
                        cybernetic_supervisor.snapshot_from_context(
                            context_cybernetics.get_stats()
                        )
                    )
                if cost_control:
                    supervisor_snapshots.append(
                        cybernetic_supervisor.snapshot_from_cost(cost_control.get_stats())
                    )
                if tool_scheduler.last_decision:
                    supervisor_snapshots.append(
                        cybernetic_supervisor.snapshot_from_tool_decision(
                            tool_scheduler.last_decision.to_dict()
                        )
                    )
                supervisor_report = cybernetic_supervisor.report(supervisor_snapshots)
                save_supervisor_report(supervisor_report)
                logger.info(
                    "CyberneticSupervisor: health=%.2f risk=%s actions=%s",
                    supervisor_report.overall_health,
                    supervisor_report.risk_level.value,
                    "; ".join(supervisor_report.recommended_actions[:3]),
                )
        except Exception as error:  # Optional diagnostics cannot replace the turn outcome.
            logger.warning("Could not save supervisor diagnostics (%s)", type(error).__name__)
        if paged_memory_finalization_error is not None:
            raise paged_memory_finalization_error
