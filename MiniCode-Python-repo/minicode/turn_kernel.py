from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
import re
from typing import Any, Callable, Literal

from minicode.layered_context import ContextBuilder, LayeredContext
from minicode.output_budget import OutputRecovery
from minicode.runtime_feedback import (
    VERIFY_FOLLOWUP, EXPLORE_FOLLOWUP, EMPTY_VERIFY_FOLLOWUP,
    EMPTY_WIDENING_FOLLOWUP, RESUME_AFTER_TRANSPORT,
)
from minicode.task_object import TaskState
from minicode.types import RuntimeEventCategory

TurnStopReason = Literal[
    "done",
    "max_steps",
    "await_user",
    "waiting_children",
    "candidate_submitted",
    "builder_submitted",
    "consultation_submitted",
    "blocked",
    "budget_exhausted",
    "provider_balance",
    "provider_error",
    "consultation_budget_limited",
    "model_usage_unreconciled",
    "consultation_input_exhausted",
    "tool_input_exhausted",
    "model_output_truncated",
    "transport_incomplete",
    "verification_failed",
    "widen_needed",
    "tool_timeout",
    "canceled",
]

TurnStepPhase = Literal["explore", "execute", "verify"]


@dataclass(slots=True)
class TurnBudgetSignals:
    remaining_steps: int | None = None
    hit_max_steps: bool = False
    tool_error_count: int = 0
    saw_tool_result: bool = False


@dataclass(slots=True)
class TurnVerificationState:
    strict: bool = False
    requires_explicit_final: bool = False
    requires_evidence: bool = False
    evidence_ready: bool = False
    evidence_summary: str = ""
    last_verification_note: str = ""
    evidence_retry_count: int = 0


@dataclass(slots=True)
class TurnStepPolicy:
    phase: TurnStepPhase = "explore"
    phase_index: int = 0
    remaining_steps: int | None = None
    guidance: str = ""
    verification_focus: str = "light"
    allow_widening: bool = False
    widening_active: bool = False
    widening_reason: str = ""
    widening_evidence_summary: str = ""
    should_compact_aggressively: bool = False

    def terminal_summary(self) -> str:
        parts = [f"phase={self.phase}"]
        if self.guidance:
            parts.append(self.guidance)
        if self.allow_widening:
            if self.widening_reason:
                parts.append(
                    f"widening is now allowed because {self.widening_reason}"
                )
            else:
                parts.append("widening is now allowed if depth stalls")
        if self.widening_active:
            parts.append("widened mode is active")
        if self.should_compact_aggressively:
            parts.append("favor compact evidence over long narration")
        return " | ".join(parts)


@dataclass(slots=True)
class StableTaskPack:
    task_title: str = ""
    task_goal: str = ""
    task_description: str = ""
    intent_type: str = ""
    action_type: str = ""
    task_graph_summary: str = ""
    protected_context: list[str] = field(default_factory=list)
    latest_tool_result_summary: str = ""
    progress_summary: str = ""
    verification_summary: str = ""
    budget_summary: str = ""

    def to_protected_text(self, *, result_is_preview: bool = False) -> str:
        lines: list[str] = []
        if self.task_title:
            lines.append(f"Task: {self.task_title}")
        if self.task_goal:
            lines.append(f"Goal: {self.task_goal}")
        if self.task_description:
            lines.append(f"Description: {self.task_description}")
        if self.intent_type or self.action_type:
            lines.append(
                f"Intent: {self.intent_type or 'unknown'} / {self.action_type or 'unknown'}"
            )
        if self.task_graph_summary:
            lines.append(f"Task graph: {self.task_graph_summary}")
        if self.progress_summary:
            lines.append(f"Progress: {self.progress_summary}")
        if self.latest_tool_result_summary:
            from minicode.runtime_progress import RESULT_PREVIEW_LABEL
            label = RESULT_PREVIEW_LABEL if result_is_preview else "Latest tool result"
            lines.append(f"{label}: {self.latest_tool_result_summary}")
        if self.verification_summary:
            lines.append(f"Verification: {self.verification_summary}")
        if self.budget_summary:
            lines.append(f"Budget: {self.budget_summary}")
        if self.protected_context:
            lines.append("Protected context:")
            for item in self.protected_context[:5]:
                lines.append(f"- {item[:240]}")
        return "\n".join(lines)


@dataclass(slots=True)
class TurnPreludeState:
    """Prelude artifacts prepared once before the recurrent tool loop."""

    task: Any | None = None
    task_metadata: dict[str, Any] = field(default_factory=dict)
    layered_context: LayeredContext | None = None
    context_builder: ContextBuilder | None = None
    auditor: Any | None = None
    task_graph: Any | None = None
    task_graph_id: str | None = None
    task_slot_key: str | None = None


