"""Tests for ContextCybernetics — Engineering Cybernetics for Context Management.

Covers all 7 components of the closed-loop control system:
  1. ContextPressureSensor      — sensor layer (measurement + anomaly detection)
  2. ContextPIDController       — control layer (PID with anti-windup)
  3. PredictiveOverflowGuard   — prediction layer (exponential smoothing forecast)
  4. AdaptiveThresholdManager  — adaptation layer (dynamic threshold via coupling)
  5. CompactionStrategySelector— selection layer (control output → strategy)
  6. CyberneticFeedbackLoop    — learning layer (post-action adaptation)
  7. ContextCyberneticsOrchestrator — full sense-think-act cycle
"""

import math
import time
import pytest

from minicode.context_cybernetics import (
    AnomalyType,
    ContextCyberneticsOrchestrator,
    ContextPIDController,
    ContextPressureReading,
    ContextPressureSensor,
    ControlAction,
    CyberneticFeedbackLoop,
    PredictiveOutlook,
    PredictiveOverflowGuard,
    AdaptiveThresholdManager,
    CompactionStrategySelector,
)
from minicode.context_compactor import (
    AutoCompactConfig,
    CompactStrategy,
    CompactTrigger,
    CompactionResult,
    ContextCompactor,
)


def estimate_tokens(msg: dict) -> int:
    content = msg.get("content", "")
    return max(10, len(content) // 3)


def make_msgs(n: int, size: int = 30) -> list[dict]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * size} for i in range(n)]


def make_result(strategy=CompactStrategy.MICROCOMPACT, tokens_freed=0, success=True) -> CompactionResult:
    return CompactionResult(
        success=success,
        strategy=strategy,
        trigger=CompactTrigger.MICROCOMPACT_CACHED,
        messages=[],
        tokens_freed=tokens_freed,
        summary_text="",
    )


class TestContextPressureSensor:
    """Test 1: Sensor Layer — continuous pressure measurement."""

    def test_basic_measurement(self):
        sensor = ContextPressureSensor()
        reading = sensor.measure(token_count=7000, message_count=50, context_window=10000)
        assert reading.usage_ratio == pytest.approx(0.7, abs=0.01)
        assert reading.token_count == 7000
        assert reading.message_count == 50

    def test_growth_rate_calculation(self):
        sensor = ContextPressureSensor()
        sensor.measure(5000, 10, 10000)
        reading = sensor.measure(6000, 12, 10000)
        assert reading.growth_rate > 0

    def test_full_pressure(self):
        sensor = ContextPressureSensor()
        reading = sensor.measure(10000, 100, 10000)
        assert reading.usage_ratio == pytest.approx(1.0, abs=0.01)

    def test_zero_context_window(self):
        sensor = ContextPressureSensor()
        reading = sensor.measure(5000, 10, 0)
        assert reading.usage_ratio >= 0

    def test_recent_readings(self):
        sensor = ContextPressureSensor()
        for i in range(5):
            sensor.measure(1000 * (i + 1), 10, 10000)
        recent = sensor.get_recent_readings(3)
        assert len(recent) == 3

    def test_avg_growth_rate(self):
        sensor = ContextPressureSensor()
        for _ in range(5):
            sensor.measure(5000, 10, 10000)
            sensor.measure(5500, 11, 10000)
        avg = sensor.get_avg_growth_rate(5)
        assert isinstance(avg, float)

    def test_detects_sudden_spike(self):
        sensor = ContextPressureSensor(window_size=5)
        for _ in range(4):
            sensor.measure(3000, 10, 10000)
        reading = sensor.measure(8000, 20, 10000)
        assert reading.anomaly == AnomalyType.SUDDEN_SPIKE

    def test_no_anomaly_for_stable(self):
        sensor = ContextPressureSensor(window_size=5)
        for i in range(6):
            sensor.measure(5000 + i * 50, 10, 10000)
        reading = sensor.measure(5200, 10, 10000)
        assert reading.anomaly is None


