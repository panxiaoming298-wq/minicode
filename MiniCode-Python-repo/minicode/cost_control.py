"""Budget PID Controller — Engineering Cybernetics for Cost Control.

Implements a closed-loop cost control system that connects the passive cost_tracker
sensor to the active ToolResultBudgetManager actuator:

  Architecture (closed-loop):
                    ┌──────────────┐
                    │  Setpoint    │ target_cost_rate
                    └──────┬───────┘
                           │ (-)
              ┌────────────▼────────────┐
              │  BudgetPIDController     │
              │  (P + I + D)             │
              └────────────┬────────────┘
                           │ budget_multiplier [0.3, 2.0]
              ┌────────────▼────────────┐
              │  BudgetActuator          │
              │  threshold = base * mult │
              │  budget   = base * mult │
              └────────────┬────────────┘
                           │ adjusted params
              ┌────────────▼────────────┐
              │  ToolResultBudgetManager  │
              │  (persist / trim results)│
              └────────────┬────────────┘
                           │ smaller context
              ┌────────────▼────────────┐
              │  API Call → fewer tokens │
              └────────────┬────────────┘
                           │ (+)
              ┌────────────▼────────────┐
              │  CostTracker (Sensor)    │
              │  cost_rate = $/min       │
              └─────────────────────────┘

Control Logic:
  - cost_rate > setpoint  → multiplier < 1.0  → tighter budget  → save tokens
  - cost_rate < setpoint  → multiplier > 1.0  → looser budget   → richer context
  - integral term prevents steady-state drift (budget creep)
  - derivative term dampens oscillation during burst spending
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


class SpendingTrend(Enum):
    STABLE = auto()
    ACCELERATING = auto()
    DECELERATING = auto()
    BURST = auto()


@dataclass
class CostRateReading:
    timestamp: float
    cost_usd: float
    total_tokens: int
    total_calls: int
    session_duration_min: float
    cost_per_minute: float = 0.0
    tokens_per_call: float = 0.0
    cost_per_1k_tokens: float = 0.0
    trend: SpendingTrend = SpendingTrend.STABLE
    acceleration: float = 0.0


@dataclass
class BudgetAdjustment:
    budget_multiplier: float
    threshold_multiplier: float
    reason: str = ""
    pid_output: float = 0.0
    cost_rate: float = 0.0
    setpoint: float = 0.0
    trend: SpendingTrend = SpendingTrend.STABLE


class CostRateSensor:
    """Derives consumption rate metrics from CostTracker snapshots.

    Sliding-window sensor that computes not just current cost/min,
    but also acceleration (d²cost/dt²), trend classification,
    and efficiency metrics (tokens/call, $/1k tokens).
    """

    def __init__(self, window_size: int = 10):
        self._window_size = window_size
        self._readings: list[CostRateReading] = []
        self._last_cost: float = 0.0
        self._last_rate: float = 0.0
        self._last_time: float = 0.0

    def measure(
        self,
        cost_usd: float,
        total_tokens: int,
        total_calls: int,
        session_start: float | None = None,
    ) -> CostRateReading:
        now = time.time()
        session_start = session_start or (now - 60.0)
        duration_min = max((now - session_start) / 60.0, 0.01)

        cost_per_minute = cost_usd / duration_min
        tokens_per_call = total_tokens / max(total_calls, 1)
        cost_per_1k = (cost_usd / max(total_tokens, 1)) * 1000 if total_tokens > 0 else 0.0

        dt = now - self._last_time if self._last_time > 0 else 1.0
        dt = max(dt, 0.01)
        raw_accel = (cost_per_minute - self._last_rate) / dt if dt > 0 else 0.0

        alpha = 0.3
        smoothed_accel = alpha * raw_accel + (1 - alpha) * self._acceleration_history()

        trend = self._classify_trend(cost_per_minute, smoothed_accel, total_calls)

        reading = CostRateReading(
            timestamp=now,
            cost_usd=cost_usd,
            total_tokens=total_tokens,
            total_calls=total_calls,
            session_duration_min=duration_min,
            cost_per_minute=cost_per_minute,
            tokens_per_call=tokens_per_call,
            cost_per_1k_tokens=cost_per_1k,
            trend=trend,
            acceleration=smoothed_accel,
        )

        self._readings.append(reading)
        if len(self._readings) > self._window_size * 3:
            self._readings = self._readings[-self._window_size * 2:]

        self._last_cost = cost_usd
        self._last_rate = cost_per_minute
        self._last_time = now

        return reading

    def _acceleration_history(self) -> float:
        recent = self._readings[-5:] if len(self._readings) >= 5 else self._readings
        return sum(r.acceleration for r in recent) / len(recent) if recent else 0.0

    def _classify_trend(
        self, rate: float, accel: float, calls: int
    ) -> SpendingTrend:
        if len(self._readings) < 3:
            return SpendingTrend.STABLE
        if accel > 0.05 and rate > 1.0:
            return SpendingTrend.BURST
        if accel > 0.01:
            return SpendingTrend.ACCELERATING
        if accel < -0.005:
            return SpendingTrend.DECELERATING
        return SpendingTrend.STABLE

    def get_recent_readings(self, n: int = 3) -> list[CostRateReading]:
        return self._readings[-n:]


class BudgetPIDController:
    """PID controller for dynamic budget adjustment.

    Maps cost-rate error to a budget multiplier in [0.3, 2.0]:

      error = setpoint - cost_rate
        > 0: under-spending → loosen budget (multiplier > 1.0)
        < 0: over-spending  → tighten budget (multiplier < 1.0)

    The neutral point (output = 1.0) means "use default thresholds".
    Output is clamped to prevent extreme values that would break functionality.
    """

    def __init__(
        self,
        kp: float = 1.5,
        ki: float = 0.08,
        kd: float = 0.2,
        setpoint_cost_per_min: float = 0.50,
        output_min: float = 0.30,
        output_max: float = 2.0,
        integral_windup_limit: float = 3.0,
    ):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.setpoint = setpoint_cost_per_min
        self.output_min = output_min
        self.output_max = output_max
        self.integral_windup_limit = integral_windup_limit

        self._integral = 0.0
        self._prev_error = 0.0
        self._prev_time: float = 0.0
        self._initialized = False

        self.output_history: list[float] = []

    def compute(self, cost_rate: float) -> float:
        now = time.time()
        if not self._initialized:
            self._prev_time = now
            self._prev_error = self.setpoint - cost_rate
            self._initialized = True
            return 1.0

        dt = now - self._prev_time
        dt = max(dt, 0.001)

        error = self.setpoint - cost_rate

        p_term = self.kp * error

        self._integral += error * dt
        self._integral = max(-self.integral_windup_limit,
                             min(self.integral_windup_limit, self._integral))
        i_term = self.ki * self._integral

        derivative = (error - self._prev_error) / dt
        d_term = self.kd * derivative

        raw_output = 1.0 + p_term + i_term + d_term
        output = max(self.output_min, min(self.output_max, raw_output))

        self._prev_error = error
        self._prev_time = now

        self.output_history.append(output)
        if len(self.output_history) > 50:
            self.output_history = self.output_history[-25:]

        return output

    def reset(self):
        self._integral = 0.0
        self._prev_error = 0.0
        self._initialized = False
        self.output_history.clear()


class BudgetActuator:
    """Applies PID output as concrete parameter adjustments.

    Maps the abstract budget_multiplier to specific parameters on
    ToolResultBudgetManager:
      - persist_threshold = BASE_PERSIST_THRESHOLD * threshold_multiplier
      - budget_per_message = BASE_BUDGET_PER_MESSAGE * budget_multiplier

    Both multipliers are derived from the same PID output but with
    different scaling curves to ensure functional correctness even
    at extreme values.
    """

    BASE_PERSIST_THRESHOLD = 4000
    BASE_BUDGET_PER_MESSAGE = 8000
    MIN_PERSIST_THRESHOLD = 1000
    MAX_PERSIST_THRESHOLD = 12000
    MIN_BUDGET_PER_MESSAGE = 2000
    MAX_BUDGET_PER_MESSAGE = 24000

    def compute_adjustment(
        self,
        pid_output: float,
        cost_rate: float,
        setpoint: float,
        trend: SpendingTrend,
    ) -> BudgetAdjustment:
        budget_mult = pid_output
        threshold_mult = pid_output

        if trend == SpendingTrend.BURST:
            threshold_mult *= 0.6
            budget_mult *= 0.7
        elif trend == SpendingTrend.ACCELERATING:
            threshold_mult *= 0.8
            budget_mult *= 0.85
        elif trend == SpendingTrend.DECELERATING:
            threshold_mult *= 1.05
            budget_mult *= 1.05

        threshold_mult = max(0.25, min(3.0, threshold_mult))
        budget_mult = max(0.25, min(3.0, budget_mult))

        int(max(
            self.MIN_PERSIST_THRESHOLD,
            min(self.MAX_PERSIST_THRESHOLD,
                round(self.BASE_PERSIST_THRESHOLD * threshold_mult)),
        ))
        int(max(
            self.MIN_BUDGET_PER_MESSAGE,
            min(self.MAX_BUDGET_PER_MESSAGE,
                round(self.BASE_BUDGET_PER_MESSAGE * budget_mult)),
        ))

        reason_parts = []
        if pid_output < 0.8:
            reason_parts.append(f"tighten(pid={pid_output:.2f})")
        elif pid_output > 1.2:
            reason_parts.append(f"loosen(pid={pid_output:.2f})")
        else:
            reason_parts.append(f"neutral(pid={pid_output:.2f})")

        if trend != SpendingTrend.STABLE:
            reason_parts.append(f"trend={trend.name}")

        return BudgetAdjustment(
            budget_multiplier=budget_mult,
            threshold_multiplier=threshold_mult,
            reason="; ".join(reason_parts),
            pid_output=pid_output,
            cost_rate=round(cost_rate, 4),
            setpoint=setpoint,
            trend=trend,
        )


class CostControlLoop:
    """Main orchestrator: sense cost rate → PID control → adjust budget.

    Ties together all components into a single run() method that can be
    called once per agent turn (or after each API call).
    """

    def __init__(
        self,
        *,
        target_cost_per_min: float = 0.50,
        kp: float = 1.5,
        ki: float = 0.08,
        kd: float = 0.2,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.sensor = CostRateSensor()
        self.pid = BudgetPIDController(
            kp=kp, ki=ki, kd=kd,
            setpoint_cost_per_min=target_cost_per_min,
        )
        self.actuator = BudgetActuator()

        self._cycle_count = 0
        self._last_adjustment: BudgetAdjustment | None = None
        self._last_reading: CostRateReading | None = None

    @property
    def last_adjustment(self) -> BudgetAdjustment | None:
        return self._last_adjustment

    @property
    def last_reading(self) -> CostRateReading | None:
        return self._last_reading

    def run(
        self,
        cost_usd: float,
        total_tokens: int,
        total_calls: int,
        session_start: float | None = None,
    ) -> BudgetAdjustment:
        if not self.enabled:
            return BudgetAdjustment(
                budget_multiplier=1.0, threshold_multiplier=1.0,
                reason="disabled",
            )

        self._cycle_count += 1

        reading = self.sensor.measure(
            cost_usd=cost_usd,
            total_tokens=total_tokens,
            total_calls=total_calls,
            session_start=session_start,
        )
        self._last_reading = reading

        pid_output = self.pid.compute(reading.cost_per_minute)

        adjustment = self.actuator.compute_adjustment(
            pid_output=pid_output,
            cost_rate=reading.cost_per_minute,
            setpoint=self.pid.setpoint,
            trend=reading.trend,
        )
        self._last_adjustment = adjustment

        return adjustment

    def apply_to_budget_manager(self, budget_manager) -> dict[str, int]:
        """Apply the latest adjustment to a ToolResultBudgetManager instance.

        Args:
            budget_manager: A ToolResultBudgetManager instance to adjust.

        Returns:
            Dict with the new 'persist_threshold' and 'budget' values.
        """
        adj = self._last_adjustment
        if not adj or not self.enabled:
            return {}

        new_threshold = int(max(
            BudgetActuator.MIN_PERSIST_THRESHOLD,
            min(BudgetActuator.MAX_PERSIST_THRESHOLD,
                round(BudgetActuator.BASE_PERSIST_THRESHOLD * adj.threshold_multiplier)),
        ))
        new_budget = int(max(
            BudgetActuator.MIN_BUDGET_PER_MESSAGE,
            min(BudgetActuator.MAX_BUDGET_PER_MESSAGE,
                round(BudgetActuator.BASE_BUDGET_PER_MESSAGE * adj.budget_multiplier)),
        ))

        budget_manager._persist_threshold = new_threshold
        budget_manager._budget = new_budget

        return {"persist_threshold": new_threshold, "budget": new_budget}

    def get_stats(self) -> dict[str, Any]:
        sensor_recent = self.sensor.get_recent_readings(1)
        reading = sensor_recent[0] if sensor_recent else None
        return {
            "control_enabled": self.enabled,
            "cycles_executed": self._cycle_count,
            "sensor": {
                "cost_per_min": round(reading.cost_per_minute, 4) if reading else 0,
                "tokens_per_call": round(reading.tokens_per_call, 1) if reading else 0,
                "cost_per_1k_tokens": round(reading.cost_per_1k_tokens, 4) if reading else 0,
                "trend": reading.trend.name if reading else "unknown",
                "acceleration": round(reading.acceleration, 6) if reading else 0,
            },
            "pid": {
                "setpoint": self.pid.setpoint,
                "kp": self.pid.kp,
                "ki": self.pid.ki,
                "kd": self.pid.kd,
                "last_output": round(self.pid.output_history[-1], 4) if self.pid.output_history else 1.0,
                "integral": round(self.pid._integral, 4),
            },
            "adjustment": {
                "budget_mult": round(self._last_adjustment.budget_multiplier, 3) if self._last_adjustment else 1.0,
                "threshold_mult": round(self._last_adjustment.threshold_multiplier, 3) if self._last_adjustment else 1.0,
                "reason": self._last_adjustment.reason if self._last_adjustment else "none",
            } if self._last_adjustment else None,
        }

    def reset(self):
        self.sensor = CostRateSensor(window_size=self.sensor._window_size)
        self.pid.reset()
        self._cycle_count = 0
        self._last_adjustment = None
        self._last_reading = None
