"""Adaptive Feedback Controller based on Engineering Cybernetics.

钱学森工程控制论核心原理:
- 负反馈：纠正偏差、维持稳定
- 正反馈：放大变化、驱动进化
- 自适应调节：根据误差动态调整系统参数

This module implements:
1. Negative feedback loop (error correction & stabilization)
2. Positive feedback loop (pattern reinforcement & skill optimization)
3. PID-inspired adaptive controller for agent tuning
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class FeedbackMode(Enum):
    """Type of feedback control."""
    NEGATIVE = "negative"   # Error correction, stabilization
    POSITIVE = "positive"   # Pattern reinforcement, evolution
    ADAPTIVE = "adaptive"   # Combined, adjusts based on context


@dataclass
class SystemState:
    """Current state of the agent system (黑箱方法 - 通过输出观测)."""
    # Performance metrics
    success_rate: float = 1.0        # Tool execution success rate (0.0 - 1.0)
    avg_response_time: float = 0.0    # Average turn duration in seconds
    token_efficiency: float = 0.0     # Useful output / total tokens (0.0 - 1.0)
    context_usage: float = 0.0        # Context window usage ratio (0.0 - 1.0)

    # Stability metrics
    error_frequency: float = 0.0      # Errors per turn
    retry_count: float = 0.0          # Average retries per turn
    oscillation_index: float = 0.0    # How much behavior oscillates (0.0 - 1.0)

    # Evolution metrics
    skill_effectiveness: float = 0.0  # Skill-based improvements (0.0 - 1.0)
    pattern_reuse_rate: float = 0.0   # How often patterns are reused
    knowledge_accumulation: float = 0.0  # Memory growth rate

    timestamp: float = field(default_factory=time.time)

    def stability_score(self) -> float:
        """Overall stability score (1.0 = perfectly stable)."""
        error_penalty = self.error_frequency * 0.3 + self.retry_count * 0.2
        oscillation_penalty = self.oscillation_index * 0.2
        usage_penalty = max(0.0, (self.context_usage - 0.8) * 0.3)
        return max(0.0, min(1.0, 1.0 - error_penalty - oscillation_penalty - usage_penalty))

    def performance_score(self) -> float:
        """Overall performance score (1.0 = perfect)."""
        return (
            self.success_rate * 0.3
            + self.token_efficiency * 0.2
            + (1.0 - self.avg_response_time / 60.0) * 0.2  # Normalize to 60s
            + self.skill_effectiveness * 0.15
            + self.pattern_reuse_rate * 0.15
        )


@dataclass
class ControlSignal:
    """Output of the feedback controller - commands to adjust agent behavior."""
    # Stability controls (negative feedback)
    reduce_parallelism: bool = False
    reduce_tool_timeout: float | None = None
    increase_nudge_frequency: bool = False
    force_compaction: bool = False
    limit_max_steps: int | None = None

    # Performance controls
    increase_model_level: bool = False   # Route to better model
    decrease_model_level: bool = False   # Route to cheaper model
    adjust_token_budget: float = 1.0     # Multiplier for token budget
    adjust_concurrency: int = 0          # Change in max concurrent tools

    # Evolution controls (positive feedback)
    promote_pattern: str | None = None   # Pattern ID to reinforce
    recommend_skill_update: bool = False
    suggest_memory_persistence: bool = False

    # Oscillation detection
    oscillation_index: float = 0.0

    # Confidence in the signal
    confidence: float = 1.0
    reason: str = ""


@dataclass
class _PIDState:
    """PID controller internal state for each controlled variable."""
    integral: float = 0.0
    previous_error: float = 0.0


class PIDController:
    """PID-inspired controller for adaptive tuning.

    工程控制论 PID 原理:
    - P (Proportional): 当前误差的直接响应
    - I (Integral): 累积误差，消除静态偏差
    - D (Derivative): 误差变化率，预测未来趋势，抑制超调
    """

    def __init__(self, kp: float = 1.0, ki: float = 0.1, kd: float = 0.05,
                 output_min: float = -1.0, output_max: float = 1.0):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_min = output_min
        self.output_max = output_max
        self._state = _PIDState()

    def compute(self, setpoint: float, measured: float, dt: float = 1.0) -> float:
        """Compute PID control output.

        Args:
            setpoint: Desired target value
            measured: Current measured value
            dt: Time delta since last computation
        """
        error = setpoint - measured

        # Proportional
        p = self.kp * error

        # Integral (with anti-windup)
        self._state.integral += error * dt
        self._state.integral = max(-10.0, min(10.0, self._state.integral))
        i = self.ki * self._state.integral

        # Derivative
        d = self.kd * (error - self._state.previous_error) / max(dt, 0.001)
        self._state.previous_error = error

        output = p + i + d
        return max(self.output_min, min(self.output_max, output))

    def reset(self) -> None:
        """Reset controller state."""
        self._state = _PIDState()


class FeedbackController:
    """Adaptive feedback controller for the agent system.

    控制论架构:
    ┌──────────────────────────────────────────────────────┐
    │                    系统（Agent）                      │
    │                                                      │
    │  传感器 → 控制器(PID) ─→ 执行器 ─→ 输出              │
    │    ↑                              ↓                  │
    │    └────── 误差计算 ←── 反馈信号 ←──┘                │
    └──────────────────────────────────────────────────────┘

    Features:
    - Negative feedback: stabilizes agent behavior
    - Positive feedback: reinforces successful patterns
    - Adaptive PID tuning for multiple system variables
    - Black-box observation (无需了解LLM内部)
    """

    def __init__(self):
        # PID controllers for key variables
        self._stability_pid = PIDController(kp=1.5, ki=0.2, kd=0.1)
        self._performance_pid = PIDController(kp=1.0, ki=0.15, kd=0.08)
        self._efficiency_pid = PIDController(kp=0.8, ki=0.1, kd=0.05)

        # Setpoints (desired values)
        self._stability_target = 0.85
        self._performance_target = 0.75
        self._efficiency_target = 0.60

        # Historical states for derivative calculation
        self._previous_state: SystemState | None = None
        self._last_update_time: float = time.time()

        # Oscillation detection (detect when agent behavior is oscillating)
        self._error_history: list[float] = []
        self._max_history = 10

        # Pattern tracking for positive feedback
        self._pattern_scores: dict[str, float] = {}

    def observe(self, state: SystemState) -> ControlSignal:
        """Observe current system state and compute control signal.

        黑箱方法：通过系统输出推断内部状态，不依赖LLM内部知识。
        """
        dt = time.time() - self._last_update_time
        self._last_update_time = time.time()

        signal = ControlSignal()

        # --- Negative Feedback Loop (负反馈：纠正偏差) ---
        stability = state.stability_score()
        stability_output = self._stability_pid.compute(self._stability_target, stability, dt)

        performance = state.performance_score()
        performance_output = self._performance_pid.compute(self._performance_target, performance, dt)

        efficiency = state.token_efficiency
        efficiency_output = self._efficiency_pid.compute(self._efficiency_target, efficiency, dt)

        # Apply stability controls
        if stability_output > 0.3:
            # System too unstable, apply corrective measures
            signal.reduce_parallelism = True
            signal.increase_nudge_frequency = True
            signal.reason = f"低稳定性 ({stability:.2f})，启动负反馈调节"

            if state.error_frequency > 3.0:
                signal.reduce_tool_timeout = 15.0  # Reduce timeout for fast failure
                signal.limit_max_steps = 20  # Limit max steps
                signal.reason += " + 错误频率过高"

            if state.context_usage > 0.85:
                signal.force_compaction = True
                signal.reason += " + 上下文超载"

        elif stability_output < -0.3:
            # System overly stable, can afford more aggressive behavior
            signal.adjust_concurrency = 2  # Allow more parallel tools
            signal.reason = f"系统稳定 ({stability:.2f})，可适当增加并发"

        # Apply performance controls
        if performance_output > 0.3:
            # Underperforming, consider using better model
            if state.avg_response_time > 30.0:
                signal.increase_model_level = True
                signal.reason = f"性能不足 ({performance:.2f})，建议升级模型"

        elif performance_output < -0.3:
            # Overperforming, can use cheaper model
            if state.success_rate > 0.9:
                signal.decrease_model_level = True
                signal.reason = f"性能优异 ({performance:.2f})，可降级模型节约成本"

        # Apply efficiency controls
        if efficiency_output > 0.3:
            # Low efficiency, reduce token budget
            signal.adjust_token_budget = 0.7
            signal.reason += f" 效率不足 ({efficiency:.2f})"

        # --- Positive Feedback Loop (正反馈：强化有效模式) ---
        if performance > 0.85 and state.pattern_reuse_rate > 0.3:
            # High performance with pattern reuse - reinforce
            signal.recommend_skill_update = True
            signal.suggest_memory_persistence = True
            signal.reason = f"高效运行 ({performance:.2f})，启动正反馈强化"

        # Update oscillation history
        error = 1.0 - state.stability_score()
        self._error_history.append(error)
        if len(self._error_history) > self._max_history:
            self._error_history.pop(0)

        # Compute oscillation index
        if len(self._error_history) >= 4:
            signal.oscillation_index = self._compute_oscillation()

        # Store state for next iteration
        self._previous_state = state
        signal.confidence = min(1.0, max(0.3, 1.0 - abs(stability_output) * 0.3))

        return signal

    def record_pattern_effectiveness(self, pattern_id: str, success: bool) -> None:
        """Record whether a pattern was effective (for positive feedback).

        正反馈：有效模式被强化，无效模式被淘汰。
        """
        current = self._pattern_scores.get(pattern_id, 0.5)
        # Exponential moving average
        alpha = 0.2
        new_score = (1 - alpha) * current + alpha * (1.0 if success else 0.0)
        self._pattern_scores[pattern_id] = new_score

        # Promote if consistently effective
        if new_score > 0.85:
            self._pattern_scores[pattern_id] = min(1.0, new_score + 0.05)

    def get_pattern_recommendations(self) -> list[tuple[str, float]]:
        """Get patterns ranked by effectiveness (for skill optimization)."""
        return sorted(
            self._pattern_scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )

    def _compute_oscillation(self) -> float:
        """Detect oscillation in error signals.

        振荡检测：高频方向变化表明系统不稳定。
        """
        if len(self._error_history) < 4:
            return 0.0

        direction_changes = 0
        for i in range(2, len(self._error_history)):
            prev_delta = self._error_history[i - 1] - self._error_history[i - 2]
            curr_delta = self._error_history[i] - self._error_history[i - 1]
            if prev_delta * curr_delta < 0:
                direction_changes += 1

        return direction_changes / (len(self._error_history) - 2)

    def reset(self) -> None:
        """Reset all controller state."""
        self._stability_pid.reset()
        self._performance_pid.reset()
        self._efficiency_pid.reset()
        self._previous_state = None
        self._error_history = []
        self._pattern_scores = {}

    def get_status(self) -> dict:
        """Get controller status for debugging."""
        return {
            "stability_target": self._stability_target,
            "performance_target": self._performance_target,
            "efficiency_target": self._efficiency_target,
            "pattern_count": len(self._pattern_scores),
            "error_history_size": len(self._error_history),
            "oscillation_index": self._compute_oscillation() if len(self._error_history) >= 4 else 0.0,
        }