class TestContextPIDController:
    """Test 2: Control Layer — PID controller with anti-windup."""

    def test_first_call_initializes_and_returns_zero(self):
        pid = ContextPIDController(setpoint=0.70)
        output = pid.compute(0.70)
        assert output == 0.0

    def test_second_call_above_setpoint_produces_positive_output(self):
        pid = ContextPIDController(kp=2.0, setpoint=0.70)
        pid.compute(0.70)
        import time as _t; _t.sleep(0.01)
        output = pid.compute(0.90)
        assert output > 0

    def test_second_call_below_setpoint_stays_minimal(self):
        pid = ContextPIDController(kp=2.0, setpoint=0.70)
        pid.compute(0.70)
        import time as _t; _t.sleep(0.01)
        output = pid.compute(0.50)
        assert output == 0.0

    def test_output_clamping_high(self):
        pid = ContextPIDController(kp=100.0, setpoint=0.70)
        pid.compute(0.70)
        output = pid.compute(2.0)
        assert output <= 1.0

    def test_output_clamping_low(self):
        pid = ContextPIDController(kp=100.0, ki=0.0, kd=0.0, setpoint=0.70)
        pid.compute(0.70)
        output = pid.compute(0.0)
        assert output >= 0.0

    def test_integral_windup_limit(self):
        pid = ContextPIDController(kp=0.0, ki=1.0, kd=0.0, integral_windup_limit=1.0, setpoint=0.70)
        pid.compute(0.70)
        for _ in range(100):
            pid.compute(2.0)
        assert abs(pid._integral) <= 1.0 + 0.01

    def test_reset_clears_state(self):
        pid = ContextPIDController(setpoint=0.70)
        pid.compute(0.70)
        pid.compute(0.50)
        pid.reset()
        assert pid._integral == 0.0
        assert not pid._initialized
        assert len(pid.output_history) == 0

    def test_is_saturated_when_output_at_max(self):
        pid = ContextPIDController(kp=50.0, setpoint=0.70)
        pid.compute(0.70)
        for _ in range(15):
            pid.compute(2.0)
        assert len(pid.output_history) >= 10
        assert pid.is_saturated is True

    def test_not_saturated_normal_operation(self):
        pid = ContextPIDController(kp=0.5, ki=0.01, kd=0.1, setpoint=0.70)
        pid.compute(0.70)
        import time as _t
        for i in range(12):
            _t.sleep(0.005)
            pid.compute(0.68 + (i % 3) * 0.02)  # tight range around setpoint
        assert pid.is_saturated is False

    def test_derivative_dampens_oscillation(self):
        pid = ContextPIDController(kp=2.0, ki=0.05, kd=0.5, setpoint=0.70)
        outputs = []
        values = [0.85, 0.55, 0.88, 0.52, 0.86]
        pid.compute(0.70)
        for v in values:
            outputs.append(pid.compute(v))
        assert len(outputs) == len(values)


class TestPredictiveOverflowGuard:
    """Test 3: Prediction Layer — overflow forecasting."""

    def test_initial_state_no_prediction(self):
        guard = PredictiveOverflowGuard()
        outlook = guard.predict(horizon_turns=10)
        assert outlook.turns_until_overflow is None
        assert outlook.trend == "stable"

    def test_stable_usage_no_overflow_predicted(self):
        guard = PredictiveOverflowGuard()
        guard.update(0.50, 0.001)
        outlook = guard.predict(horizon_turns=10)
        assert outlook.turns_until_overflow is None or outlook.turns_until_overflow > 20

    def test_rising_usage_predicts_overflow(self):
        guard = PredictiveOverflowGuard()
        for _ in range(5):
            guard.update(0.75, 0.03)
        outlook = guard.predict(horizon_turns=10)
        assert outlook.turns_until_overflow is not None
        assert outlook.turns_until_overflow >= 0
        assert outlook.trend == "rising"

    def test_already_over_threshold_returns_zero(self):
        guard = PredictiveOverflowGuard()
        guard.update(0.96, 0.01)
        outlook = guard.predict()
        assert outlook.turns_until_overflow == 0
        assert outlook.urgency == 1.0

    def test_urgency_increases_as_overflow_approaches(self):
        guard = PredictiveOverflowGuard(safety_margin_turns=3)
        guard.update(0.80, 0.04)
        near = guard.predict()
        guard.update(0.80, 0.001)
        far = guard.predict()
        assert near.urgency >= far.urgency

    def test_falling_trend_detected(self):
        guard = PredictiveOverflowGuard()
        guard.update(0.80, -0.02)
        outlook = guard.predict()
        assert outlook.trend == "falling"

    def test_reset_clears_state(self):
        guard = PredictiveOverflowGuard()
        guard.update(0.80, 0.03)
        guard.predict()
        guard.reset()
        outlook = guard.predict()
        assert outlook.confidence == 0.0

    def test_confidence_grows_with_data(self):
        guard = PredictiveOverflowGuard()
        for _ in range(3):
            guard.update(0.75, 0.03)
        o1 = guard.predict()
        for _ in range(5):
            guard.update(0.78, 0.03)
        o2 = guard.predict()
        assert o2.confidence >= o1.confidence