@dataclass(slots=True)
class TurnRecurrentState:
    """Mutable loop state for a single agent turn."""

    max_steps: int | None
    profile_name: str = "single"
    widen_after_step: int | None = None
    empty_response_retry_limit: int = 2
    recoverable_thinking_retry_limit: int = 3
    # Bound repairs between verified file revisions. The lifetime counter remains
    # monotonic for audit; reads, prose, no-op writes and widening grant no credit.
    repair_retry_limit: int = 4
    repair_retry_total: int = 0
    repair_retry_checkpoint: int = 0
    truncated_output_retry_limit: int = 3
    truncated_output_consecutive_count: int = 0
    transport_retry_limit: int = 2
    transport_retry_count: int = 0
    output_recovery: OutputRecovery = field(default_factory=OutputRecovery)
    write_revisions: set[str] = field(default_factory=set)
    saw_tool_result: bool = False
    empty_response_retry_count: int = 0
    recoverable_thinking_retry_count: int = 0
    truncated_output_retry_count: int = 0
    successful_write_observation_count: int = 0
    tool_error_count: int = 0
    tool_observation_count: int = 0
    successful_tool_observation_count: int = 0
    step: int = 0
    widening_active: bool = False
    widening_transition_count: int = 0
    widening_trigger_reason: str = ""
    widening_trigger_evidence: str = ""
    latest_tool_result_summary: str = ""
    progress_state: dict[str, Any] = field(default_factory=dict)
    verification_state: TurnVerificationState = field(default_factory=TurnVerificationState)
    budget_signals: TurnBudgetSignals = field(default_factory=TurnBudgetSignals)
    stop_reason: TurnStopReason | None = None
    wait_kind: Literal["input", "approval"] | None = None
    timed_out: bool = False
    ambiguous_side_effect: bool = False
    unsafe_effect_observed: bool = False
    unsettled_tool: bool = False
    stable_task_pack: StableTaskPack | None = None
    step_policy: TurnStepPolicy = field(default_factory=TurnStepPolicy)

    def has_remaining_steps(self) -> bool:
        return self.max_steps is None or self.step < self.max_steps

    def begin_step(self) -> int:
        self.step += 1
        self._refresh_budget_signals()
        return self.step

    def repair_retries_available(self) -> bool:
        """Whether this activation may still spend any repair continuation.

        Read-only: no model admission or extra task budget is granted here.
        """
        return self.repair_retry_total - self.repair_retry_checkpoint < self.repair_retry_limit

    def can_retry_empty_response(self) -> bool:
        return (self.empty_response_retry_count < self.empty_response_retry_limit
                and self.repair_retries_available())

    def record_empty_response_retry(self) -> None:
        self.empty_response_retry_count += 1
        self.repair_retry_total += 1

    def can_retry_truncated_output(self) -> bool:
        return self.repair_retries_available() and (
            self.truncated_output_consecutive_count < self.truncated_output_retry_limit)

    def record_truncated_output_retry(self) -> None:
        self.truncated_output_retry_count += 1
        self.truncated_output_consecutive_count += 1
        self.repair_retry_total += 1

    def can_retry_recoverable_thinking(self) -> bool:
        return (
            self.recoverable_thinking_retry_count
            < self.recoverable_thinking_retry_limit
            and self.repair_retries_available()
        )

    def record_recoverable_thinking_retry(self) -> None:
        self.recoverable_thinking_retry_count += 1
        self.repair_retry_total += 1

    def record_tool_result(
        self,
        ok: bool,
        summary: str | None = None,
        *,
        wait_kind: Literal["input", "approval"] | None = None,
        timed_out: bool = False,
        ambiguous_side_effect: bool = False,
        unsafe_effect: bool = False,
        unsettled_tool: bool = False,
        workspace_write: bool = False,
        write_revision: str | None = None,
        interrupted_before_execution: bool = False,
    ) -> None:
        self.saw_tool_result = True
        self.tool_observation_count += 1
        if ok:
            self.successful_tool_observation_count += 1
            if (workspace_write and write_revision and write_revision not in self.write_revisions
                and not (wait_kind or timed_out or ambiguous_side_effect)):
                self.write_revisions.add(write_revision)
                self.successful_write_observation_count += 1
                self.truncated_output_consecutive_count = 0
                self.repair_retry_checkpoint = self.repair_retry_total
        # A native unstarted steering interruption is neither an execution
        # failure nor successful evidence. Contradictory effect flags fail closed.
        if not ok and (not interrupted_before_execution
                       or wait_kind or timed_out or ambiguous_side_effect or unsafe_effect or unsettled_tool or write_revision):
            self.tool_error_count += 1
        if summary:
            normalized = " ".join(summary.split())
            self.latest_tool_result_summary = normalized[:280]
            if ok:
                self.verification_state.evidence_summary = normalized[:200]
                self.verification_state.evidence_ready = True
        if wait_kind is not None:
            self.wait_kind = wait_kind
        self.timed_out = self.timed_out or timed_out
        self.ambiguous_side_effect = (
            self.ambiguous_side_effect or ambiguous_side_effect
        )
        self.unsafe_effect_observed = self.unsafe_effect_observed or unsafe_effect
        self.unsettled_tool = self.unsettled_tool or unsettled_tool
        self._refresh_budget_signals()

    def set_progress_summary(self, summary: str) -> None:
        self.progress_state["summary"] = summary[:280]

    def set_stop_reason(self, reason: TurnStopReason) -> None:
        self.stop_reason = reason
        self._refresh_budget_signals()

    def has_verification_evidence(self) -> bool:
        return (
            self.successful_tool_observation_count > 0
            and bool(self.verification_state.evidence_summary)
        )

    def activate_widening(self, *, extra_steps: int = 0) -> bool:
        if self.widening_active:
            return False
        self.widening_active = True
        self.widening_transition_count += 1
        self.empty_response_retry_count = 0
        self.recoverable_thinking_retry_count = 0
        if extra_steps > 0 and self.max_steps is not None:
            self.max_steps += extra_steps
        self._refresh_budget_signals()
        return True

    def final_task_state(self) -> TaskState:
        if self.stop_reason == "done":
            return (
                TaskState.FAILED
                if self.timed_out or self.ambiguous_side_effect
                else TaskState.COMPLETED
            )
        if self.stop_reason in {"await_user", "candidate_submitted", "builder_submitted", "consultation_submitted"}:
            return TaskState.PAUSED
        if self.stop_reason in {
            "max_steps",
            "blocked",
            "verification_failed",
            "widen_needed",
            "tool_timeout",
            "canceled",
        }:
            return TaskState.FAILED
        # A missing terminal reason is a protocol failure, even when no tool
        # error happened.  Coda consumers must never infer success from a
        # quiet/exceptional exit.
        return TaskState.FAILED

    def _refresh_budget_signals(self) -> None:
        remaining_steps = None
        hit_max_steps = False
        if self.max_steps is not None:
            remaining_steps = max(self.max_steps - self.step, 0)
            hit_max_steps = self.step >= self.max_steps
        self.budget_signals = TurnBudgetSignals(
            remaining_steps=remaining_steps,
            hit_max_steps=hit_max_steps,
            tool_error_count=self.tool_error_count,
            saw_tool_result=self.saw_tool_result,
        )


