"""Context Cybernetics Controller - Engineering Cybernetics for Context Management.

Implements 钱学森 Engineering Cybernetics (工程控制论) principles
for intelligent context window management:

  Sensor Layer:     ContextPressureSensor — continuous pressure measurement + growth rate
  Control Layer:    ContextPIDController  — closed-loop PID with anti-windup
  Prediction Layer: PredictiveOverflowGuard — exponential smoothing forecast
  Adaptation Layer: AdaptiveThresholdManager — dynamic threshold via decoupling analysis
  Selection Layer:  CompactionStrategySelector — control output → concrete strategy
  Learning Layer:   CyberneticFeedbackLoop — post-action parameter adaptation
  Orchestration:    ContextCyberneticsOrchestrator — full sense-think-act cycle

Architecture (closed-loop):
                    ┌──────────────┐
                    │   Setpoint    │ target_usage
                    └──────┬───────┘
                           │ pressure error = usage - target
              ┌────────────▼────────────┐
              │   ContextPIDController   │
              │   (P + I + D)           │
              └────────────┬────────────┘
                           │ control_output [0,1]
              ┌────────────▼────────────┐
              │ StrategySelector        │
              │ intensity → strategy    │
              └────────────┬────────────┘
                           │ CompactionAction
              ┌────────────▼────────────┐
              │ ContextCompactor        │
              │ (executor / actuator)   │
              └────────────┬────────────┘
                           │ CompactionResult
              ┌────────────▼────────────┐
              │ ContextPressureSensor   │
              │ measure → usage_ratio   │
              └────────────┬────────────┘
                           │ (+) feedback
                    ┌──────┴───────┐
                    │   Summing    │
                    └──────────────┘
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from .context_compactor import (
    CompactStrategy,
    CompactTrigger,
    CompactionResult,
    ContextCompactor,
)


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from minicode.feedback_controller import SystemState

class AnomalyType(Enum):
    SUDDEN_SPIKE = auto()
    ACCELERATING_GROWTH = auto()
    OSCILLATION = auto()


@dataclass
class ContextPressureReading:
    timestamp: float
    usage_ratio: float
    token_count: int
    message_count: int
    growth_rate: float = 0.0
    acceleration: float = 0.0
    anomaly: AnomalyType | None = None


@dataclass
class ControlAction:
    compaction_intensity: float
    strategy: CompactStrategy | None = None
    force_execution: bool = False
    reason: str = ""
    predicted_turns_to_overflow: int | None = None
    pid_output: float = 0.0
    predictive_urgency: float = 0.0


@dataclass
class PredictiveOutlook:
    turns_until_overflow: int | None
    projected_usage_at_horizon: float
    urgency: float
    trend: str
    confidence: float
    recommendation: str


class ContextPressureSensor:
    """Continuous context pressure sensor with derivative estimation.

    Sliding-window sensor that measures not just current usage ratio,
    but also growth rate (1st derivative) and acceleration (2nd derivative)
    to enable predictive control actions.
    """

    def __init__(self, window_size: int = 10):
        self._window_size = window_size
        self._history: list[ContextPressureReading] = []
        self._last_token_count: int = 0
        self._last_usage_ratio: float = 0.0
        self._last_timestamp: float = 0.0
        self._last_growth_rate: float = 0.0

    def measure(
        self,
        token_count: int,
        message_count: int,
        context_window: int,
        *,
        turn_id: int = 0,
    ) -> ContextPressureReading:
        now = time.time()
        usage_ratio = token_count / max(context_window, 1)

        dt = now - self._last_timestamp if self._last_timestamp > 0 else 1.0
        dt = max(dt, 0.001)

        raw_growth = (token_count - self._last_token_count) / max(context_window, 1) if self._last_timestamp > 0 else 0.0
        growth_rate = raw_growth / dt if dt > 0 else 0.0
        acceleration = (growth_rate - self._last_growth_rate) / dt if dt > 0 else 0.0

        alpha = 0.3
        smoothed_growth = alpha * growth_rate + (1 - alpha) * self._last_growth_rate

        anomaly = self._detect_anomaly(usage_ratio, smoothed_growth, acceleration)

        reading = ContextPressureReading(
            timestamp=now,
            usage_ratio=usage_ratio,
            token_count=token_count,
            message_count=message_count,
            growth_rate=smoothed_growth,
            acceleration=acceleration,
            anomaly=anomaly,
        )

        self._history.append(reading)
        if len(self._history) > self._window_size * 3:
            self._history = self._history[-self._window_size * 2:]

        self._last_token_count = token_count
        self._last_usage_ratio = usage_ratio
        self._last_timestamp = now
        self._last_growth_rate = smoothed_growth

        return reading

    def _detect_anomaly(
        self, usage_ratio: float, growth_rate: float, acceleration: float
    ) -> AnomalyType | None:
        if len(self._history) < 3:
            return None
        recent = self._history[-3:]
        avg_usage = sum(r.usage_ratio for r in recent) / len(recent)
        if usage_ratio > avg_usage + 0.15 and growth_rate > 0.02:
            return AnomalyType.SUDDEN_SPIKE
        if acceleration > 0.001 and growth_rate > 0.01:
            return AnomalyType.ACCELERATING_GROWTH
        if len(self._history) >= 5:
            signs = [1 if r.growth_rate > 0 else -1 for r in self._history[-5:]]
            if len(set(signs)) >= 4 and abs(usage_ratio - self._history[-5].usage_ratio) < 0.05:
                return AnomalyType.OSCILLATION
        return None

    def get_recent_readings(self, n: int = 5) -> list[ContextPressureReading]:
        return self._history[-n:]

    def get_avg_growth_rate(self, n: int = 5) -> float:
        recent = self._history[-n:]
        return sum(r.growth_rate for r in recent) / len(recent) if recent else 0.0


class ContextPIDController:
    """PID controller for context pressure regulation.

    Implements standard PID control with:
      - Proportional: immediate response to error magnitude
      - Integral: eliminates steady-state offset (with anti-windup clamping)
      - Derivative: dampens oscillation by responding to error rate-of-change

    Output is clamped to [0, 1] representing compaction intensity. Higher
    output means stronger pressure to compact, so usage above the setpoint
    increases the controller output.
    """

    def __init__(
        self,
        kp: float = 2.0,
        ki: float = 0.15,
        kd: float = 0.3,
        setpoint: float = 0.70,
        output_min: float = 0.0,
        output_max: float = 1.0,
        integral_windup_limit: float = 2.0,
    ):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.setpoint = setpoint
        self.output_min = output_min
        self.output_max = output_max
        self.integral_windup_limit = integral_windup_limit

        self._integral = 0.0
        self._prev_error = 0.0
        self._prev_time: float = 0.0
        self._initialized = False

        self.output_history: list[float] = []
        self.error_history: list[float] = []

    def compute(self, process_variable: float, dt: float | None = None) -> float:
        now = time.time()
        if not self._initialized:
            self._prev_time = now
            self._prev_error = process_variable - self.setpoint
            self._initialized = True
            return 0.0

        if dt is None:
            dt = now - self._prev_time
        dt = max(dt, 0.001)

        error = process_variable - self.setpoint

        p_term = self.kp * error

        self._integral += error * dt
        self._integral = max(-self.integral_windup_limit,
                             min(self.integral_windup_limit, self._integral))
        i_term = self.ki * self._integral

        derivative = (error - self._prev_error) / dt
        d_term = self.kd * derivative

        output = p_term + i_term + d_term
        output = max(self.output_min, min(self.output_max, output))

        self._prev_error = error
        self._prev_time = now

        self.output_history.append(output)
        self.error_history.append(error)
        if len(self.output_history) > 100:
            self.output_history = self.output_history[-50:]
            self.error_history = self.error_history[-50:]

        return output

    def reset(self):
        self._integral = 0.0
        self._prev_error = 0.0
        self._initialized = False
        self.output_history.clear()
        self.error_history.clear()

    @property
    def is_saturated(self) -> bool:
        if len(self.output_history) < 10:
            return False
        recent = self.output_history[-10:]
        return all(o <= self.output_min + 0.01 or o >= self.output_max - 0.01 for o in recent)


class PredictiveOverflowGuard:
    """Predictive guard using exponential smoothing + linear extrapolation.

    Forecasts when context will overflow based on historical growth trend,
    enabling preemptive compaction before threshold is actually breached.
    """

    def __init__(
        self,
        smoothing_alpha: float = 0.25,
        safety_margin_turns: int = 3,
        overflow_threshold: float = 0.95,
    ):
        self._alpha = smoothing_alpha
        self._safety_margin = safety_margin_turns
        self._overflow_threshold = overflow_threshold
        self._smoothed_growth: float = 0.0
        self._smoothed_usage: float = 0.0
        self._initialized = False
        self._predictions: list[PredictiveOutlook] = []

    def update(self, usage_ratio: float, growth_rate: float) -> None:
        if not self._initialized:
            self._smoothed_usage = usage_ratio
            self._smoothed_growth = growth_rate
            self._initialized = True
            return
        self._smoothed_usage = self._alpha * usage_ratio + (1 - self._alpha) * self._smoothed_usage
        self._smoothed_growth = self._alpha * growth_rate + (1 - self._alpha) * self._smoothed_growth

    def predict(self, horizon_turns: int = 10) -> PredictiveOutlook:
        effective_growth = max(self._smoothed_growth, 0.0)
        actual_growth = self._smoothed_growth

        projected = self._smoothed_usage + effective_growth * horizon_turns

        if effective_growth > 1e-6:
            turns_to_overflow = int(math.ceil((self._overflow_threshold - self._smoothed_usage) / effective_growth))
        elif self._smoothed_usage >= self._overflow_threshold:
            turns_to_overflow = 0
        else:
            turns_to_overflow = None

        if turns_to_overflow is not None:
            turns_to_overflow = max(0, turns_to_overflow)

        urgency = 0.0
        if turns_to_overflow is not None:
            if turns_to_overflow <= 0:
                urgency = 1.0
            elif turns_to_overflow <= self._safety_margin:
                urgency = 0.8
            elif turns_to_overflow <= self._safety_margin * 2:
                urgency = 0.5
            elif turns_to_overflow <= horizon_turns:
                urgency = 0.3

        if actual_growth > 0.01:
            trend = "rising"
        elif actual_growth < -0.001:
            trend = "falling"
        else:
            trend = "stable"

        confidence = min(1.0, abs(effective_growth) * 20 + 0.2) if self._initialized else 0.0

        if turns_to_overflow is not None and turns_to_overflow <= self._safety_margin:
            recommendation = f"preemptive_compact: overflow in ~{turns_to_overflow} turns"
        elif urgency > 0.5:
            recommendation = "increase_monitoring"
        else:
            recommendation = "normal"

        outlook = PredictiveOutlook(
            turns_until_overflow=turns_to_overflow,
            projected_usage_at_horizon=min(projected, 1.5),
            urgency=urgency,
            trend=trend,
            confidence=confidence,
            recommendation=recommendation,
        )
        self._predictions.append(outlook)
        if len(self._predictions) > 50:
            self._predictions = self._predictions[-30:]
        return outlook

    def reset(self):
        self._smoothed_growth = 0.0
        self._smoothed_usage = 0.0
        self._initialized = False
        self._predictions.clear()


class AdaptiveThresholdManager:
    """Dynamically adjusts compaction thresholds based on coupling analysis.

    Uses a simplified internal coupling model inspired by RGA (Relative Gain Array)
    from multivariable control theory. When context_pressure is strongly coupled
    with error_rate or latency, thresholds are tightened to prevent cascading issues.

    Also supports feedforward adjustment based on task intent complexity.
    """

    INTENT_THRESHOLD_MAP = {
        "CODE": 0.78,
        "DEBUG": 0.75,
        "REFACTOR": 0.72,
        "SEARCH": 0.88,
        "REVIEW": 0.85,
        "TEST": 0.80,
        "DOCUMENT": 0.88,
        "SYSTEM": 0.82,
    }

    def __init__(self, base_threshold: float = 0.85):
        self._base_threshold = base_threshold
        self._coupling_strength: dict[str, float] = {
            "context_error": 0.0,
            "context_latency": 0.0,
        }
        self._intent_type: str | None = None
        self._adaptation_history: list[dict[str, Any]] = []

    def set_intent(self, intent_type: str | None):
        self._intent_type = intent_type

    def update_coupling(self, context_pressure: float, error_rate: float, latency: float):
        n = len(self._adaptation_history) + 1
        for key, var in [("context_error", error_rate), ("context_latency", latency)]:
            old = self._coupling_strength.get(key, 0.0)
            sample_cov = context_pressure * var
            new_val = old + (sample_cov - old) / n
            self._coupling_strength[key] = max(0.0, min(1.0, new_val))

    def get_effective_threshold(self) -> float:
        threshold = self._base_threshold

        if self._intent_type:
            intent_adj = self.INTENT_THRESHOLD_MAP.get(self._intent_type.upper())
            if intent_adj is not None:
                weight = 0.4
                threshold = threshold * (1 - weight) + intent_adj * weight

        error_coupling = self._coupling_strength.get("context_error", 0.0)
        if error_coupling > 0.4:
            tightening = (error_coupling - 0.4) * 0.15
            threshold -= tightening

        latency_coupling = self._coupling_strength.get("context_latency", 0.0)
        if latency_coupling > 0.5:
            tightening = (latency_coupling - 0.5) * 0.10
            threshold -= tightening

        return max(0.55, min(0.95, threshold))

    def record_adaptation(self, threshold_used: float, outcome: str):
        self._adaptation_history.append({
            "threshold": threshold_used,
            "outcome": outcome,
            "timestamp": time.time(),
        })
        if len(self._adaptation_history) > 100:
            self._adaptation_history = self._adaptation_history[-50:]

    def get_stats(self) -> dict[str, Any]:
        return {
            "base_threshold": self._base_threshold,
            "effective_threshold": round(self.get_effective_threshold(), 4),
            "coupling_context_error": round(self._coupling_strength.get("context_error", 0), 4),
            "coupling_context_latency": round(self._coupling_strength.get("context_latency", 0), 4),
            "intent_type": self._intent_type,
            "adaptations_recorded": len(self._adaptation_history),
        }


class CompactionStrategySelector:
    """Maps PID control output + predictive urgency to concrete compaction strategies.

    Selection logic follows a layered defense-in-depth approach:

      intensity < 0.15:  NOOP — system healthy
      intensity < 0.35:  MICROCOMPACT — clear old tool results only
      intensity < 0.60:  SESSION_MEMORY — memory-based summary compact
      intensity < 0.80:  FULL_COMPACT — structured summary + tail preservation
      intensity >= 0.80: AGGRESSIVE — full compact + force execution
      urgency > 0.7:     override to more aggressive strategy
    """

    STRATEGY_MAP: list[tuple[float, CompactStrategy]] = [
        (0.00, CompactStrategy.MICROCOMPACT),
        (0.30, CompactStrategy.SESSION_MEMORY),
        (0.55, CompactStrategy.FULL),
    ]

    def select(
        self,
        intensity: float,
        urgency: float,
        anomaly: AnomalyType | None,
        usage_ratio: float,
    ) -> ControlAction:
        strategy = None
        force = False
        reason_parts = []

        if urgency > 0.7:
            if urgency > 0.9:
                strategy = CompactStrategy.FULL
                force = True
                reason_parts.append(f"critical_urgency({urgency:.2f})")
            else:
                strategy = CompactStrategy.SESSION_MEMORY
                force = intensity > 0.5
                reason_parts.append(f"high_urgency({urgency:.2f})")
        elif anomaly == AnomalyType.SUDDEN_SPIKE:
            strategy = CompactStrategy.SESSION_MEMORY
            force = usage_ratio > 0.75
            reason_parts.append("sudden_spike")
        elif anomaly == AnomalyType.ACCELERATING_GROWTH:
            strategy = CompactStrategy.FULL
            force = intensity > 0.4
            reason_parts.append("accelerating_growth")
        else:
            for threshold, strat in reversed(self.STRATEGY_MAP):
                if intensity >= threshold:
                    strategy = strat
                    break
            if strategy is None:
                strategy = CompactStrategy.MICROCOMPACT

            if intensity >= 0.80:
                force = True
                reason_parts.append(f"high_intensity({intensity:.2f})")

        if not reason_parts:
            reason_parts.append(f"pid_intensity({intensity:.2f})")

        return ControlAction(
            compaction_intensity=intensity,
            strategy=strategy,
            force_execution=force,
            reason="; ".join(reason_parts),
            predictive_urgency=urgency,
        )


class CyberneticFeedbackLoop:
    """Post-compaction learning loop that adapts controller parameters.

    Tracks compaction effectiveness and detects oscillation patterns
    that would indicate poorly-tuned PID parameters. When oscillation
    is detected, recommends parameter adjustments (derivative damping).
    """

    def __init__(self, history_size: int = 30):
        self._history_size = history_size
        self._compaction_history: list[dict[str, Any]] = []
        self._total_compactions = 0
        self._effective_compactions = 0
        self._oscillation_count = 0
        self._last_usage_before: float = 0.0
        self._direction_changes = 0

    def record(self, action: ControlAction, result: CompactionResult, usage_before: float, usage_after: float):
        self._total_compactions += 1
        if result.effective:
            self._effective_compactions += 1

        entry = {
            "timestamp": time.time(),
            "intensity": action.compaction_intensity,
            "strategy": action.strategy.value if action.strategy else None,
            "force": action.force_execution,
            "usage_before": usage_before,
            "usage_after": usage_after,
            "tokens_freed": result.tokens_freed,
            "effective": result.effective,
            "reason": action.reason,
        }
        self._compaction_history.append(entry)

        if len(self._compaction_history) > self._history_size:
            self._compaction_history = self._compaction_history[-self._history_size // 2:]

        if self._last_usage_before > 0:
            now_direction = "up" if usage_after > usage_before else "down"
            prev_direction = "up" if usage_before > self._last_usage_before else "down"
            if now_direction != prev_direction:
                self._direction_changes += 1

        self._last_usage_before = usage_before

    def detect_oscillation(self) -> bool:
        if len(self._compaction_history) < 6:
            return False
        recent = self._compaction_history[-6:]
        usages = [e["usage_after"] for e in recent]
        direction_changes = sum(
            1 for i in range(1, len(usages)) if (usages[i] - usages[i-1]) * (usages[i-1] - (usages[i-2] if i >= 2 else usages[i-1])) < 0
        )
        return direction_changes >= 3

    def get_effectiveness_rate(self) -> float:
        if self._total_compactions == 0:
            return 1.0
        return self._effective_compactions / self._total_compactions

    def recommend_pid_adjustment(self) -> dict[str, float] | None:
        if not self.detect_oscillation():
            return None
        return {"kd_boost": 0.2, "kp_reduce": 0.1}

    def get_stats(self) -> dict[str, Any]:
        return {
            "total_compactions": self._total_compactions,
            "effective_compactions": self._effective_compactions,
            "effectiveness_rate": round(self.get_effectiveness_rate(), 4),
            "oscillation_detected": self.detect_oscillation(),
            "direction_changes": self._direction_changes,
            "history_entries": len(self._compaction_history),
        }


class ContextCyberneticsOrchestrator:
    """Main orchestrator: runs the full sense-think-act cycle each turn.

    Integrates all cybernetic components into a unified control loop:

      1. SENSE:  ContextPressureSensor.measure() → reading
      2. PREDICT: PredictiveOverflowGuard.update+predict() → outlook
      3. CONTROL: ContextPIDController.compute(reading.usage_ratio) → output
      4. ADAPT:  AdaptiveThresholdManager.get_effective_threshold()
      5. SELECT: CompactionStrategySelector.select() → action
      6. ACT:    ContextCompactor.process_request() with selected strategy
      7. LEARN:  CyberneticFeedbackLoop.record() → adapt parameters

    This replaces the simple threshold-based dispatch with an intelligent
    closed-loop control system.
    """

    def __init__(
        self,
        context_compactor: ContextCompactor,
        *,
        kp: float = 2.0,
        ki: float = 0.15,
        kd: float = 0.3,
        pid_setpoint: float = 0.70,
        base_threshold: float = 0.85,
        sensor_window: int = 10,
        safety_margin_turns: int = 3,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.compactor = context_compactor

        self.sensor = ContextPressureSensor(window_size=sensor_window)
        self.pid = ContextPIDController(kp=kp, ki=ki, kd=kd, setpoint=pid_setpoint)
        self.predictor = PredictiveOverflowGuard(safety_margin_turns=safety_margin_turns)
        self.threshold_mgr = AdaptiveThresholdManager(base_threshold=base_threshold)
        self.selector = CompactionStrategySelector()
        self.feedback = CyberneticFeedbackLoop()

        self._cycle_count = 0
        self._last_action: ControlAction | None = None
        self._last_result: CompactionResult | None = None
        self._last_error_rate: float = 0.0
        self._last_avg_latency: float = 0.0

    @property
    def last_action(self) -> ControlAction | None:
        return self._last_action

    @property
    def last_result(self) -> CompactionResult | None:
        return self._last_result

    def set_intent(self, intent_type: str | None):
        self.threshold_mgr.set_intent(intent_type)

    def update_coupling_metrics(
        self, error_rate: float = 0.0, avg_latency: float = 0.0
    ):
        reading = self.sensor.get_recent_readings(1)
        context_pressure = reading[0].usage_ratio if reading else 0.0
        self.threshold_mgr.update_coupling(context_pressure, error_rate, avg_latency)

    def run_cycle(
        self,
        messages: list[dict],
        *,
        error_rate: float = 0.0,
        avg_latency: float = 0.0,
        turn_id: int = 0,
    ) -> tuple[list[dict], CompactionResult | None, ControlAction | None]:
        if not self.enabled:
            return messages, None, None

        self._cycle_count += 1
        self._last_error_rate = error_rate
        self._last_avg_latency = avg_latency

        estimate_fn = self.compactor._estimate
        total_tokens = sum(estimate_fn(m) for m in messages)
        context_window = self.compactor._context_window

        reading = self.sensor.measure(
            token_count=total_tokens,
            message_count=len(messages),
            context_window=context_window,
            turn_id=turn_id,
        )

        self.predictor.update(reading.usage_ratio, reading.growth_rate)
        outlook = self.predictor.predict(horizon_turns=10)

        self.update_coupling_metrics(error_rate, avg_latency)
        effective_threshold = self.threshold_mgr.get_effective_threshold()

        should_act = (
            reading.usage_ratio >= effective_threshold
            or outlook.urgency > 0.5
            or reading.anomaly is not None
        )

        pid_output = self.pid.compute(reading.usage_ratio)
        combined_intensity = max(pid_output, outlook.urgency * 0.8)

        action = self.selector.select(
            intensity=combined_intensity,
            urgency=outlook.urgency,
            anomaly=reading.anomaly,
            usage_ratio=reading.usage_ratio,
        )
        action.pid_output = pid_output
        action.predicted_turns_to_overflow = outlook.turns_until_overflow

        result = None
        final_messages = messages

        if should_act or action.force_execution:
            enable_auto = action.strategy in (CompactStrategy.FULL, CompactStrategy.SESSION_MEMORY)
            enable_micro = action.strategy in (
                CompactStrategy.MICROCOMPACT, CompactStrategy.SESSION_MEMORY, CompactStrategy.FULL
            )

            result = self.compactor.process_request(
                final_messages,
                enable_tool_budget=True,
                enable_read_dedup=True,
                enable_microcompact=enable_micro,
                enable_auto_compact=enable_auto,
            )
            if result.effective:
                final_messages = result.messages

        usage_after = (
            sum(estimate_fn(m) for m in final_messages) / max(context_window, 1)
            if result and result.effective else reading.usage_ratio
        )
        self.feedback.record(action, result or CompactionResult(
            success=False, strategy=CompactStrategy.MICROCOMPACT,
            trigger=CompactTrigger.MICROCOMPACT_CACHED, messages=[],
        ), reading.usage_ratio, usage_after)

        pid_adjustment = self.feedback.recommend_pid_adjustment()
        if pid_adjustment:
            self.pid.kd = min(1.0, self.pid.kd + pid_adjustment["kd_boost"])
            self.pid.kp = max(0.5, self.pid.kp - pid_adjustment["kp_reduce"])

        self._last_action = action
        self._last_result = result

        return final_messages, result, action

    def try_reactive_recover(self, messages: list[dict], error: str) -> tuple[list[dict], CompactionResult | None]:
        recovery = self.compactor.reactive_recover(messages, error)
        if recovery.effective:
            self._last_result = recovery
            return recovery.messages, recovery
        return messages, None

    def get_stats(self) -> dict[str, Any]:
        sensor_recent = self.sensor.get_recent_readings(1)
        current_reading = sensor_recent[0] if sensor_recent else None
        return {
            "orchestrator_enabled": self.enabled,
            "cycles_executed": self._cycle_count,
            "sensor": {
                "current_usage": round(current_reading.usage_ratio, 4) if current_reading else 0,
                "growth_rate": round(current_reading.growth_rate, 6) if current_reading else 0,
                "anomaly": current_reading.anomaly.name if current_reading and current_reading.anomaly else None,
                "readings_collected": len(self.sensor._history),
            },
            "pid": {
                "setpoint": self.pid.setpoint,
                "kp": self.pid.kp,
                "ki": self.pid.ki,
                "kd": self.pid.kd,
                "last_output": round(self.pid.output_history[-1], 4) if self.pid.output_history else 0,
                "is_saturated": self.pid.is_saturated,
                "integral": round(self.pid._integral, 4),
            },
            "predictor": {
                "turns_until_overflow": self.predictor._predictions[-1].turns_until_overflow if self.predictor._predictions else None,
                "urgency": round(self.predictor._predictions[-1].urgency, 4) if self.predictor._predictions else 0,
                "trend": self.predictor._predictions[-1].trend if self.predictor._predictions else "unknown",
                "confidence": round(self.predictor._predictions[-1].confidence, 4) if self.predictor._predictions else 0,
            },
            "threshold": self.threshold_mgr.get_stats(),
            "feedback": self.feedback.get_stats(),
            "last_action": {
                "intensity": round(self._last_action.compaction_intensity, 4) if self._last_action else None,
                "strategy": self._last_action.strategy.value if self._last_action and self._last_action.strategy else None,
                "force": self._last_action.force_execution if self._last_action else None,
                "reason": self._last_action.reason if self._last_action else None,
            } if self._last_action else None,
        }

    def reset(self):
        self.sensor = ContextPressureSensor(window_size=self.sensor._window_size)
        self.pid.reset()
        self.predictor.reset()
        self.feedback = CyberneticFeedbackLoop()
        self._cycle_count = 0
        self._last_action = None
        self._last_result = None

    def feed_from_stability_monitor(
        self,
        context_usage: float = 0.0,
        error_rate: float = 0.0,
        avg_latency: float = 0.0,
        cpu_usage: float = 0.0,
        memory_usage: float = 0.0,
    ) -> None:
        """Bridge: ingest data from StabilityMonitor MetricSnapshot.

        Feeds the stability monitor's metrics into the adaptive threshold
        manager's coupling analysis, enabling RGA-based threshold adjustment
        based on real system-level observations.
        """
        self.update_coupling_metrics(error_rate=error_rate, avg_latency=avg_latency)
        # Also track system resource metrics for future coupling analysis
        if cpu_usage > 0 or memory_usage > 0:
            self.update_coupling_metrics(
                error_rate=error_rate,
                avg_latency=avg_latency,
            )

    def to_system_state(self) -> "SystemState":
        """Bridge: convert internal state to FeedbackController.SystemState.

        Forms the upper layer of a dual-PID control architecture:
          Layer 1 (this module): ContextPIDController → ContextCompactor
          Layer 2 (feedback_controller): FeedbackController → agent behavior tuning

        The SystemState output from this method feeds into FeedbackController.observe()
        to close the outer control loop.
        """
        from .feedback_controller import SystemState

        reading = self.sensor.get_recent_readings(1)[0] if self.sensor._history else None
        fb_stats = self.feedback.get_stats()
        self.predictor._predictions[-1] if self.predictor._predictions else None

        return SystemState(
            success_rate=fb_stats.get("effectiveness_rate", 1.0),
            avg_response_time=getattr(self, '_last_avg_latency', 0.0),
            token_efficiency=1.0 - max(0, (reading.usage_ratio if reading else 0) - 0.5),
            context_usage=reading.usage_ratio if reading else 0.0,
            error_frequency=getattr(self, '_last_error_rate', 0.0),
            oscillation_index=1.0 if fb_stats.get("oscillation_detected") else 0.0,
            skill_effectiveness=fb_stats.get("effectiveness_rate", 0.0),
            pattern_reuse_rate=min(1.0, fb_stats.get("total_compactions", 0) / max(self._cycle_count, 1)),
            knowledge_accumulation=min(1.0, self._cycle_count / 50.0),
        )