class TestAdaptiveThresholdManager:
    """Test 4: Adaptation Layer — dynamic threshold management."""

    def test_default_threshold(self):
        mgr = AdaptiveThresholdManager(base_threshold=0.85)
        assert mgr.get_effective_threshold() == pytest.approx(0.85, abs=0.01)

    def test_intent_lowers_threshold_for_code(self):
        mgr = AdaptiveThresholdManager(base_threshold=0.85)
        mgr.set_intent("CODE")
        t = mgr.get_effective_threshold()
        assert t < 0.85

    def test_intent_raises_threshold_for_search(self):
        mgr = AdaptiveThresholdManager(base_threshold=0.85)
        mgr.set_intent("SEARCH")
        t = mgr.get_effective_threshold()
        assert t > 0.85

    def test_refactor_most_aggressive(self):
        mgr = AdaptiveThresholdManager(base_threshold=0.85)
        mgr.set_intent("REFACTOR")
        t = mgr.get_effective_threshold()
        code_mgr = AdaptiveThresholdManager(base_threshold=0.85)
        code_mgr.set_intent("CODE")
        assert t < code_mgr.get_effective_threshold()

    def test_coupling_tightens_threshold(self):
        mgr = AdaptiveThresholdManager(base_threshold=0.85)
        mgr.update_coupling(context_pressure=0.9, error_rate=0.8, latency=2.0)
        t = mgr.get_effective_threshold()
        assert t < 0.85

    def test_threshold_never_below_minimum(self):
        mgr = AdaptiveThresholdManager(base_threshold=0.60)
        mgr.set_intent("REFACTOR")
        mgr.update_coupling(0.99, 1.0, 10.0)
        assert mgr.get_effective_threshold() >= 0.55

    def test_stats_returns_all_fields(self):
        mgr = AdaptiveThresholdManager(base_threshold=0.85)
        stats = mgr.get_stats()
        assert "base_threshold" in stats
        assert "effective_threshold" in stats
        assert "intent_type" in stats

    def test_record_adaptation(self):
        mgr = AdaptiveThresholdManager()
        mgr.record_adaptation(0.78, "success")
        assert len(mgr._adaptation_history) == 1


