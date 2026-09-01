"""State Observer based on Engineering Cybernetics.

钱学森工程控制论核心原理:
- 状态观测器：通过可测量输出估计不可测量状态
- 黑箱方法：通过输入输出关系推断系统内部状态
- 卡尔曼滤波：最优状态估计理论

This module implements:
1. Kalman filter-based state estimation
2. Black-box system identification
3. State prediction and extrapolation
4. Observability analysis
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ObservedState:
    """Estimated internal state of the agent system."""
    internal_load: float = 0.0       # 0.0 - 1.0, true computational load
    hidden_errors: float = 0.0       # 0.0 - 1.0, unreported error probability
    context_pressure: float = 0.0    # 0.0 - 1.0, latent context pressure
    skill_mastery: float = 0.0       # 0.0 - 1.0, current skill effectiveness
    system_degradation: float = 0.0  # 0.0 - 1.0, accumulated degradation
    estimated_timestamp: float = field(default_factory=time.time)
    confidence: float = 1.0


@dataclass
class MeasurementVector:
    """Observable measurements from the system."""
    timestamp: float
    response_time: float = 0.0
    success_rate: float = 1.0
    token_usage: float = 0.0
    error_count: int = 0
    retry_count: int = 0
    context_length: int = 0
    tool_calls: int = 0


class KalmanFilter:
    """1-D Kalman filter for state estimation.

    卡尔曼滤波器:
    最优状态估计算法，在噪声环境下精确估计系统状态。
    """

    def __init__(self, process_noise: float = 0.01, measurement_noise: float = 0.1,
                 initial_estimate: float = 0.0, initial_uncertainty: float = 1.0):
        self.process_noise = process_noise
        self.measurement_noise = measurement_noise
        self.estimate = initial_estimate
        self.uncertainty = initial_uncertainty

    def update(self, measurement: float) -> float:
        prediction_uncertainty = self.uncertainty + self.process_noise
        kalman_gain = prediction_uncertainty / (prediction_uncertainty + self.measurement_noise)
        self.estimate = self.estimate + kalman_gain * (measurement - self.estimate)
        self.uncertainty = (1.0 - kalman_gain) * prediction_uncertainty
        self.uncertainty = max(0.0, min(1.0, self.uncertainty))
        return self.estimate

    def predict(self, dt: float = 1.0) -> float:
        self.uncertainty += self.process_noise * dt
        return self.estimate

    def get_confidence(self) -> float:
        return 1.0 - self.uncertainty

    def reset(self, initial_estimate: float = 0.0, initial_uncertainty: float = 1.0) -> None:
        self.estimate = initial_estimate
        self.uncertainty = initial_uncertainty


class StateObserver:
    """State observer for the agent system.

    状态观测器（黑箱方法）:
    ┌────────────────────────────────────────────────────────┐
    │  可测量输出 ─→ 观测器 ─→ 内部状态估计                   │
    │    (响应时间、成功率)       (真实负载、隐藏错误)        │
    │                                    ↓                   │
    │                              状态预测 + 预警           │
    └────────────────────────────────────────────────────────┘

    Features:
    - Multi-dimensional Kalman filtering
    - Black-box system identification
    - State prediction and trend analysis
    - Observability assessment
    """

    def __init__(self):
        self._internal_load_kf = KalmanFilter(
            process_noise=0.02, measurement_noise=0.15,
            initial_estimate=0.0, initial_uncertainty=0.5,
        )
        self._hidden_errors_kf = KalmanFilter(
            process_noise=0.01, measurement_noise=0.2,
            initial_estimate=0.0, initial_uncertainty=0.5,
        )
        self._context_pressure_kf = KalmanFilter(
            process_noise=0.03, measurement_noise=0.1,
            initial_estimate=0.0, initial_uncertainty=0.5,
        )
        self._skill_mastery_kf = KalmanFilter(
            process_noise=0.05, measurement_noise=0.25,
            initial_estimate=0.5, initial_uncertainty=0.8,
        )
        self._system_degradation_kf = KalmanFilter(
            process_noise=0.005, measurement_noise=0.1,
            initial_estimate=0.0, initial_uncertainty=0.3,
        )

        self._measurement_history: list[MeasurementVector] = []
        self._state_history: list[ObservedState] = []
        self._max_history = 100

        self._response_time_baseline: float = 0.0
        self._sample_count: int = 0

    def update(self, measurement: MeasurementVector) -> ObservedState:
        self._measurement_history.append(measurement)
        if len(self._measurement_history) > self._max_history:
            self._measurement_history.pop(0)

        self._sample_count += 1
        if self._sample_count == 1:
            self._response_time_baseline = measurement.response_time

        internal_load = self._estimate_internal_load(measurement)
        hidden_errors = self._estimate_hidden_errors(measurement)
        context_pressure = self._estimate_context_pressure(measurement)
        skill_mastery = self._estimate_skill_mastery(measurement)
        system_degradation = self._estimate_system_degradation(measurement)

        overall_confidence = (
            self._internal_load_kf.get_confidence() * 0.25
            + self._hidden_errors_kf.get_confidence() * 0.25
            + self._context_pressure_kf.get_confidence() * 0.2
            + self._skill_mastery_kf.get_confidence() * 0.15
            + self._system_degradation_kf.get_confidence() * 0.15
        )

        state = ObservedState(
            internal_load=internal_load,
            hidden_errors=hidden_errors,
            context_pressure=context_pressure,
            skill_mastery=skill_mastery,
            system_degradation=system_degradation,
            estimated_timestamp=time.time(),
            confidence=overall_confidence,
        )

        self._state_history.append(state)
        if len(self._state_history) > self._max_history:
            self._state_history.pop(0)

        return state

    def predict_state(self, steps_ahead: int = 3) -> ObservedState:
        if len(self._state_history) < 3:
            return ObservedState()

        recent = self._state_history[-min(5, len(self._state_history)):]

        def extrapolate(attr: str) -> float:
            if len(recent) < 2:
                return getattr(recent[-1], attr)

            trend = recent[-1].__dict__[attr] - recent[0].__dict__[attr]
            trend_per_step = trend / (len(recent) - 1)
            predicted = recent[-1].__dict__[attr] + trend_per_step * steps_ahead
            return max(0.0, min(1.0, predicted))

        return ObservedState(
            internal_load=extrapolate("internal_load"),
            hidden_errors=extrapolate("hidden_errors"),
            context_pressure=extrapolate("context_pressure"),
            skill_mastery=extrapolate("skill_mastery"),
            system_degradation=extrapolate("system_degradation"),
            estimated_timestamp=time.time(),
            confidence=max(0.1, recent[-1].confidence - steps_ahead * 0.1),
        )

    def get_observability_score(self) -> float:
        if not self._measurement_history:
            return 0.0

        variance_scores = []
        for attr in ["response_time", "success_rate", "token_usage"]:
            values = [getattr(m, attr) for m in self._measurement_history[-20:]]
            if len(values) < 2:
                variance_scores.append(0.0)
                continue

            mean_val = sum(values) / len(values)
            variance = sum((x - mean_val) ** 2 for x in values) / len(values)
            std_dev = math.sqrt(variance)

            if std_dev > 0.01:
                variance_scores.append(min(1.0, std_dev * 2))
            else:
                variance_scores.append(0.0)

        return sum(variance_scores) / len(variance_scores) if variance_scores else 0.0

    def get_state_summary(self) -> dict[str, Any]:
        if not self._state_history:
            return {"status": "no_data"}

        latest = self._state_history[-1]
        return {
            "internal_load": f"{latest.internal_load:.2f}",
            "hidden_errors": f"{latest.hidden_errors:.2f}",
            "context_pressure": f"{latest.context_pressure:.2f}",
            "skill_mastery": f"{latest.skill_mastery:.2f}",
            "system_degradation": f"{latest.system_degradation:.2f}",
            "confidence": f"{latest.confidence:.2f}",
            "observability": f"{self.get_observability_score():.2f}",
        }

    def _estimate_internal_load(self, measurement: MeasurementVector) -> float:
        latency_ratio = measurement.response_time / max(self._response_time_baseline, 0.001)
        latency_score = min(1.0, latency_ratio / 3.0)

        tool_intensity = min(1.0, measurement.tool_calls / 10.0)

        estimated_load = latency_score * 0.6 + tool_intensity * 0.4
        return self._internal_load_kf.update(estimated_load)

    def _estimate_hidden_errors(self, measurement: MeasurementVector) -> float:
        error_indicator = 1.0 - measurement.success_rate
        retry_ratio = min(1.0, measurement.retry_count / 3.0)

        hidden_estimate = error_indicator * 0.7 + retry_ratio * 0.3
        return self._hidden_errors_kf.update(hidden_estimate)

    def _estimate_context_pressure(self, measurement: MeasurementVector) -> float:
        context_usage = min(1.0, measurement.context_length / 100000)
        response_degradation = max(0.0, (measurement.response_time - self._response_time_baseline) / max(self._response_time_baseline, 0.001))
        response_score = min(1.0, response_degradation / 2.0)

        pressure_estimate = context_usage * 0.7 + response_score * 0.3
        return self._context_pressure_kf.update(pressure_estimate)

    def _estimate_skill_mastery(self, measurement: MeasurementVector) -> float:
        if measurement.success_rate > 0.9 and measurement.retry_count == 0:
            skill_indicator = 0.9
        elif measurement.success_rate > 0.7:
            skill_indicator = 0.6
        else:
            skill_indicator = 0.3

        return self._skill_mastery_kf.update(skill_indicator)

    def _estimate_system_degradation(self, measurement: MeasurementVector) -> float:
        if self._sample_count < 10:
            return 0.0

        recent = self._measurement_history[-5:]
        avg_recent_errors = sum(m.error_count for m in recent) / len(recent)
        avg_recent_response = sum(m.response_time for m in recent) / len(recent)

        baseline_response = self._response_time_baseline
        if baseline_response <= 0:
            return 0.0

        response_increase = max(0.0, (avg_recent_response - baseline_response) / baseline_response)
        error_increase = avg_recent_errors / 5.0

        degradation_estimate = min(1.0, response_increase * 0.4 + error_increase * 0.6)
        return self._system_degradation_kf.update(degradation_estimate)

    def reset(self) -> None:
        self._internal_load_kf.reset(0.0, 0.5)
        self._hidden_errors_kf.reset(0.0, 0.5)
        self._context_pressure_kf.reset(0.0, 0.5)
        self._skill_mastery_kf.reset(0.5, 0.8)
        self._system_degradation_kf.reset(0.0, 0.3)
        self._measurement_history = []
        self._state_history = []
        self._sample_count = 0
        self._response_time_baseline = 0.0