@dataclass(slots=True)
class AssistantTurnDecision:
    """Structured outcome for one assistant response inside the recurrent loop."""

    kind: Literal["progress", "retry", "fallback", "final"]
    assistant_content: str | None = None
    user_content: str | None = None
    protect_final_answer: bool = False
    stop_reason: TurnStopReason | None = None
    runtime_event_category: RuntimeEventCategory | None = None
    # Only pure flow guidance may be omitted from handoff freshness checks.
    # Verification evidence and new constraints deliberately keep the default.
    feedback_kind: Literal["continuation"] | None = None


@dataclass(slots=True)
class ToolTurnDecision:
    kind: Literal["continue", "await_user", "stop"]
    assistant_content: str | None = None
    stop_reason: TurnStopReason | None = None
    progress_summary: str = ""


@dataclass(slots=True)
class TurnCodaSummary:
    step: int
    tool_error_count: int
    success: bool
    result_summary: str
    error_rate: float
    avg_latency: float
    context_usage: float
    task_state: TaskState
    stop_reason: TurnStopReason | None


def _summarize_task_graph(task_graph: Any | None, task_slot_key: str | None) -> str:
    if task_graph is None:
        return ""
    progress = 0.0
    try:
        progress = float(task_graph.get_progress_percentage())
    except Exception:
        progress = 0.0
    slot_state = ""
    if task_slot_key:
        try:
            slot = task_graph.slots.get(task_slot_key)
            if slot is not None and getattr(slot, "state", None) is not None:
                slot_state = getattr(slot.state, "value", str(slot.state))
        except Exception:
            slot_state = ""
    parts = [f"progress={progress:.0f}%"]
    if slot_state:
        parts.append(f"slot={slot_state}")
    return ", ".join(parts)


def _derive_widening_signal(
    turn_state: TurnRecurrentState,
    *,
    step: int,
) -> tuple[bool, str, str]:
    if turn_state.widening_active:
        return False, "", ""
    if turn_state.widen_after_step is None or step < turn_state.widen_after_step:
        return False, "", ""

    if turn_state.tool_error_count > 0:
        evidence = (
            turn_state.latest_tool_result_summary
            or f"{turn_state.tool_error_count} tool error(s) observed in this run"
        )
        return True, "tool failures already made the narrow path unstable", evidence[:200]

    if turn_state.has_verification_evidence() and (
        turn_state.empty_response_retry_count > 0
        or turn_state.recoverable_thinking_retry_count > 0
    ):
        evidence = (
            turn_state.latest_tool_result_summary
            or "the narrow path produced evidence but the next step still stalled"
        )
        return True, "the narrow path already produced evidence and then stalled", evidence[:200]

    if (
        not turn_state.saw_tool_result
        and turn_state.empty_response_retry_count >= turn_state.empty_response_retry_limit
    ):
        return (
            True,
            "the model stalled repeatedly before producing new evidence",
            (
                "assistant returned repeated empty responses while the turn stayed on "
                "the same narrow path"
            ),
        )

    if (
        not turn_state.saw_tool_result
        and turn_state.recoverable_thinking_retry_count
        >= turn_state.recoverable_thinking_retry_limit
    ):
        return (
            True,
            "recoverable pauses kept repeating on the same narrow path",
            (
                "the model kept hitting recoverable pause/max-token retries without "
                "producing fresh external evidence"
            ),
        )

    return False, "", ""


