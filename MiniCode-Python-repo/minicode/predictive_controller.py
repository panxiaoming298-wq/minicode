"""Predictive Controller based on Engineering Cybernetics.

钱学森工程控制论核心原理:
- 预测控制：基于系统模型预测未来状态，提前调整控制
- 前馈-反馈复合控制：结合预测和反馈实现最优控制
- 滚动优化：在预测时域内滚动优化控制策略

This module implements:
1. Exponential smoothing time series prediction
2. Moving average trend prediction
3. Multi-horizon forecasting
4. Predictive action recommendation
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PredictionHorizon(Enum):
    """Prediction time horizon."""
    SHORT = "short"       # 1-3 steps ahead
    MEDIUM = "medium"     # 3-10 steps ahead
    LONG = "long"         # 10-50 steps ahead


@dataclass
class PredictionResult:
    """Result of a prediction."""
    metric_name: str
    predicted_value: float
    confidence: float
    prediction_horizon: PredictionHorizon
    trend_direction: str  # "up", "down", "stable"
    predicted_at: float = field(default_factory=time.time)
    actual_value: float | None = None
    error: float | None = None


@dataclass
class PredictiveAction:
    """Recommended action based on prediction."""
    action_type: str
    urgency: float          # 0.0 - 1.0
    metric_name: str
    predicted_issue: str
    recommended_action: str
    expected_benefit: str
    deadline_steps: int = 3


class ExponentialSmoother:
    """Exponential smoothing for time series prediction.

    指数平滑预测:
    对历史数据加权平均，近期数据权重更高。
    """

    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self._forecast: float = 0.0
        self._initialized: bool = False
        self._mae: float = 0.0

    def update(self, actual: float) -> float:
        if not self._initialized:
            self._forecast = actual
            self._initialized = True
            return self._forecast

        error = abs(actual - self._forecast)
        self._mae = 0.9 * self._mae + 0.1 * error

        self._forecast = self.alpha * actual + (1 - self.alpha) * self._forecast
        return self._forecast

    def predict(self, steps_ahead: int = 1) -> float:
        return self._forecast

    def get_confidence(self) -> float:
        if self._mae < 0.01:
            return 0.95
        return max(0.1, 1.0 - self._mae * 2)

    def reset(self) -> None:
        self._forecast = 0.0
        self._initialized = False
        self._mae = 0.0


class MovingAveragePredictor:
    """Moving average trend predictor.

    移动平均趋势预测:
    通过不同时间窗口的移动平均判断趋势方向。
    """

    def __init__(self, window_sizes: tuple[int, int, int] = (5, 10, 20)):
        self._windows: dict[int, deque[float]] = {w: deque(maxlen=w) for w in window_sizes}
        self._all_values: deque[float] = deque(maxlen=100)

    def add_value(self, value: float) -> None:
        for window in self._windows.values():
            window.append(value)
        self._all_values.append(value)

    def predict_trend(self) -> str:
        if len(self._all_values) < 3:
            return "stable"

        short_avg = self._get_average(5)
        medium_avg = self._get_average(10)
        long_avg = self._get_average(20)

        if short_avg is None or medium_avg is None or long_avg is None:
            return "stable"

        if short_avg > medium_avg > long_avg:
            return "up"
        elif short_avg < medium_avg < long_avg:
            return "down"
        return "stable"

    def predict_value(self, steps_ahead: int = 1) -> float | None:
        if len(self._all_values) < 2:
            return None

        trend = self.predict_trend()
        recent = list(self._all_values)[-10:]

        if len(recent) < 2:
            return recent[-1] if recent else None

        recent_slope = (recent[-1] - recent[0]) / (len(recent) - 1)

        if trend == "up":
            slope_multiplier = 1.2
        elif trend == "down":
            slope_multiplier = 1.2
        else:
            slope_multiplier = 0.5

        predicted = recent[-1] + recent_slope * steps_ahead * slope_multiplier
        return predicted

    def _get_average(self, window_size: int) -> float | None:
        window = self._windows.get(window_size)
        if not window or len(window) < window_size * 0.5:
            return None
        return sum(window) / len(window)

    def reset(self) -> None:
        for window in self._windows.values():
            window.clear()
        self._all_values.clear()


class PredictiveController:
    """Predictive controller for proactive system management.

    预测控制器（超前控制）:
    ┌─────────────────────────────────────────────────────────────┐
    │  历史数据 ─→ 预测模型 ─→ 未来状态预测 ─→ 预防性控制动作     │
    │                       ↓                                      │
    │                 风险预警 + 优化建议                          │
    └─────────────────────────────────────────────────────────────┘

    Features:
    - Multi-metric prediction
    - Trend detection and extrapolation
    - Predictive action recommendation
    - Confidence-based filtering
    """

    def __init__(self, max_history: int = 100):
        self._predictors: dict[str, dict[str, Any]] = {}
        self._prediction_history: list[PredictionResult] = []
        self._max_history = max_history
        self._max_pred_history = 200

        self._init_metrics()

    def _init_metrics(self) -> None:
        metrics = [
            "response_time",
            "error_rate",
            "context_usage",
            "cpu_usage",
            "memory_usage",
            "throughput",
            "stability_score",
            "performance_score",
        ]
        for metric in metrics:
            self._predictors[metric] = {
                "exp_smoother": ExponentialSmoother(alpha=0.3),
                "ma_predictor": MovingAveragePredictor(),
                "last_prediction": None,
                "prediction_count": 0,
            }

    def update(self, metric_name: str, value: float) -> None:
        if metric_name not in self._predictors:
            self._predictors[metric_name] = {
                "exp_smoother": ExponentialSmoother(alpha=0.3),
                "ma_predictor": MovingAveragePredictor(),
                "last_prediction": None,
                "prediction_count": 0,
            }

        predictor = self._predictors[metric_name]
        predictor["exp_smoother"].update(value)
        predictor["ma_predictor"].add_value(value)

    def predict(self, metric_name: str, horizon: PredictionHorizon = PredictionHorizon.SHORT) -> PredictionResult | None:
        if metric_name not in self._predictors:
            return None

        predictor = self._predictors[metric_name]

        steps_ahead = {
            PredictionHorizon.SHORT: 3,
            PredictionHorizon.MEDIUM: 7,
            PredictionHorizon.LONG: 20,
        }.get(horizon, 3)

        exp_prediction = predictor["exp_smoother"].predict(steps_ahead)
        ma_prediction = predictor["ma_predictor"].predict_value(steps_ahead)

        if ma_prediction is not None:
            predicted_value = exp_prediction * 0.4 + ma_prediction * 0.6
        else:
            predicted_value = exp_prediction

        confidence = predictor["exp_smoother"].get_confidence()

        trend = predictor["ma_predictor"].predict_trend()

        result = PredictionResult(
            metric_name=metric_name,
            predicted_value=predicted_value,
            confidence=confidence,
            prediction_horizon=horizon,
            trend_direction=trend,
        )

        predictor["last_prediction"] = result
        predictor["prediction_count"] += 1

        self._prediction_history.append(result)
        if len(self._prediction_history) > self._max_pred_history:
            self._prediction_history.pop(0)

        return result

    def predict_all(self, horizon: PredictionHorizon = PredictionHorizon.SHORT) -> list[PredictionResult]:
        results = []
        for metric_name in self._predictors:
            result = self.predict(metric_name, horizon)
            if result:
                results.append(result)
        return results

    def generate_predictive_actions(self) -> list[PredictiveAction]:
        actions = []

        for metric_name, predictor in self._predictors.items():
            if predictor["last_prediction"] is None:
                continue

            prediction = predictor["last_prediction"]
            if prediction.confidence < 0.5:
                continue

            action = self._assess_prediction(metric_name, prediction)
            if action:
                actions.append(action)

        actions.sort(key=lambda a: a.urgency, reverse=True)
        return actions

    def record_actual(self, metric_name: str, actual_value: float) -> None:
        for pred in reversed(self._prediction_history):
            if pred.metric_name == metric_name and pred.actual_value is None:
                pred.actual_value = actual_value
                pred.error = abs(pred.predicted_value - actual_value)
                break

    def get_prediction_accuracy(self) -> dict[str, float]:
        accuracy: dict[str, list[float]] = {}

        for pred in self._prediction_history:
            if pred.error is not None:
                if pred.metric_name not in accuracy:
                    accuracy[pred.metric_name] = []

                error_ratio = pred.error / max(abs(pred.predicted_value), 0.01)
                accuracy[pred.metric_name].append(max(0.0, 1.0 - error_ratio))

        return {
            metric: sum(errors) / len(errors) if errors else 0.0
            for metric, errors in accuracy.items()
        }

    def get_prediction_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "predictions": {},
            "trends": {},
            "accuracy": self.get_prediction_accuracy(),
        }

        for metric_name, predictor in self._predictors.items():
            if predictor["last_prediction"]:
                pred = predictor["last_prediction"]
                summary["predictions"][metric_name] = {
                    "value": pred.predicted_value,
                    "confidence": pred.confidence,
                    "trend": pred.trend_direction,
                }
                summary["trends"][metric_name] = pred.trend_direction

        return summary

    def _assess_prediction(self, metric_name: str, prediction: PredictionResult) -> PredictiveAction | None:
        thresholds = {
            "response_time": {"high": 45.0, "low": 5.0},
            "error_rate": {"high": 3.0, "low": 0.0},
            "context_usage": {"high": 0.85, "low": 0.0},
            "cpu_usage": {"high": 0.9, "low": 0.0},
            "memory_usage": {"high": 0.9, "low": 0.0},
            "throughput": {"high": 10.0, "low": 0.5},
            "stability_score": {"high": 1.0, "low": 0.5},
            "performance_score": {"high": 1.0, "low": 0.5},
        }

        if metric_name not in thresholds:
            return None

        thresh = thresholds[metric_name]

        if prediction.predicted_value > thresh["high"] and prediction.trend_direction == "up":
            urgency = min(1.0, (prediction.predicted_value - thresh["high"]) / thresh["high"])

            actions_map = {
                "response_time": ("reduce_concurrency", "减少并发任务", "降低响应延迟", 2),
                "error_rate": ("enable_safe_mode", "启用安全模式", "防止错误扩散", 1),
                "context_usage": ("trigger_compaction", "触发上下文压缩", "释放上下文空间", 2),
                "cpu_usage": ("throttle_tasks", "节流任务", "降低CPU使用率", 2),
                "memory_usage": ("trigger_gc", "触发内存回收", "释放内存", 2),
            }

            if metric_name in actions_map:
                action_type, recommended, benefit, deadline = actions_map[metric_name]
                return PredictiveAction(
                    action_type=action_type,
                    urgency=urgency,
                    metric_name=metric_name,
                    predicted_issue=f"{metric_name} 预测值 {prediction.predicted_value:.2f} 将超过阈值 {thresh['high']:.2f}",
                    recommended_action=recommended,
                    expected_benefit=benefit,
                    deadline_steps=deadline,
                )

        if metric_name in ("stability_score", "performance_score"):
            if prediction.predicted_value < thresh["low"] and prediction.trend_direction == "down":
                urgency = min(1.0, (thresh["low"] - prediction.predicted_value) / thresh["low"])
                return PredictiveAction(
                    action_type="intervention_required",
                    urgency=urgency,
                    metric_name=metric_name,
                    predicted_issue=f"{metric_name} 预测将降至 {prediction.predicted_value:.2f}",
                    recommended_action="启动干预机制",
                    expected_benefit="防止性能进一步下降",
                    deadline_steps=3,
                )

        return None

    def reset(self) -> None:
        for metric_name in self._predictors:
            self._predictors[metric_name]["exp_smoother"].reset()
            self._predictors[metric_name]["ma_predictor"].reset()
            self._predictors[metric_name]["last_prediction"] = None
            self._predictors[metric_name]["prediction_count"] = 0
        self._prediction_history = []