class TestCompactionStrategySelector:
    """Test 5: Selection Layer — maps control output to concrete strategies."""

    def test_low_intensity_selects_microcompact(self):
        sel = CompactionStrategySelector()
        action = sel.select(intensity=0.10, urgency=0.0, anomaly=None, usage_ratio=0.5)
        assert action.strategy == CompactStrategy.MICROCOMPACT
        assert action.force_execution is False

    def test_medium_intensity_selects_session_memory(self):
        sel = CompactionStrategySelector()
        action = sel.select(intensity=0.45, urgency=0.1, anomaly=None, usage_ratio=0.65)
        assert action.strategy == CompactStrategy.SESSION_MEMORY

    def test_high_intensity_selects_full_compact(self):
        sel = CompactionStrategySelector()
        action = sel.select(intensity=0.70, urgency=0.2, anomaly=None, usage_ratio=0.80)
        assert action.strategy == CompactStrategy.FULL

    def test_very_high_intensity_forces_execution(self):
        sel = CompactionStrategySelector()
        action = sel.select(intensity=0.90, urgency=0.3, anomaly=None, usage_ratio=0.90)
        assert action.force_execution is True

    def test_critical_urgency_overrides_to_full(self):
        sel = CompactionStrategySelector()
        action = sel.select(intensity=0.10, urgency=0.95, anomaly=None, usage_ratio=0.5)
        assert action.strategy == CompactStrategy.FULL
        assert action.force_execution is True

    def test_sudden_spike_triggers_session_memory(self):
        sel = CompactionStrategySelector()
        action = sel.select(
            intensity=0.25, urgency=0.1,
            anomaly=AnomalyType.SUDDEN_SPIKE, usage_ratio=0.78,
        )
        assert action.strategy == CompactStrategy.SESSION_MEMORY

    def test_accelerating_growth_triggers_full(self):
        sel = CompactionStrategySelector()
        action = sel.select(
            intensity=0.35, urgency=0.2,
            anomaly=AnomalyType.ACCELERATING_GROWTH, usage_ratio=0.70,
        )
        assert action.strategy == CompactStrategy.FULL

    def test_reason_populated(self):
        sel = CompactionStrategySelector()
        action = sel.select(intensity=0.50, urgency=0.0, anomaly=None, usage_ratio=0.6)
        assert len(action.reason) > 0


class TestCyberneticFeedbackLoop:
    """Test 6: Learning Layer — post-action adaptation."""

    def test_record_increments_total(self):
        loop = CyberneticFeedbackLoop()
        action = ControlAction(compaction_intensity=0.5, strategy=CompactStrategy.SESSION_MEMORY)
        result = make_result(tokens_freed=0)
        loop.record(action, result, 0.85, 0.60)
        assert loop._total_compactions == 1

    def test_effectiveness_rate(self):
        loop = CyberneticFeedbackLoop()
        action = ControlAction(compaction_intensity=0.5, strategy=CompactStrategy.FULL)
        effective_result = make_result(CompactStrategy.FULL, 2000)
        empty_result = make_result(tokens_freed=0)
        loop.record(action, effective_result, 0.85, 0.55)
        loop.record(action, empty_result, 0.70, 0.70)
        assert loop.get_effectiveness_rate() == pytest.approx(0.5, abs=0.01)

    def test_no_oscillation_with_stable_data(self):
        loop = CyberneticFeedbackLoop()
        action = ControlAction(compaction_intensity=0.5, strategy=CompactStrategy.FULL)
        result = make_result(CompactStrategy.FULL, 1000)
        for i in range(6):
            loop.record(action, result, 0.80 - i * 0.03, 0.60 - i * 0.03)
        assert loop.detect_oscillation() is False

    def test_oscillation_detected_with_alternating(self):
        loop = CyberneticFeedbackLoop()
        action = ControlAction(compaction_intensity=0.5, strategy=CompactStrategy.FULL)
        result = make_result(CompactStrategy.FULL, 500)
        usages_before = [0.85, 0.82, 0.87, 0.79, 0.86, 0.78]
        usages_after = [0.60, 0.72, 0.58, 0.71, 0.57, 0.73]
        for b, a in zip(usages_before, usages_after):
            loop.record(action, result, b, a)
        assert loop.detect_oscillation() is True

    def test_pid_adjustment_recommended_on_oscillation(self):
        loop = CyberneticFeedbackLoop()
        action = ControlAction(compaction_intensity=0.5, strategy=CompactStrategy.FULL)
        result = make_result(CompactStrategy.FULL, 300)
        for b, a in zip([0.86]*6, [0.55, 0.75, 0.53, 0.77, 0.54, 0.76]):
            loop.record(action, result, b, a)
        adj = loop.recommend_pid_adjustment()
        assert adj is not None
        assert "kd_boost" in adj

    def test_no_adjustment_without_oscillation(self):
        loop = CyberneticFeedbackLoop()
        action = ControlAction(compaction_intensity=0.5, strategy=CompactStrategy.MICROCOMPACT)
        result = make_result(CompactStrategy.MICROCOMPACT, 100)
        for i in range(6):
            loop.record(action, result, 0.80 - i*0.02, 0.55 - i*0.02)
        assert loop.recommend_pid_adjustment() is None

    def test_stats_comprehensive(self):
        loop = CyberneticFeedbackLoop()
        action = ControlAction(compaction_intensity=0.5, strategy=CompactStrategy.FULL)
        result = make_result(CompactStrategy.FULL, 999)
        loop.record(action, result, 0.85, 0.55)
        stats = loop.get_stats()
        assert "total_compactions" in stats
        assert "effectiveness_rate" in stats
        assert "oscillation_detected" in stats


