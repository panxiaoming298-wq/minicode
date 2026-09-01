"""System Stability Monitor based on Engineering Cybernetics.

钱学森工程控制论核心原理:
- 稳定性分析：系统在扰动下恢复平衡的能力
- 鲁棒性：不确定环境下的可靠运行
- 系统观测：通过传感器监测关键指标

This module implements:
1. Real-time stability monitoring
2. Robustness assessment under uncertainty
3. Anomaly detection with early warning
4. System health scoring
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from minicode.context_cybernetics import ContextCyberneticsOrchestrator

class HealthLevel(Enum):
    """System health level."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class MetricSnapshot:
    """Point-in-time snapshot of system metrics."""
    timestamp: float
    cpu_usage: float = 0.0        # 0.0 - 1.0
    memory_usage: float = 0.0     # 0.0 - 1.0
    context_usage: float = 0.0    # 0.0 - 1.0
    error_rate: float = 0.0       # Errors per turn
    avg_latency: float = 0.0      # Seconds
    throughput: float = 0.0       # Tasks per minute
    active_tasks: int = 0
    queued_tasks: int = 0


@dataclass
class AnomalyRecord:
    """Record of a detected anomaly."""
    timestamp: float
    metric_name: str
    value: float
    threshold: float
    severity: str  # low / medium / high / critical
    description: str


@dataclass
class StabilityReport:
    """Comprehensive stability report."""
    health_level: HealthLevel
    health_score: float          # 0.0 - 1.0
    stability_index: float       # 0.0 - 1.0
    robustness_score: float      # 0.0 - 1.0
    anomalies: list[AnomalyRecord] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    summary: str = ""


