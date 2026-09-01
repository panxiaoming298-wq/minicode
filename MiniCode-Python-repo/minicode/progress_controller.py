"""Progress controller for long-running agent tasks.

Applies cybernetic feedback to task progress:

  SENSE: step count, completion ratio, errors, tool calls, output changes, tests
  CONTROL: determine whether the loop is healthy, stalled, or over-budget
  ACT: continue, switch strategy, narrow scope, verify, or request confirmation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProgressAction(str, Enum):
    CONTINUE = "continue"
    VERIFY = "verify"
    SWITCH_STRATEGY = "switch_strategy"
    NARROW_SCOPE = "narrow_scope"
    REQUEST_CONFIRMATION = "request_confirmation"
    STOP = "stop"


@dataclass
class ProgressSignal:
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    output_changed: bool = False
    tests_passed: bool | None = None
    elapsed_seconds: float = 0.0
    max_steps: int | None = None


@dataclass
class ProgressDecision:
    action: ProgressAction
    health_score: float
    stall_score: float
    reasons: list[str] = field(default_factory=list)
    suggested_next_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "health_score": round(self.health_score, 3),
            "stall_score": round(self.stall_score, 3),
            "reasons": list(self.reasons),
            "suggested_next_steps": list(self.suggested_next_steps),
        }


class ProgressController:
    """Decide whether execution is making healthy progress."""

    def decide(self, signal: ProgressSignal) -> ProgressDecision:
        total = max(signal.total_steps, 1)
        completion_ratio = signal.completed_steps / total
        failure_ratio = signal.failed_steps / total
        tool_error_ratio = signal.tool_errors / max(signal.tool_calls, 1)

        health = 0.35 + completion_ratio * 0.45
        health -= failure_ratio * 0.35
        health -= tool_error_ratio * 0.25
        if signal.output_changed:
            health += 0.15
        if signal.tests_passed is True:
            health += 0.15
        elif signal.tests_passed is False:
            health -= 0.20

        step_pressure = 0.0
        if signal.max_steps:
            step_pressure = min(1.0, signal.total_steps / max(signal.max_steps, 1))

        stall = 0.0
        reasons: list[str] = []
        suggestions: list[str] = []

        if completion_ratio == 0 and signal.total_steps >= 3:
            stall += 0.30
            reasons.append("no completed steps after multiple attempts")
        if signal.tool_calls >= 5 and not signal.output_changed:
            stall += 0.25
            reasons.append("many tool calls without output change")
        if tool_error_ratio >= 0.5 and signal.tool_calls >= 2:
            stall += 0.30
            reasons.append("high tool error ratio")
        if step_pressure >= 0.85:
            stall += 0.20
            reasons.append("near step budget")
        if signal.tests_passed is False:
            stall += 0.20
            reasons.append("verification failed")
        if signal.elapsed_seconds > 600 and completion_ratio < 0.3:
            stall += 0.20
            reasons.append(f"slow progress: {completion_ratio:.0%} after {signal.elapsed_seconds:.0f}s")

        health = max(0.0, min(1.0, health))
        stall = max(0.0, min(1.0, stall))

        if signal.tests_passed is True and completion_ratio >= 0.95:
            return ProgressDecision(
                ProgressAction.STOP,
                health,
                stall,
                ["task complete and verified"],
                ["finalize result"],
            )

        if stall >= 0.75:
            suggestions.extend(["summarize blocker", "ask user to choose scope or strategy"])
            return ProgressDecision(
                ProgressAction.REQUEST_CONFIRMATION,
                health,
                stall,
                reasons,
                suggestions,
            )

        if stall >= 0.50:
            suggestions.extend(["switch tool or implementation approach", "reduce parallel work"])
            return ProgressDecision(
                ProgressAction.SWITCH_STRATEGY,
                health,
                stall,
                reasons,
                suggestions,
            )

        if step_pressure >= 0.80 and completion_ratio < 0.70:
            suggestions.extend(["narrow to minimum viable subtask", "defer optional work"])
            return ProgressDecision(
                ProgressAction.NARROW_SCOPE,
                health,
                stall,
                reasons or ["step pressure is high"],
                suggestions,
            )

        if signal.output_changed and signal.tests_passed is None:
            return ProgressDecision(
                ProgressAction.VERIFY,
                health,
                stall,
                reasons or ["output changed and needs verification"],
                ["run risk-appropriate verification"],
            )

        return ProgressDecision(
            ProgressAction.CONTINUE,
            health,
            stall,
            reasons or ["progress is healthy"],
            ["continue current plan"],
        )