def derive_turn_step_policy(turn_state: TurnRecurrentState, *, read_only: bool = False) -> TurnStepPolicy:
    """Derive the current per-step policy from budget, profile, and progress."""

    step = max(turn_state.step, 1)
    max_steps = turn_state.max_steps or 0
    remaining_steps = turn_state.budget_signals.remaining_steps
    evidence_ready = turn_state.has_verification_evidence()

    verify_after = max(3, ceil(max_steps * 0.7)) if max_steps else 6
    if turn_state.verification_state.strict:
        verify_after = min(verify_after, 4 if max_steps else 4)
    execute_after = 2 if turn_state.profile_name == "single-deep" else 1

    if turn_state.widening_active and not (
        remaining_steps is not None and remaining_steps <= 1
    ):
        phase: TurnStepPhase = "execute"
    elif step <= execute_after:
        phase: TurnStepPhase = "explore"
    elif (
        (max_steps and step >= verify_after)
        or (remaining_steps is not None and remaining_steps <= 2)
        or (
            turn_state.verification_state.strict
            and turn_state.saw_tool_result
            and step >= execute_after + 1
        )
    ):
        phase = "verify"
    else:
        phase = "execute"

    allow_widening, widening_reason, widening_evidence_summary = (
        _derive_widening_signal(turn_state, step=step)
    )

    if turn_state.widening_active:
        guidance = (
            "compare alternative approaches, reuse the evidence you already have, "
            "and avoid repeating the same narrow line of attack"
        )
        verification_focus = "normal"
    elif phase == "explore":
        guidance = "inspect, decompose, and anchor the task before committing"
        verification_focus = "light"
    elif phase == "execute":
        guidance = "prefer concrete tool use and incremental edits"
        verification_focus = "normal"
    else:
        guidance = "verify changes, test evidence, and finalize only with support"
        verification_focus = "strict" if turn_state.verification_state.strict else "normal"

    if read_only:
        # Scope changes the guidance, never evidence requirements or the stop
        # policy. A read-only finding is not verification of the primary's edits.
        guidance = {
            "explore": "inspect evidence relevant to the assigned question; avoid unrelated exploration",
            "execute": "resolve remaining evidence gaps; once supported, return findings without extra tool calls",
            "verify": "check the assigned claims against evidence, report limitations, and finalize the read-only report",
        }[phase]
        if turn_state.widening_active:
            guidance += "; consider alternative explanations within the assignment"

    policy = TurnStepPolicy(
        phase=phase,
        phase_index=step,
        remaining_steps=remaining_steps,
        guidance=guidance,
        verification_focus=verification_focus,
        allow_widening=allow_widening,
        widening_active=turn_state.widening_active,
        widening_reason=widening_reason,
        widening_evidence_summary=widening_evidence_summary,
        should_compact_aggressively=(
            phase == "verify" or allow_widening or turn_state.widening_active
        ),
    )
    turn_state.step_policy = policy
    turn_state.widening_trigger_reason = widening_reason
    turn_state.widening_trigger_evidence = widening_evidence_summary
    turn_state.verification_state.requires_explicit_final = phase == "verify"
    turn_state.verification_state.requires_evidence = (
        phase == "verify"
        and turn_state.verification_state.strict
        and turn_state.saw_tool_result
    )
    turn_state.verification_state.evidence_ready = evidence_ready
    if evidence_ready and not turn_state.verification_state.evidence_summary:
        turn_state.verification_state.evidence_summary = turn_state.latest_tool_result_summary[:200]
    turn_state.verification_state.last_verification_note = (
        f"phase={phase}, verification={verification_focus}, "
        f"widening={'active' if turn_state.widening_active else ('ready' if allow_widening else 'hold')}, "
        f"evidence={'ready' if turn_state.verification_state.evidence_ready else 'missing'}"
    )
    if widening_reason:
        turn_state.verification_state.last_verification_note += (
            f", widening_reason={widening_reason}"
        )
    return policy


def render_turn_policy_message(
    *,
    previous_policy: TurnStepPolicy | None,
    current_policy: TurnStepPolicy,
) -> str | None:
    """Return a compact terminal-visible policy update when the phase meaningfully changes."""

    if previous_policy is not None:
        if (
            previous_policy.phase_index > 0
            and previous_policy.phase == current_policy.phase
            and previous_policy.allow_widening == current_policy.allow_widening
            and previous_policy.widening_active == current_policy.widening_active
        ):
            return None
    message = (
        f"Runtime phase: {current_policy.phase}. {current_policy.guidance} "
        f"(verification={current_policy.verification_focus}, "
        f"remaining_steps="
        f"{'open' if current_policy.remaining_steps is None else current_policy.remaining_steps})."
    )
    if current_policy.allow_widening:
        if current_policy.widening_reason:
            message += (
                " Widening is now available because "
                f"{current_policy.widening_reason}."
            )
        else:
            message += " Widening is now available if the current path keeps stalling."
    if current_policy.widening_active:
        message += " Widened mode is active."
    return message


def _step_aware_followup_nudge(
    *,
    step_policy: TurnStepPolicy | None,
    saw_tool_result: bool,
    nudge_continue: str,
    nudge_after_tool_result: str,
) -> str:
    if step_policy is None:
        return nudge_after_tool_result if saw_tool_result else nudge_continue
    if step_policy.phase == "verify":
        return VERIFY_FOLLOWUP
    if step_policy.phase == "explore" and not saw_tool_result:
        return EXPLORE_FOLLOWUP
    return nudge_after_tool_result if saw_tool_result else nudge_continue


