"""Risk-adaptive verification controller.

This module applies engineering cybernetics to verification planning:

  SENSE:   changed files, task intent, constraints, historical failures
  CONTROL: risk score with proportional safety margins
  ACT:     select smoke / targeted / full verification commands
  FEEDBACK: update risk from the latest verification outcome

The controller deliberately produces a plan instead of executing commands.
Execution remains owned by the caller so the agent loop can respect permissions,
workspace policy, and user intent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from minicode.task_object import ConstraintType, TaskObject


class VerificationRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class VerificationMode(str, Enum):
    NONE = "none"
    SMOKE = "smoke"
    TARGETED = "targeted"
    FULL = "full"


@dataclass
class VerificationSignal:
    """Observed verification inputs for a task or change set."""

    changed_files: list[str] = field(default_factory=list)
    intent_type: str = ""
    action_type: str = ""
    requires_tests: bool = False
    recent_failures: int = 0
    previous_verification_failed: bool = False
    coverage_sensitive: bool = False
    user_requested_full: bool = False


@dataclass
class VerificationPlan:
    """Controller output: selected verification level and commands."""

    risk: VerificationRisk
    mode: VerificationMode
    score: float
    commands: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    @property
    def should_run(self) -> bool:
        return self.mode != VerificationMode.NONE and bool(self.commands)

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk.value,
            "mode": self.mode.value,
            "score": round(self.score, 3),
            "commands": list(self.commands),
            "reasons": list(self.reasons),
            "changed_files": list(self.changed_files),
            "should_run": self.should_run,
        }


class VerificationController:
    """Select verification scope from risk signals.

    The controller uses bounded scoring rather than a fixed rule table. This
    keeps the behavior explainable while allowing additional feedback signals
    to be added later.
    """

    CORE_MODULE_HINTS = (
        "agent_loop",
        "context_cybernetics",
        "context_compactor",
        "cost_control",
        "feedback_controller",
        "pipeline_engine",
        "self_healing_engine",
        "stability_monitor",
        "tooling",
    )

    def plan_for_task(self, task: TaskObject) -> VerificationPlan:
        signal = VerificationSignal(
            changed_files=list(task.relevant_files),
            intent_type=task.parsed_intent.intent_type.value if task.parsed_intent else "",
            action_type=task.parsed_intent.action_type.value if task.parsed_intent else "",
            requires_tests=any(c.type == ConstraintType.TEST_REQUIRED for c in task.constraints),
            coverage_sensitive=any("coverage" in str(c.reason).lower() for c in task.constraints),
            user_requested_full=any("full" in str(c.reason).lower() for c in task.constraints),
        )
        return self.plan(signal)

    def plan(self, signal: VerificationSignal) -> VerificationPlan:
        files = self._normalize_files(signal.changed_files)
        score = 0.0
        reasons: list[str] = []

        if not files and signal.intent_type in {"question", "chat", "explain", "search"}:
            return VerificationPlan(
                risk=VerificationRisk.LOW,
                mode=VerificationMode.NONE,
                score=0.0,
                reasons=["read-only or conversational task"],
                changed_files=[],
            )

        if signal.requires_tests:
            score += 0.25
            reasons.append("task requires tests")

        if signal.intent_type in {"code", "debug", "refactor", "test"}:
            score += 0.20
            reasons.append(f"code-related intent: {signal.intent_type}")

        if signal.action_type in {"create", "update", "delete"}:
            score += 0.15
            reasons.append(f"write action: {signal.action_type}")

        if signal.previous_verification_failed:
            score += 0.25
            reasons.append("previous verification failed")

        if signal.recent_failures > 0:
            score += min(0.20, 0.05 * signal.recent_failures)
            reasons.append(f"recent failures: {signal.recent_failures}")

        if signal.coverage_sensitive:
            score += 0.10
            reasons.append("coverage-sensitive change")

        if signal.user_requested_full:
            score += 0.30
            reasons.append("full verification requested")

        for path in files:
            p = Path(path)
            lower = path.replace("\\", "/").lower()
            suffix = p.suffix.lower()
            name = p.name.lower()

            if suffix == ".py":
                score += 0.08
                reasons.append(f"python file: {path}")
            elif suffix in {".md", ".txt", ".rst"}:
                score += 0.02
                reasons.append(f"documentation file: {path}")
            elif suffix in {".toml", ".yaml", ".yml", ".json"}:
                score += 0.10
                reasons.append(f"configuration file: {path}")

            if lower.startswith("tests/") or "/tests/" in lower or name.startswith("test_"):
                score += 0.08
                reasons.append(f"test file changed: {path}")

            if lower.startswith("minicode/") and any(hint in name for hint in self.CORE_MODULE_HINTS):
                score += 0.18
                reasons.append(f"core control module: {path}")

            if lower.endswith("__init__.py") or "pyproject.toml" in lower:
                score += 0.15
                reasons.append(f"packaging/import surface: {path}")

        score = min(score, 1.0)
        risk = self._risk_for_score(score)
        mode = self._mode_for_risk(risk, files)
        commands = self._commands_for(mode, files)

        return VerificationPlan(
            risk=risk,
            mode=mode,
            score=score,
            commands=commands,
            reasons=self._dedupe(reasons) or ["low-risk change"],
            changed_files=files,
        )

    def update_from_result(self, plan: VerificationPlan, *, passed: bool) -> VerificationSignal:
        """Convert the latest result into feedback for the next planning cycle."""

        return VerificationSignal(
            changed_files=plan.changed_files,
            previous_verification_failed=not passed,
            recent_failures=0 if passed else 1,
            requires_tests=plan.mode in {VerificationMode.TARGETED, VerificationMode.FULL},
        )

    def _normalize_files(self, files: list[str]) -> list[str]:
        normalized: list[str] = []
        for file in files:
            if not file:
                continue
            item = str(file).replace("\\", "/").strip()
            if item and item not in normalized:
                normalized.append(item)
        return normalized

    def _risk_for_score(self, score: float) -> VerificationRisk:
        if score >= 0.80:
            return VerificationRisk.CRITICAL
        if score >= 0.55:
            return VerificationRisk.HIGH
        if score >= 0.25:
            return VerificationRisk.MEDIUM
        return VerificationRisk.LOW

    def _mode_for_risk(self, risk: VerificationRisk, files: list[str]) -> VerificationMode:
        if not files and risk == VerificationRisk.LOW:
            return VerificationMode.NONE
        if risk == VerificationRisk.CRITICAL:
            return VerificationMode.FULL
        if risk == VerificationRisk.HIGH:
            return VerificationMode.TARGETED
        if risk == VerificationRisk.MEDIUM:
            return VerificationMode.TARGETED
        if any(Path(f).suffix.lower() == ".py" for f in files):
            return VerificationMode.SMOKE
        return VerificationMode.NONE

    def _commands_for(self, mode: VerificationMode, files: list[str]) -> list[str]:
        if mode == VerificationMode.NONE:
            return []
        if mode == VerificationMode.FULL:
            return ["pytest -q"]

        test_files = [f for f in files if self._is_test_file(f)]
        py_files = [f for f in files if Path(f).suffix.lower() == ".py"]

        if mode == VerificationMode.SMOKE:
            targets = test_files[:3] or ["tests/test_agent_loop.py"]
            return [f"pytest {' '.join(targets)} -q"]

        targets = test_files or self._infer_test_targets(py_files)
        if not targets:
            targets = ["tests"]
        command = f"pytest {' '.join(targets[:6])} -q"
        if len(targets) > 6:
            return [command, "pytest -q"]
        return [command]

    def _infer_test_targets(self, py_files: list[str]) -> list[str]:
        targets: list[str] = []
        for file in py_files:
            p = Path(file)
            if not file.startswith("minicode/"):
                continue
            stem = p.stem
            direct = f"tests/test_{stem}.py"
            if direct not in targets:
                targets.append(direct)

            if stem in {"context_cybernetics", "context_compactor", "cost_control"}:
                for extra in (
                    "tests/test_context_cybernetics.py",
                    "tests/test_context_compactor.py",
                    "tests/test_cost_control.py",
                ):
                    if extra not in targets:
                        targets.append(extra)
        return targets

    def _is_test_file(self, file: str) -> bool:
        name = Path(file).name.lower()
        lower = file.lower()
        return name.startswith("test_") or lower.startswith("tests/") or "/tests/" in lower

    def _dedupe(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

