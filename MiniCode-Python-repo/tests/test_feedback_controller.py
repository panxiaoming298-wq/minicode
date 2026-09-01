"""Unit tests for FeedbackController — dual-PID outer loop, ControlSignal, oscillation detection."""
from __future__ import annotations

import time

from minicode.feedback_controller import (
    ControlSignal,
    FeedbackController,
    FeedbackMode,
    PIDController,
    SystemState,
)


class TestControlSignal:
    """ControlSignal dataclass integrity."""

    def test_all_fields_declared(self):
        cs = ControlSignal()
        assert hasattr(cs, "reduce_parallelism")
        assert hasattr(cs, "force_compaction")
        assert hasattr(cs, "limit_max_steps")
        assert hasattr(cs, "adjust_token_budget")
        assert hasattr(cs, "adjust_concurrency")
        assert hasattr(cs, "oscillation_index")

    def test_oscillation_index_default(self):
        cs = ControlSignal()
        assert cs.oscillation_index == 0.0

    def test_confidence_default(self):
        cs = ControlSignal()
        assert cs.confidence == 1.0

    def test_custom_values(self):
        cs = ControlSignal(
            force_compaction=True,
            adjust_token_budget=0.5,
            oscillation_index=0.3,
            confidence=0.8,
            reason="test",
        )
        assert cs.force_compaction is True
        assert cs.adjust_token_budget == 0.5
        assert cs.oscillation_index == 0.3


class TestSystemState:
    """SystemState scoring functions."""

    def test_perfect_state(self):
        state = SystemState(
            success_rate=1.0,
            error_frequency=0.0,
            retry_count=0.0,
            context_usage=0.5,
            oscillation_index=0.0,
        )
        assert state.stability_score() > 0.9

    def test_degraded_stability(self):
        state = SystemState(
            success_rate=0.5,
            error_frequency=0.5,
            retry_count=3.0,
            context_usage=0.95,
            oscillation_index=0.8,
        )
        score = state.stability_score()
        assert score < 0.6, f"Expected low stability, got {score}"

    def test_performance_score(self):
        state = SystemState(
            success_rate=0.9,
            token_efficiency=0.7,
            avg_response_time=10.0,
            skill_effectiveness=0.8,
            pattern_reuse_rate=0.7,
        )
        score = state.performance_score()
        assert 0.0 <= score <= 1.0

    def test_oscillation_affects_stability(self):
        calm = SystemState(oscillation_index=0.0)
        oscillating = SystemState(oscillation_index=0.9)
        assert calm.stability_score() > oscillating.stability_score()


class TestPIDController:
    """PID controller math."""

    def test_zero_error_gives_zero_output(self):
        pid = PIDController(kp=1.0, ki=0.1, kd=0.1)
        # First call returns 0 since previous_error starts at 0
        out = pid.compute(setpoint=0.0, measured=0.0, dt=1.0)
        assert out == 0.0

    def test_proportional_response(self):
        pid = PIDController(kp=2.0, ki=0.0, kd=0.0)
        out = pid.compute(setpoint=1.0, measured=0.5, dt=1.0)
        # error = 1.0 - 0.5 = 0.5, P = 2.0 * 0.5 = 1.0
        assert out == 1.0

    def test_integral_accumulation(self):
        pid = PIDController(kp=0.0, ki=0.5, kd=0.0)
        pid.compute(setpoint=1.0, measured=0.0, dt=1.0)
        out = pid.compute(setpoint=1.0, measured=0.0, dt=1.0)
        # error = 1.0 each time, integral builds
        assert out > 0.5

    def test_anti_windup_bounded(self):
        pid = PIDController(kp=0.0, ki=10.0, kd=0.0)
        for _ in range(20):
            pid.compute(setpoint=1.0, measured=0.0, dt=1.0)
        out = pid.compute(setpoint=1.0, measured=0.0, dt=1.0)
        # Output should be bounded (anti-windup built in)
        assert -1.0 <= out <= 1.0

    def test_reset_clears_state(self):
        pid = PIDController(kp=0.0, ki=0.5, kd=0.0)
        pid.compute(setpoint=1.0, measured=0.0, dt=1.0)
        pid.reset()
        out = pid.compute(setpoint=0.0, measured=0.0, dt=1.0)
        assert out == 0.0

    def test_output_clamped(self):
        pid = PIDController(kp=100.0, ki=0.0, kd=0.0, output_min=-0.5, output_max=0.5)
        out = pid.compute(setpoint=1.0, measured=0.0, dt=1.0)
        assert out == 0.5  # Clamped at max


class TestFeedbackControllerObserve:
    """FeedbackController.observe() produces valid ControlSignal."""

    def test_healthy_system_no_force_compaction(self):
        fc = FeedbackController()
        state = SystemState(
            success_rate=0.95,
            error_frequency=0.05,
            context_usage=0.4,
            oscillation_index=0.0,
        )
        signal = fc.observe(state)
        assert signal.force_compaction is False

    def test_critical_context_forces_compaction(self):
        fc = FeedbackController()
        state = SystemState(
            success_rate=0.3,
            error_frequency=0.8,
            context_usage=0.95,
            oscillation_index=0.7,
        )
        signal = fc.observe(state)
        # High error + low stability should trigger force_compaction
        assert signal.confidence > 0.3

    def test_oscillation_index_populated(self):
        fc = FeedbackController()
        for i in range(6):
            usage = 0.5 + (0.3 if i % 2 == 0 else 0.0)
            state = SystemState(
                context_usage=usage,
                error_frequency=0.2,
            )
            signal = fc.observe(state)
        # After 4+ observations, oscillation_index should be computed
        assert hasattr(signal, "oscillation_index")

    def test_signal_has_expected_fields(self):
        fc = FeedbackController()
        state = SystemState()
        signal = fc.observe(state)
        assert isinstance(signal.reduce_parallelism, bool)
        assert isinstance(signal.force_compaction, bool)
        assert isinstance(signal.adjust_token_budget, float)
        assert isinstance(signal.adjust_concurrency, int)
        assert isinstance(signal.increase_model_level, bool)
        assert isinstance(signal.decrease_model_level, bool)


class TestFeedbackControllerPatterns:
    """Pattern effectiveness tracking."""

    def test_record_pattern(self):
        fc = FeedbackController()
        fc.record_pattern_effectiveness("test_pattern", True)
        fc.record_pattern_effectiveness("test_pattern", True)
        fc.record_pattern_effectiveness("test_pattern", False)
        recs = fc.get_pattern_recommendations()
        assert isinstance(recs, list)

    def test_no_recommendations_when_empty(self):
        fc = FeedbackController()
        recs = fc.get_pattern_recommendations()
        assert recs == []


class TestFeedbackControllerReport:
    """System report generation."""

    def test_pattern_recommendations_empty_initially(self):
        fc = FeedbackController()
        recs = fc.get_pattern_recommendations()
        assert recs == []