def _content_mentions_evidence(content: str, evidence_summary: str) -> bool:
    """Recognize a reference to evidence, not certify the claim made about it.

    Structured node/final verification remains responsible for correctness.
    English substrings (e.g. ``test`` inside ``latest``) are not references, and
    ordinary Chinese validation language must not need an English paraphrase.
    """
    # Protocol markers are control syntax, never a citation of evidence. In
    # particular, a bare <verification_passed> must not satisfy this guard just
    # because its spelling contains the English word "verification".
    content = re.sub(r"<verification_(?:passed|failed)>|</?(?:progress|final)>", "", content, flags=re.I)
    normalized_content = " ".join(content.lower().split())
    if not normalized_content:
        return False
    english_markers = (
        "verified",
        "verification",
        "validated",
        "test",
        "tests",
        "testing",
        "checked",
        "inspected",
        "confirmed",
        "according to",
        "based on",
        "tool output",
        "output shows",
        "log shows",
        "diff shows",
        "i ran",
        "i checked",
    )
    chinese_markers = (
        "根据",
        "来源",
        "报道",
        "已核实",
        "已核验",
        "证据",
        "结果显示",
        "网页显示",
        "测试",
        "验证",
        "检查",
        "验收",
        "核验",
        "校验",
        "日志显示",
        "输出显示",
        "差异显示",
    )
    if any(marker in normalized_content for marker in chinese_markers) or any(
        re.search(r"(?<![a-z])" + re.escape(marker) + r"(?![a-z])", normalized_content)
        for marker in english_markers
    ):
        return True
    evidence_tokens = [
        token.strip(".,:;()[]{}'\"")
        for token in evidence_summary.lower().split()
        if len(token.strip(".,:;()[]{}'\"")) >= 4
    ]
    overlap = sum(1 for token in set(evidence_tokens[:8]) if token and token in normalized_content)
    return overlap >= 2


def build_verification_evidence_nudge(evidence_summary: str) -> str:
    evidence_fragment = evidence_summary[:180].strip()
    if evidence_fragment:
        return (
            "You are in strict verification mode. Before finalizing, cite the strongest "
            f"evidence from this run, for example: {evidence_fragment}. If that evidence "
            "is insufficient, run one more validation step or state the exact blocker."
        )
    return (
        "You are in strict verification mode. Before finalizing, summarize the concrete "
        "evidence from this run or state the exact blocker. Do not end with an unsupported conclusion."
    )


def build_widening_transition_nudge(
    latest_tool_result_summary: str,
    *,
    widening_reason: str = "",
    widening_evidence_summary: str = "",
) -> str:
    evidence_fragment = (
        widening_evidence_summary[:180].strip()
        or latest_tool_result_summary[:180].strip()
    )
    reason_fragment = widening_reason.strip()
    if evidence_fragment:
        lead = (
            "Switch to widened mode because "
            f"{reason_fragment}. "
            if reason_fragment
            else "Switch to widened mode. "
        )
        return (
            lead
            + "Do not keep pushing the same narrow path. Compare at least two "
            "alternative approaches, reuse the strongest evidence already gathered, "
            f"and choose the next step grounded in this run: {evidence_fragment}"
        )
    if reason_fragment:
        return (
            "Switch to widened mode because "
            f"{reason_fragment}. Do not keep pushing the same narrow path. Compare "
            "at least two alternative approaches, inspect a different source of "
            "evidence, and then choose the most promising next step."
        )
    return (
        "Switch to widened mode. Do not keep pushing the same narrow path. Compare at least "
        "two alternative approaches, inspect a different source of evidence, and then choose "
        "the most promising next step."
    )


def build_stable_task_pack(
    *,
    task: Any | None,
    task_metadata: dict[str, Any] | None,
    protected_context: list[str] | None,
    task_graph: Any | None,
    task_slot_key: str | None,
    latest_tool_result_summary: str,
    progress_state: dict[str, Any] | None,
    verification_state: TurnVerificationState | None,
    budget_signals: TurnBudgetSignals | None,
) -> StableTaskPack | None:
    if task is None and not protected_context and not latest_tool_result_summary:
        return None

    metadata = task_metadata or {}
    progress_summary = ""
    if progress_state:
        progress_summary = str(progress_state.get("summary", ""))

    verification_summary = ""
    if verification_state:
        verification_parts = []
        if verification_state.strict:
            verification_parts.append("strict")
        if verification_state.requires_explicit_final:
            verification_parts.append("explicit-final")
        if verification_state.requires_evidence:
            verification_parts.append("evidence-required")
        if verification_state.evidence_ready:
            verification_parts.append("evidence-ready")
        if verification_state.evidence_summary:
            verification_parts.append(f"evidence={verification_state.evidence_summary[:120]}")
        if verification_state.last_verification_note:
            verification_parts.append(verification_state.last_verification_note)
        verification_summary = ", ".join(verification_parts)

    budget_summary = ""
    if budget_signals:
        remaining = (
            "open"
            if budget_signals.remaining_steps is None
            else str(budget_signals.remaining_steps)
        )
        budget_summary = (
            f"remaining_steps={remaining}, "
            f"tool_errors={budget_signals.tool_error_count}, "
            f"saw_tool_result={budget_signals.saw_tool_result}"
        )

    return StableTaskPack(
        task_title=str(getattr(task, "title", "") or ""),
        task_goal=str(getattr(task, "goal", "") or ""),
        task_description=str(getattr(task, "description", "") or ""),
        intent_type=str(metadata.get("intent_type", "") or ""),
        action_type=str(metadata.get("action_type", "") or ""),
        task_graph_summary=_summarize_task_graph(task_graph, task_slot_key),
        protected_context=list(protected_context or []),
        latest_tool_result_summary=latest_tool_result_summary,
        progress_summary=progress_summary,
        verification_summary=verification_summary,
        budget_summary=budget_summary,
    )