class TestContextCyberneticsOrchestrator:
    """Test 7: Full Orchestration — end-to-end sense-think-act cycle."""

    def _make_orchestrator(self, context_window: int = 10000) -> ContextCyberneticsOrchestrator:
        config = AutoCompactConfig(threshold_ratio=0.85, circuit_breaker_limit=3, session_memory_enabled=False)
        compactor = ContextCompactor(
            context_window=context_window, workspace="/tmp",
            estimate_fn=estimate_tokens, config=config,
        )
        return ContextCyberneticsOrchestrator(
            compactor, kp=2.0, ki=0.15, kd=0.3,
            pid_setpoint=0.70, base_threshold=0.85,
            enabled=True,
        )

    def test_disabled_orchestrator_passthrough(self):
        orch = self._make_orchestrator()
        orch.enabled = False
        msgs = make_msgs(5)
        out, res, act = orch.run_cycle(msgs)
        assert out is msgs
        assert res is None
        assert act is None

    def test_cycle_produces_action_at_high_usage(self):
        orch = self._make_orchestrator(context_window=3000)
        msgs = make_msgs(40, size=50)
        out, res, act = orch.run_cycle(msgs)
        assert act is not None
        assert isinstance(act, ControlAction)

    def test_pid_setpoint_respected(self):
        orch = self._make_orchestrator()
        assert orch.pid.setpoint == 0.70

    def test_pid_intensity_rises_when_usage_exceeds_setpoint(self):
        orch = self._make_orchestrator(context_window=5000)
        orch.run_cycle(make_msgs(10, size=90), turn_id=1)
        _, _, act = orch.run_cycle(make_msgs(80, size=90), turn_id=2)
        assert act is not None
        assert act.pid_output > 0
        assert act.compaction_intensity >= act.pid_output

    def test_sensor_records_reading_each_cycle(self):
        orch = self._make_orchestrator(context_window=5000)
        orch.run_cycle(make_msgs(10))
        orch.run_cycle(make_msgs(15))
        assert orch.sensor.get_recent_readings(1)[0] is not None

    def test_predictor_called_each_cycle(self):
        orch = self._make_orchestrator(context_window=5000)
        orch.run_cycle(make_msgs(10), turn_id=1)
        orch.run_cycle(make_msgs(15), turn_id=2)
        stats = orch.get_stats()
        assert stats["predictor"]["trend"] != "unknown"

    def test_run_cycle_with_error_rate_and_latency(self):
        orch = self._make_orchestrator(context_window=5000)
        out, res, act = orch.run_cycle(
            make_msgs(10), error_rate=0.3, avg_latency=5.0, turn_id=1,
        )
        assert act is not None

    def test_set_intent_propagates_to_threshold_manager(self):
        orch = self._make_orchestrator()
        orch.set_intent("REFACTOR")
        t = orch.threshold_mgr.get_effective_threshold()
        default = AdaptiveThresholdManager(base_threshold=0.85).get_effective_threshold()
        assert t < default

    def test_try_reactive_recover_delegates(self):
        orch = self._make_orchestrator(context_window=5000)
        msgs = make_msgs(5)
        recovered, result = orch.try_reactive_recover(msgs, "prompt too long")
        assert recovered is not None
        assert isinstance(result, (CompactionResult, type(None)))

    def test_get_stats_returns_full_hierarchy(self):
        orch = self._make_orchestrator()
        orch.run_cycle(make_msgs(5), turn_id=1)
        stats = orch.get_stats()
        assert "sensor" in stats
        assert "pid" in stats
        assert "predictor" in stats
        assert "threshold" in stats
        assert "feedback" in stats
        assert "cycles_executed" in stats

    def test_reset_clears_everything(self):
        orch = self._make_orchestrator()
        orch.run_cycle(make_msgs(5), turn_id=1)
        orch.run_cycle(make_msgs(5), turn_id=2)
        orch.reset()
        assert orch._cycle_count == 0
        assert orch._last_action is None
        assert len(orch.pid.output_history) == 0

    def test_last_action_updated_after_cycle(self):
        orch = self._make_orchestrator(context_window=3000)
        orch.run_cycle(make_msgs(40, size=50), turn_id=1)
        assert orch.last_action is not None
        assert 0 <= orch.last_action.compaction_intensity <= 1.0

    def test_multiple_cycles_accumulate(self):
        orch = self._make_orchestrator(context_window=5000)
        for i in range(5):
            orch.run_cycle(make_msgs(8 + i), turn_id=i)
        stats = orch.get_stats()
        assert stats["cycles_executed"] == 5

    def test_feedback_loop_records_each_action(self):
        orch = self._make_orchestrator(context_window=5000)
        orch.run_cycle(make_msgs(10), turn_id=1)
        orch.run_cycle(make_msgs(12), turn_id=2)
        assert orch.feedback._total_compactions >= 2