class StabilityMonitor:
    """Real-time stability monitor for the agent system.

    控制论架构（状态观测器）:
    ┌──────────────────────────────────────────────────────────┐
    │  系统执行 ─→ 传感器采集 ─→ 指标计算 ─→ 异常检测          │
    │                    ↓                                     │
    │              健康评分 + 预警通知                          │
    └──────────────────────────────────────────────────────────┘

    Features:
    - Sliding window analysis for trend detection
    - Multi-dimensional health scoring
    - Anomaly detection with configurable thresholds
    - Robustness assessment under varying load
    """

    # Threshold configurations
    _THRESHOLDS = {
        "cpu_usage": {"warning": 0.8, "critical": 0.95},
        "memory_usage": {"warning": 0.85, "critical": 0.95},
        "context_usage": {"warning": 0.75, "critical": 0.9},
        "error_rate": {"warning": 2.0, "critical": 5.0},
        "avg_latency": {"warning": 30.0, "critical": 60.0},
        "throughput": {"warning": 1.0, "critical": 0.5},
    }

    # Weights for health score computation
    _HEALTH_WEIGHTS = {
        "error_rate": 0.3,
        "context_usage": 0.2,
        "avg_latency": 0.15,
        "cpu_usage": 0.15,
        "memory_usage": 0.1,
        "throughput": 0.1,
    }

    def __init__(self, window_size: int = 100):
        self._window_size = window_size
        self._metrics: deque[MetricSnapshot] = deque(maxlen=window_size)
        self._anomalies: list[AnomalyRecord] = []
        self._max_anomalies = 50

        # Baseline metrics (learned over time)
        self._baseline_latency: float = 0.0
        self._baseline_throughput: float = 0.0
        self._baseline_error_rate: float = 0.0
        self._sample_count: int = 0

    def record_snapshot(self, snapshot: MetricSnapshot) -> None:
        """Record a new metric snapshot."""
        self._metrics.append(snapshot)
        self._update_baseline(snapshot)

        # Check for anomalies
        self._detect_anomalies(snapshot)

    def get_stability_report(self) -> StabilityReport:
        """Generate comprehensive stability report."""
        if not self._metrics:
            return StabilityReport(
                health_level=HealthLevel.HEALTHY,
                health_score=1.0,
                stability_index=1.0,
                robustness_score=1.0,
                summary="No data available yet",
            )

        report = StabilityReport(
            health_level=HealthLevel.HEALTHY,
            health_score=self._compute_health_score(),
            stability_index=self._compute_stability_index(),
            robustness_score=self._compute_robustness_score(),
        )

        # Determine health level
        if report.health_score < 0.3:
            report.health_level = HealthLevel.CRITICAL
        elif report.health_score < 0.5:
            report.health_level = HealthLevel.WARNING
        elif report.health_score < 0.7:
            report.health_level = HealthLevel.DEGRADED

        # Add recent anomalies
        report.anomalies = list(self._anomalies[-10:])

        # Generate recommendations
        report.recommendations = self._generate_recommendations(report)

        # Generate summary
        report.summary = self._generate_summary(report)

        return report

    def check_health(self) -> tuple[HealthLevel, float]:
        """Quick health check. Returns (level, score)."""
        score = self._compute_health_score()
        if score < 0.3:
            return HealthLevel.CRITICAL, score
        elif score < 0.5:
            return HealthLevel.WARNING, score
        elif score < 0.7:
            return HealthLevel.DEGRADED, score
        return HealthLevel.HEALTHY, score

    def is_stable(self, threshold: float = 0.7) -> bool:
        """Check if system is stable above threshold."""
        return self._compute_stability_index() >= threshold

    def get_recent_metrics(self, count: int = 10) -> list[MetricSnapshot]:
        """Get recent metric snapshots."""
        return list(self._metrics)[-count:]

    def get_anomaly_count(self, since: float | None = None) -> int:
        """Get anomaly count, optionally since a timestamp."""
        if since is None:
            return len(self._anomalies)
        return sum(1 for a in self._anomalies if a.timestamp >= since)

    def feed_orchestrator(self, orchestrator: "ContextCyberneticsOrchestrator") -> None:
        """Push latest MetricSnapshot into the cybernetics orchestrator.

        Bridges StabilityMonitor (system-level observer) with
        ContextCyberneticsOrchestrator (context-level controller),
        enabling unified coupling analysis for adaptive threshold tuning.
        """
        latest = self._metrics[-1] if self._metrics else None
        if latest and orchestrator is not None:
            orchestrator.feed_from_stability_monitor(
                context_usage=latest.context_usage,
                error_rate=latest.error_rate,
                avg_latency=latest.avg_latency,
                cpu_usage=latest.cpu_usage,
                memory_usage=latest.memory_usage,
            )

    def _compute_health_score(self) -> float:
        """Compute overall health score from weighted metrics."""
        if not self._metrics:
            return 1.0

        latest = self._metrics[-1]
        score = 0.0

        # Error rate component (lower is better)
        error_score = max(0.0, 1.0 - latest.error_rate / self._THRESHOLDS["error_rate"]["critical"])
        score += error_score * self._HEALTH_WEIGHTS["error_rate"]

        # Context usage component (lower is better, up to 0.75 is fine)
        context_score = max(0.0, 1.0 - max(0.0, latest.context_usage - 0.75) / 0.25)
        score += context_score * self._HEALTH_WEIGHTS["context_usage"]

        # Latency component (lower is better)
        latency_score = max(0.0, 1.0 - latest.avg_latency / self._THRESHOLDS["avg_latency"]["critical"])
        score += latency_score * self._HEALTH_WEIGHTS["avg_latency"]

        # CPU component
        cpu_score = max(0.0, 1.0 - latest.cpu_usage)
        score += cpu_score * self._HEALTH_WEIGHTS["cpu_usage"]

        # Memory component
        memory_score = max(0.0, 1.0 - latest.memory_usage)
        score += memory_score * self._HEALTH_WEIGHTS["memory_usage"]

        # Throughput component (higher is better)
        if self._baseline_throughput > 0:
            throughput_ratio = latest.throughput / self._baseline_throughput
            throughput_score = min(1.0, throughput_ratio)
            score += throughput_score * self._HEALTH_WEIGHTS["throughput"]
        else:
            score += 0.5 * self._HEALTH_WEIGHTS["throughput"]

        return max(0.0, min(1.0, score))

    def _compute_stability_index(self) -> float:
        """Compute stability index based on metric variance.

        稳定性指数：指标波动越小越稳定。
        """
        if len(self._metrics) < 5:
            return 1.0  # Not enough data, assume stable

        # Compute coefficient of variation for key metrics
        latencies = [m.avg_latency for m in self._metrics if m.avg_latency > 0]
        if not latencies:
            return 1.0

        mean_latency = sum(latencies) / len(latencies)
        variance = sum((x - mean_latency) ** 2 for x in latencies) / len(latencies)
        std_dev = math.sqrt(variance)
        cv = std_dev / mean_latency if mean_latency > 0 else 0

        # Error rate stability
        error_rates = [m.error_rate for m in self._metrics]
        mean_errors = sum(error_rates) / len(error_rates)
        error_variance = sum((x - mean_errors) ** 2 for x in error_rates) / len(error_rates)
        error_std = math.sqrt(error_variance)
        error_cv = error_std / mean_errors if mean_errors > 0 else 0

        # Combined stability (lower CV = more stable)
        stability = 1.0 / (1.0 + cv * 0.5 + error_cv * 0.5)
        return max(0.0, min(1.0, stability))

    def _compute_robustness_score(self) -> float:
        """Compute robustness score under varying conditions.

        鲁棒性评分：在负载变化时保持稳定的能力。
        """
        if len(self._metrics) < 10:
            return 1.0  # Not enough data

        # Check performance under high load
        high_load_metrics = [m for m in self._metrics if m.active_tasks > 2]
        if not high_load_metrics:
            return 0.9  # Never tested under load

        # Compare error rates under load vs normal
        normal_metrics = [m for m in self._metrics if m.active_tasks <= 2]

        high_load_errors = sum(m.error_rate for m in high_load_metrics) / len(high_load_metrics)
        normal_errors = sum(m.error_rate for m in normal_metrics) / len(normal_metrics) if normal_metrics else 0

        # Robustness = how well we maintain error rate under load
        if normal_errors == 0:
            return 1.0 if high_load_errors == 0 else 0.5
        robustness = 1.0 - (high_load_errors - normal_errors) / normal_errors
        return max(0.0, min(1.0, robustness))

    def _detect_anomalies(self, snapshot: MetricSnapshot) -> None:
        """Detect anomalies in the latest snapshot."""
        for metric_name, thresholds in self._THRESHOLDS.items():
            value = getattr(snapshot, metric_name, None)
            if value is None:
                continue

            if value >= thresholds["critical"]:
                self._anomalies.append(AnomalyRecord(
                    timestamp=snapshot.timestamp,
                    metric_name=metric_name,
                    value=value,
                    threshold=thresholds["critical"],
                    severity="critical",
                    description=f"{metric_name} reached critical level: {value:.2f}",
                ))
            elif value >= thresholds["warning"]:
                self._anomalies.append(AnomalyRecord(
                    timestamp=snapshot.timestamp,
                    metric_name=metric_name,
                    value=value,
                    threshold=thresholds["warning"],
                    severity="warning",
                    description=f"{metric_name} reached warning level: {value:.2f}",
                ))

        # Trim anomalies
        if len(self._anomalies) > self._max_anomalies:
            self._anomalies = self._anomalies[-self._max_anomalies:]

    def _update_baseline(self, snapshot: MetricSnapshot) -> None:
        """Update baseline metrics using exponential moving average."""
        alpha = 0.1  # Smoothing factor
        self._sample_count += 1

        if self._sample_count == 1:
            self._baseline_latency = snapshot.avg_latency
            self._baseline_throughput = snapshot.throughput
            self._baseline_error_rate = snapshot.error_rate
        else:
            self._baseline_latency = (1 - alpha) * self._baseline_latency + alpha * snapshot.avg_latency
            self._baseline_throughput = (1 - alpha) * self._baseline_throughput + alpha * snapshot.throughput
            self._baseline_error_rate = (1 - alpha) * self._baseline_error_rate + alpha * snapshot.error_rate

    def _generate_recommendations(self, report: StabilityReport) -> list[str]:
        """Generate actionable recommendations based on current state."""
        recommendations = []

        if report.health_score < 0.5:
            recommendations.append("系统健康状况较差，建议减少并发任务数量")

        if report.stability_index < 0.5:
            recommendations.append("系统波动较大，建议启用更保守的重试策略")

        if report.robustness_score < 0.6:
            recommendations.append("高负载下性能下降明显，建议实施资源隔离")

        # Check specific metric issues
        if self._metrics:
            latest = self._metrics[-1]
            if latest.context_usage > 0.8:
                recommendations.append("上下文使用率过高，建议触发强制压缩")
            if latest.error_rate > 2.0:
                recommendations.append("错误率偏高，建议检查工具配置和权限设置")
            if latest.avg_latency > 30.0:
                recommendations.append("响应延迟较高，建议优化并发策略")

        return recommendations

    def _generate_summary(self, report: StabilityReport) -> str:
        """Generate human-readable summary."""
        level_desc = {
            HealthLevel.HEALTHY: "系统运行正常",
            HealthLevel.DEGRADED: "系统性能下降",
            HealthLevel.WARNING: "系统存在警告",
            HealthLevel.CRITICAL: "系统处于临界状态",
        }

        summary = f"健康状态: {level_desc[report.health_level]} "
        summary += f"(评分: {report.health_score:.2f}, "
        summary += f"稳定性: {report.stability_index:.2f}, "
        summary += f"鲁棒性: {report.robustness_score:.2f})"

        if report.anomalies:
            critical_count = sum(1 for a in report.anomalies if a.severity == "critical")
            warning_count = sum(1 for a in report.anomalies if a.severity == "warning")
            summary += f" | 异常: {critical_count} 严重, {warning_count} 警告"

        return summary

    def reset(self) -> None:
        """Reset monitor state."""
        self._metrics.clear()
        self._anomalies.clear()
        self._baseline_latency = 0.0
        self._baseline_throughput = 0.0
        self._baseline_error_rate = 0.0
        self._sample_count = 0