def build_turn_coda_summary(
    *,
    turn_state: TurnRecurrentState,
    context_usage: float,
) -> TurnCodaSummary:
    """Build a normalized turn summary for coda/finalization logic."""

    task_state = turn_state.final_task_state()
    success = task_state is TaskState.COMPLETED
    if turn_state.stop_reason == "await_user":
        result_summary = (
            f"Turn paused after {turn_state.step} steps, "
            f"{turn_state.tool_error_count} errors"
        )
    elif turn_state.stop_reason == "max_steps":
        result_summary = (
            f"Turn stopped at the max step budget after {turn_state.step} steps, "
            f"{turn_state.tool_error_count} errors"
        )
    else:
        result_summary = (
            f"Turn finished with stop_reason={turn_state.stop_reason or 'implicit'}, "
            f"{turn_state.step} steps, {turn_state.tool_error_count} errors"
        )
    return TurnCodaSummary(
        step=turn_state.step,
        tool_error_count=turn_state.tool_error_count,
        success=success,
        result_summary=result_summary,
        error_rate=turn_state.tool_error_count / max(turn_state.step, 1),
        avg_latency=turn_state.step * 2.0,
        context_usage=context_usage,
        task_state=task_state,
        stop_reason=turn_state.stop_reason,
    )


def finalize_work_chain_task(
    *,
    task: Any | None,
    auditor: Any | None,
    coda_summary: TurnCodaSummary,
    success_outcome: Any,
    failure_outcome: Any,
) -> None:
    """Apply final task state and audit completion during coda."""

    if task is None:
        return

    task.set_state(coda_summary.task_state)
    task.result_summary = coda_summary.result_summary

    if auditor is None:
        return

    auditor.complete_decision(
        success_outcome if coda_summary.success else failure_outcome,
        coda_summary.step * 100.0,
        task.result_summary,
        task.error_message if not coda_summary.success else "",
    )


def decide_truncated_model_output(
    *, turn_state: TurnRecurrentState, stop_reason: str | None, tool_batch: bool = False,
) -> AssistantTurnDecision | None:
    """Provider termination outranks nonempty text, progress and evidence claims.

    A truncated response is never a result, and a truncated TOOL BATCH is never
    executed: the proposal may be only a prefix, so no call from it reaches a tool,
    hook, approval or submission. That refusal is not the same decision as ending
    the run, though, and conflating them is what made an otherwise healthy run die
    the moment the provider hit its output ceiling.

    Recovery stays in this activation, retaining completed tool effects. Known
    automatic allowances can grow, and each retry requests a smaller proposal.
    Consecutive failures are bounded; only new native file revisions refresh the
    recovery window. Every request still passes the unchanged task budget gate.
    """
    if stop_reason not in {"length", "max_tokens"}:
        return None
    if turn_state.can_retry_truncated_output() and turn_state.has_remaining_steps():
        turn_state.record_truncated_output_retry()
        increased = turn_state.output_recovery.increase()
        attempt = turn_state.truncated_output_consecutive_count
        sizing = (
            "If a tool is needed, propose only ONE small, complete tool call using already authorized tools. "
            "Never continue raw partial JSON, repeat prior analysis or replay already completed commands. "
        )
        if tool_batch:
            sizing += (
                "For file implementation tasks where editing is authorized, prefer targeted edits; "
                "create a new large file as a small valid skeleton, then extend it with targeted edits. "
                "write_file replaces the entire file: never use it to append a later chunk. "
            )
        if attempt >= 2:
            sizing += "Reduce the next result to one small section or one authorized operation. "
        if attempt >= 3:
            sizing += "Keep the next report or tool arguments under roughly 512 tokens; continue any remaining work in subsequent steps. "
        if increased:
            sizing += f"Automatic output allowance increased from {increased[0]} to {increased[1]} tokens, subject to admission. "
        context_recovery = turn_state.output_recovery.latest.get("context_recovery_requested") is True
        if context_recovery:
            sizing += ("The previous output filled the remaining context window. Make room using a fresh "
                       "native handoff note and context rotation when available before resuming business work; "
                       "do not repeat the same oversized request or discard required evidence. ")
        if tool_batch:
            guidance = (
                f"The provider truncated your previous response (stop_reason={stop_reason}) "
                "while you were proposing tool calls, so that ENTIRE batch was refused: no "
                "call in it was executed, and it is not a completed result. Continue "
                "within the existing budget. " + sizing + "Finish the missing result "
                "concisely without repeating prior analysis. State the exact blocker if "
                "one exists; never claim unseen work completed."
            )
        else:
            guidance = (
                f"The provider truncated your previous response (stop_reason={stop_reason}); "
                "it is not a completed result. Continue within the existing budget. "
                + sizing + "Finish the missing result concisely without repeating prior analysis, or "
                "state the exact blocker. Use the native handoff tool when one is provided; "
                "the final report must be self-contained and include all required findings. "
                "Do not claim unseen work completed."
            )
        status = "本次输出达到长度限制，正在保留已有进度并缩小下一步输出。"
        if increased:
            status += f"自动输出额度已调整为 {increased[1]:,} Token，仍受任务预算限制。"
        if context_recovery:
            status += "正在为思考与输出腾出上下文空间，总窗口上限保持不变。"
        if tool_batch:
            status += "本次不完整的工具调用未执行。"
        return AssistantTurnDecision(kind="retry", runtime_event_category="recovery",
                                     user_content=guidance, assistant_content=status, feedback_kind="continuation")
    detail = ("No tools from this incomplete response were executed."
              if tool_batch else "The bounded continuation is exhausted or no turn steps remain.")
    return AssistantTurnDecision(kind="fallback", stop_reason="model_output_truncated",
        assistant_content=f"Model output was truncated (stop_reason={stop_reason}). {detail} No completed result was accepted.")


