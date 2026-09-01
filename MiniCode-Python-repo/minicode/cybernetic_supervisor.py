"""Unified cybernetic supervisor dashboard.

The supervisor does not replace individual controllers. It aggregates their
outputs into a single health/risk summary so runtime code can log, display, or
act on the combined control state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minicode.config import MINI_CODE_DIR


SUPERVISOR_STATE_PATH = MINI_CODE_DIR / "cybernetic_supervisor.json"


class SupervisorRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ControlSnapshot:
    name: str
    health: float = 1.0
    risk: float = 0.0
    action: str = "continue"
    reasons: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "health": round(self.health, 3),
            "risk": round(self.risk, 3),
            "action": self.action,
            "reasons": list(self.reasons),
            "raw": dict(self.raw),
        }


@dataclass
class SupervisorReport:
    overall_health: float
    risk_level: SupervisorRisk
    snapshots: list[ControlSnapshot]
    recommended_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_health": round(self.overall_health, 3),
            "risk_level": self.risk_level.value,
            "snapshots": [s.to_dict() for s in self.snapshots],
            "recommended_actions": list(self.recommended_actions),
        }

    def format_summary(self) -> str:
        lines = [
            "Cybernetic Supervisor",
            f"  overall_health: {self.overall_health:.2f}",
            f"  risk_level: {self.risk_level.value}",
        ]
        if self.recommended_actions:
            lines.append("  actions:")
            for action in self.recommended_actions[:5]:
                lines.append(f"    - {action}")
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SupervisorReport":
        snapshots = [
            ControlSnapshot(
                name=str(item.get("name", "")),
                health=float(item.get("health", 1.0) or 0.0),
                risk=float(item.get("risk", 0.0) or 0.0),
                action=str(item.get("action", "continue")),
                reasons=[str(r) for r in item.get("reasons", [])],
                raw=item.get("raw", {}) if isinstance(item.get("raw"), dict) else {},
            )
            for item in data.get("snapshots", [])
            if isinstance(item, dict)
        ]
        return cls(
            overall_health=float(data.get("overall_health", 1.0) or 0.0),
            risk_level=SupervisorRisk(str(data.get("risk_level", "low"))),
            snapshots=snapshots,
            recommended_actions=[str(a) for a in data.get("recommended_actions", [])],
        )


class CyberneticSupervisor:
    """Aggregate controller outputs into a runtime health report."""

    def report(self, snapshots: list[ControlSnapshot]) -> SupervisorReport:
        if not snapshots:
            return SupervisorReport(
                overall_health=1.0,
                risk_level=SupervisorRisk.LOW,
                snapshots=[],
                recommended_actions=["continue current execution"],
            )

        health = sum(max(0.0, min(1.0, s.health)) for s in snapshots) / len(snapshots)
        max_risk = max(max(0.0, min(1.0, s.risk)) for s in snapshots)
        risk_level = self._risk_level(max_risk, health)
        actions = self._recommended_actions(snapshots, risk_level)
        return SupervisorReport(
            overall_health=health,
            risk_level=risk_level,
            snapshots=snapshots,
            recommended_actions=actions,
        )

    def snapshot_from_context(self, stats: dict[str, Any] | None) -> ControlSnapshot:
        stats = stats or {}
        sensor = stats.get("sensor", {}) if isinstance(stats.get("sensor"), dict) else {}
        predictor = stats.get("predictor", {}) if isinstance(stats.get("predictor"), dict) else {}
        usage = float(sensor.get("current_usage", 0.0) or 0.0)
        urgency = float(predictor.get("urgency", 0.0) or 0.0)
        risk = max(usage, urgency)
        action = "compact" if risk >= 0.80 else "monitor"
        return ControlSnapshot(
            name="context",
            health=1.0 - min(1.0, risk),
            risk=risk,
            action=action,
            reasons=[f"usage={usage:.2f}", f"urgency={urgency:.2f}"],
            raw=stats,
        )

    def snapshot_from_cost(self, stats: dict[str, Any] | None) -> ControlSnapshot:
        stats = stats or {}
        sensor = stats.get("sensor", {}) if isinstance(stats.get("sensor"), dict) else {}
        adjustment = stats.get("adjustment", {}) if isinstance(stats.get("adjustment"), dict) else {}
        cost_per_min = float(sensor.get("cost_per_min", 0.0) or 0.0)
        multiplier = float(adjustment.get("budget_mult", 1.0) or 1.0)
        risk = min(1.0, cost_per_min)
        action = "tighten_budget" if multiplier < 0.8 or risk >= 0.7 else "monitor"
        return ControlSnapshot(
            name="cost",
            health=1.0 - risk,
            risk=risk,
            action=action,
            reasons=[f"cost_per_min={cost_per_min:.3f}", f"budget_mult={multiplier:.2f}"],
            raw=stats,
        )

    def snapshot_from_decision(self, name: str, data: dict[str, Any] | None) -> ControlSnapshot:
        data = data or {}
        action = str(data.get("action") or data.get("mode") or "continue")
        risk = self._risk_from_decision(data)
        health = float(data.get("health_score", 1.0 - risk) or 0.0)
        reasons = data.get("reasons", [])
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        return ControlSnapshot(
            name=name,
            health=max(0.0, min(1.0, health)),
            risk=risk,
            action=action,
            reasons=[str(r) for r in reasons],
            raw=data,
        )

    def snapshot_from_tool_decision(self, data: dict[str, Any] | None) -> ControlSnapshot:
        data = data or {}
        multiplier = float(data.get("concurrency_multiplier", 1.0) or 1.0)
        cooldown = float(data.get("cooldown_seconds", 0.0) or 0.0)
        backoff = float(data.get("retry_backoff_multiplier", 1.0) or 1.0)
        risk = max(0.0, min(1.0, (1.0 - multiplier) + min(0.5, cooldown / 4.0)))
        action = "reduce_parallelism" if multiplier < 0.75 else "monitor"
        if backoff > 1.5:
            action = "increase_retry_backoff"
        reasons = data.get("reasons", [])
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        return ControlSnapshot(
            name="tool_scheduling",
            health=1.0 - risk,
            risk=risk,
            action=action,
            reasons=[str(r) for r in reasons],
            raw=data,
        )

    def _risk_from_decision(self, data: dict[str, Any]) -> float:
        if "stall_score" in data:
            return float(data.get("stall_score") or 0.0)
        risk_value = str(data.get("risk", "")).lower()
        if risk_value == "critical":
            return 1.0
        if risk_value == "high":
            return 0.75
        if risk_value == "medium":
            return 0.45
        if risk_value == "low":
            return 0.15
        mode = str(data.get("mode", "")).lower()
        if mode in {"none", "continue", "standard"}:
            return 0.10
        if mode in {"summary", "smoke", "targeted"}:
            return 0.35
        if mode in {"full", "strong"}:
            return 0.65
        return 0.0

    def _risk_level(self, max_risk: float, health: float) -> SupervisorRisk:
        if max_risk >= 0.90 or health < 0.25:
            return SupervisorRisk.CRITICAL
        if max_risk >= 0.70 or health < 0.45:
            return SupervisorRisk.HIGH
        if max_risk >= 0.40 or health < 0.70:
            return SupervisorRisk.MEDIUM
        return SupervisorRisk.LOW

    def _recommended_actions(
        self,
        snapshots: list[ControlSnapshot],
        risk_level: SupervisorRisk,
    ) -> list[str]:
        actions: list[str] = []
        for snap in sorted(snapshots, key=lambda s: s.risk, reverse=True):
            if snap.risk >= 0.40 or snap.action not in {"continue", "monitor", "standard"}:
                actions.append(f"{snap.name}: {snap.action}")
        if not actions:
            actions.append("continue current execution")
        if risk_level in {SupervisorRisk.HIGH, SupervisorRisk.CRITICAL}:
            actions.append("summarize state before further expansion")
        return self._dedupe(actions)

    def _dedupe(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out


def save_supervisor_report(report: SupervisorReport) -> None:
    """Persist the latest supervisor report for slash-command diagnostics."""
    SUPERVISOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUPERVISOR_STATE_PATH.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_supervisor_report() -> SupervisorReport | None:
    """Load the latest persisted supervisor report, if present."""
    if not SUPERVISOR_STATE_PATH.exists():
        return None
    try:
        data = json.loads(SUPERVISOR_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return SupervisorReport.from_dict(data)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