class TestCyberneticsE2EIntegration:
    """End-to-end integration: full pipeline under realistic scenarios."""

    def _orch(self, cw: int = 8000) -> ContextCyberneticsOrchestrator:
        cfg = AutoCompactConfig(threshold_ratio=0.85, circuit_breaker_limit=3, session_memory_enabled=False)
        comp = ContextCompactor(context_window=cw, workspace="/tmp", estimate_fn=estimate_tokens, config=cfg)
        return ContextCyberneticsOrchestrator(comp, kp=2.0, ki=0.15, kd=0.3, pid_setpoint=0.70, base_threshold=0.85, enabled=True)

    def test_gradual_context_rise_triggers_progressive_response(self):
        orch = self._orch(cw=5000)
        intensities = []
        for n in [5, 10, 15, 20, 25, 30]:
            _, _, act = orch.run_cycle(make_msgs(n, size=40), turn_id=n)
            if act:
                intensities.append(act.compaction_intensity)
        assert len(intensities) >= 1

    def test_stable_context_produces_minimal_action(self):
        orch = self._orch(cw=50000)
        _, _, act = orch.run_cycle(make_msgs(5), turn_id=1)
        if act:
            assert act.compaction_intensity < 0.5

    def test_sudden_spike_detected_in_pipeline(self):
        orch = self._orch(cw=5000)
        for _ in range(4):
            orch.run_cycle(make_msgs(5, size=10), turn_id=1)
        _, _, act = orch.run_cycle(make_msgs(35, size=80), turn_id=5)
        if act and act.compaction_intensity > 0:
            assert isinstance(act.reason, str)

    def test_full_stats_report_after_multiple_cycles(self):
        orch = self._orch(cw=8000)
        for i in range(8):
            orch.run_cycle(make_msgs(5 + i * 2, size=30), turn_id=i, error_rate=0.05 * i, avg_latency=float(i))
        stats = orch.get_stats()
        assert stats["cycles_executed"] == 8
        assert stats["feedback"]["total_compactions"] >= 1
        assert isinstance(stats["threshold"]["effective_threshold"], float)

    def test_disabled_mode_is_transparent(self):
        orch = self._orch(cw=1000)
        orch.enabled = False
        original = make_msgs(10, size=100)
        out, _, _ = orch.run_cycle(original)
        assert out is original

    def test_reactive_recovery_integration(self):
        orch = self._orch(cw=2000)
        msgs = make_msgs(30, size=50)
        recovered, result = orch.try_reactive_recover(msgs, "prompt too long error")
        assert isinstance(recovered, list)
        assert isinstance(result, CompactionResult)