def decide_assistant_turn(
    *,
    turn_state: TurnRecurrentState,
    step_content: str,
    step_kind: str | None,
    stop_reason: str | None,
    block_types: list[str] | None,
    ignored_block_types: list[str] | None,
    is_empty: bool,
    treat_as_progress: bool,
    is_recoverable_thinking_stop: bool,
    format_diagnostics: Callable[[str | None, list[str] | None, list[str] | None], str],
    nudge_continue: str,
    nudge_after_tool_result: str,
    resume_after_pause: str,
    resume_after_max_tokens: str,
    nudge_after_empty_response: str,
    nudge_after_empty_no_tools: str,
    step_policy: TurnStepPolicy | None = None,
    transport_complete: bool = True,
) -> AssistantTurnDecision:
    """Decide how the loop should react to an assistant-only step."""

    if not transport_complete:
        # Recover only the model response in this activation: tool receipts and
        # completed effects remain intact. Incomplete text is never a final.
        if (turn_state.transport_retry_count < turn_state.transport_retry_limit
                and turn_state.repair_retries_available() and turn_state.has_remaining_steps()):
            turn_state.transport_retry_count += 1
            turn_state.repair_retry_total += 1
            return AssistantTurnDecision(kind="retry", runtime_event_category="recovery",
                assistant_content="模型响应传输不完整，正在重新请求完整结果。",
                user_content=RESUME_AFTER_TRANSPORT, feedback_kind="continuation")
        return AssistantTurnDecision(kind="fallback", stop_reason="transport_incomplete",
            assistant_content="Model response transport was incomplete. Bounded recovery is exhausted "
                "or no turn steps remain. No completed result was accepted; prior tool progress is retained.")

    truncated = decide_truncated_model_output(turn_state=turn_state, stop_reason=stop_reason)
    if truncated is not None:
        return truncated

    if treat_as_progress:
        return AssistantTurnDecision(
            kind="progress",
            feedback_kind="continuation",
            assistant_content=step_content,
            user_content=_step_aware_followup_nudge(
                step_policy=step_policy,
                saw_tool_result=turn_state.saw_tool_result and step_kind != "progress",
                nudge_continue=nudge_continue,
                nudge_after_tool_result=nudge_after_tool_result,
            ),
        )

    if is_recoverable_thinking_stop and turn_state.can_retry_recoverable_thinking():
        turn_state.record_recoverable_thinking_retry()
        progress_content = (
            "Model hit max_tokens during thinking; requesting the next step."
            if stop_reason == "max_tokens"
            else "Model returned pause_turn; requesting the next step."
        )
        return AssistantTurnDecision(
            kind="progress",
            assistant_content=progress_content,
            user_content=(
                resume_after_pause
                if stop_reason == "pause_turn"
                else resume_after_max_tokens
            ),
            runtime_event_category="recovery",
            feedback_kind="continuation",
        )

    if is_empty and turn_state.can_retry_empty_response():
        turn_state.record_empty_response_retry()
        retry_nudge = (
            EMPTY_VERIFY_FOLLOWUP
            if step_policy is not None and step_policy.phase == "verify"
            else (
                EMPTY_WIDENING_FOLLOWUP
                if step_policy is not None and step_policy.allow_widening
                else (
                    nudge_after_empty_response
                    if turn_state.saw_tool_result
                    else nudge_after_empty_no_tools
                )
            )
        )
        return AssistantTurnDecision(
            kind="retry",
            user_content=retry_nudge,
            feedback_kind="continuation",
        )

    if is_empty:
        late_verify = bool(
            step_policy is not None
            and step_policy.phase == "verify"
            and turn_state.verification_state.requires_explicit_final
        )
        widen_ready = bool(step_policy is not None and step_policy.allow_widening)
        diagnostics_suffix = format_diagnostics(
            stop_reason,
            block_types,
            ignored_block_types,
        )
        if turn_state.saw_tool_result:
            fallback = (
                "Model returned an empty response after tool execution and the turn "
                "was stopped. There were "
                f"{turn_state.tool_error_count} tool error(s); retry, adjust the "
                f"command, or choose a different approach.{diagnostics_suffix}"
                if turn_state.tool_error_count > 0
                else "Model returned an empty response after tool execution and the "
                "turn was stopped. Retry or ask the model to continue the remaining "
                f"steps.{diagnostics_suffix}"
            )
        else:
            fallback = (
                "Model returned an empty response and the turn was stopped."
                f"{diagnostics_suffix}"
            )
        typed_stop_reason: TurnStopReason = "blocked"
        if late_verify and turn_state.saw_tool_result:
            typed_stop_reason = "verification_failed"
            fallback += (
                " The turn had already shifted into verification mode, so this run "
                "ended as a verification failure rather than an ordinary block."
            )
        elif widen_ready:
            typed_stop_reason = "widen_needed"
            fallback += (
                " Depth stopped paying off after repeated pressure, so a wider search "
                "or handoff is now justified."
            )
        return AssistantTurnDecision(
            kind="fallback",
            assistant_content=fallback,
            stop_reason=typed_stop_reason,
        )

    if (
        step_policy is not None
        and step_policy.phase == "verify"
        and turn_state.verification_state.requires_evidence
        and not turn_state.has_verification_evidence()
    ):
        if turn_state.verification_state.evidence_retry_count >= 1:
            return AssistantTurnDecision(
                kind="fallback",
                assistant_content=(
                    "Verification guard stopped the turn: the final answer still "
                    "has no successful tool evidence after one clarification. "
                    "No successful delivery is inferred; the existing tool results are retained."
                ),
                stop_reason="verification_failed",
            )
        # Do not reset after another read: that would allow the same guard loop
        # to consume the remaining budget indefinitely. One retry per activation.
        turn_state.verification_state.evidence_retry_count += 1
        return AssistantTurnDecision(
            kind="progress",
            assistant_content=(
                "Verification guard: final answer withheld until it obtains concrete "
                "evidence from this run (clarification 1/1)."
            ),
            user_content=build_verification_evidence_nudge(
                turn_state.verification_state.evidence_summary
                or turn_state.latest_tool_result_summary
            ),
            runtime_event_category="guard",
        )

    if (step_policy is not None and step_policy.phase == "verify"
            and turn_state.verification_state.requires_evidence
            and not _content_mentions_evidence(step_content, turn_state.verification_state.evidence_summary)):
        # Wording is presentation, never an acceptance criterion. Attach the
        # observed receipt without spending another model call or discarding a
        # completed result. Runtime's structured acceptance gate still applies.
        step_content += "\n\n本轮工具记录：" + turn_state.verification_state.evidence_summary

    return AssistantTurnDecision(
        kind="final",
        assistant_content=step_content,
        protect_final_answer=True,
        stop_reason="done",
    )


def decide_tool_turn(
    *,
    tool_name: str,
    result_output: str,
    await_user: bool,
    runtime_stop_reason: str | None = None,
    result_ok: bool = True,
    ambiguous_side_effect: bool = False,
) -> ToolTurnDecision:
    """Keep tool-result ask-user handling on the same typed decision surface."""

    if ambiguous_side_effect:
        return ToolTurnDecision(
            kind="stop", stop_reason="blocked",
            assistant_content=result_output + "\n\nOperation outcome is uncertain; automatic replay stopped. "
                              "Inspect the existing output or remote status before another attempt.",
            progress_summary=f"unresolved side effects after {tool_name}; task remains incomplete",
        )

    if runtime_stop_reason == "consultation_input_exhausted":
        return ToolTurnDecision(
            kind="stop", assistant_content=result_output,
            stop_reason="consultation_input_exhausted",
            progress_summary="consultation input correction exhausted; task remains incomplete",
        )
    if runtime_stop_reason == "tool_input_exhausted" and not result_ok:
        return ToolTurnDecision(
            kind="stop", assistant_content=result_output, stop_reason="tool_input_exhausted",
            progress_summary=f"unchanged invalid arguments for {tool_name}; task remains incomplete",
        )
    if await_user:
        return ToolTurnDecision(
            kind="await_user",
            assistant_content=result_output,
            stop_reason="await_user",
            progress_summary=f"awaiting user after {tool_name}",
        )
    if result_ok and tool_name == "submit_candidate" and runtime_stop_reason == "candidate_submitted":
        return ToolTurnDecision(
            kind="stop", stop_reason="candidate_submitted",
            assistant_content="Candidate submitted for Runtime verification; final delivery is still pending.",
            progress_summary="primary handed off its candidate; Runtime acceptance remains required",
        )
    if result_ok and tool_name == "submit_builder_candidate" and runtime_stop_reason == "builder_submitted":
        return ToolTurnDecision(
            kind="stop", stop_reason="builder_submitted",
            assistant_content="Builder candidate submitted; patch export, parent acceptance and final verification remain pending.",
            progress_summary="Builder handed off its isolated candidate for parent review",
        )
    if result_ok and tool_name == "submit_consultation" and runtime_stop_reason == "consultation_submitted":
        return ToolTurnDecision(
            kind="stop", stop_reason="consultation_submitted",
            assistant_content="Consultation report submitted; Runtime verification and parent delivery remain pending.",
            progress_summary="helper handed off its report; Runtime verification remains required",
        )
    return ToolTurnDecision(
        kind="continue",
        progress_summary=f"processed tool result from {tool_name}",
    )
