"""Tests for CostControlLoop — BudgetPIDController for Cost Regulation.

Covers all 5 components of the cost control closed-loop:
  1. CostRateSensor — consumption rate measurement + trend classification
  2. BudgetPIDController — PID with anti-windup for budget adjustment
  3. BudgetActuator — maps PID output to concrete threshold/budget values
  4. CostControlLoop — full sense→control→actuate orchestration
  5. Integration with ToolResultBudgetManager
"""

import time
import pytest

from minicode.cost_control import (
    BudgetActuator,
    BudgetAdjustment,
    BudgetPIDController,
    CostControlLoop,
    CostRateReading,
    CostRateSensor,
    SpendingTrend,
)
from minicode.context_compactor import (
    AutoCompactConfig,
    ContextCompactor,
)


def estimate_tokens(msg: dict) -> int:
    return max(10, len(msg.get("content", "")) // 3)


class TestCostRateSensor:
    """Test 1: Sensor Layer — consumption rate measurement."""

    def test_basic_measurement(self):
        s = CostRateSensor()
        r = s.measure(cost_usd=0.50, total_tokens=10000, total_calls=5)
        assert r.cost_usd == 0.50
        assert r.total_tokens == 10000
        assert r.total_calls == 5
        assert r.session_duration_min > 0

    def test_cost_per_minute(self):
        s = CostRateSensor()
        r = s.measure(cost_usd=1.00, total_tokens=5000, total_calls=3, session_start=time.time() - 60.0)
        assert abs(r.cost_per_minute - 1.0) < 0.1

    def test_tokens_per_call(self):
        s = CostRateSensor()
        r = s.measure(cost_usd=0.10, total_tokens=9000, total_calls=3)
        assert r.tokens_per_call == pytest.approx(3000.0)

    def test_stable_trend_default(self):
        s = CostRateSensor(window_size=5)
        for _ in range(6):
            s.measure(0.50, 10000, 5)
        r = s.measure(0.50, 10000, 5)
        assert r.trend in (SpendingTrend.STABLE, SpendingTrend.ACCELERATING)

    def test_burst_detection(self):
        s = CostRateSensor(window_size=5)
        for _ in range(4):
            s.measure(0.05, 500, 2)
        r = s.measure(5.00, 50000, 20)
        assert r.trend == SpendingTrend.BURST

    def test_recent_readings(self):
        s = CostRateSensor()
        for i in range(6):
            s.measure(float(i) * 0.1, i * 1000, i + 1)
        recent = s.get_recent_readings(3)
        assert len(recent) == 3


class TestBudgetPIDController:
    """Test 2: Control Layer — PID for budget adjustment."""

    def test_first_call_returns_neutral(self):
        pid = BudgetPIDController(setpoint_cost_per_min=0.50)
        out = pid.compute(0.50)
        assert out == 1.0

    def test_under_spending_loosens_budget(self):
        pid = BudgetPIDController(kp=1.5, setpoint_cost_per_min=0.50)
        pid.compute(0.50)
        time.sleep(0.01)
        out = pid.compute(0.05)  # way under setpoint → positive error → output > 1
        assert out >= 1.0

    def test_over_spending_tightens_budget(self):
        pid = BudgetPIDController(kp=1.5, setpoint_cost_per_min=0.50)
        pid.compute(0.50)
        time.sleep(0.01)
        out = pid.compute(5.00)  # way over setpoint → negative error → output < 1
        assert out <= 1.0

    def test_output_clamped_high(self):
        pid = BudgetPIDController(kp=100.0, setpoint_cost_per_min=0.50)
        pid.compute(0.50)
        for _ in range(5):
            time.sleep(0.001)
            pid.compute(0.0)  # huge under-spend
        out = pid.compute(0.0)
        assert out <= 2.0

    def test_output_clamped_low(self):
        pid = BudgetPIDController(kp=100.0, ki=0.0, kd=0.0, setpoint_cost_per_min=0.50)
        pid.compute(0.50)
        for _ in range(5):
            time.sleep(0.001)
            pid.compute(100.0)  # huge over-spend
        out = pid.compute(100.0)
        assert out >= 0.30

    def test_integral_windup_protection(self):
        pid = BudgetPIDController(kp=0.0, ki=1.0, integral_windup_limit=2.0, setpoint_cost_per_min=0.50)
        pid.compute(0.50)
        for _ in range(100):
            pid.compute(0.0)
        assert abs(pid._integral) <= 2.0 + 0.05

    def test_reset_clears_state(self):
        pid = BudgetPIDController(setpoint_cost_per_min=0.50)
        pid.compute(0.10)
        pid.compute(5.00)
        pid.reset()
        assert pid._integral == 0.0
        assert not pid._initialized
        assert len(pid.output_history) == 0

    def test_convergence_toward_setpoint(self):
        pid = BudgetPIDController(kp=1.0, ki=0.05, kd=0.1, setpoint_cost_per_min=0.50)
        pid.compute(0.50)
        outs = []
        for i in range(15):
            time.sleep(0.002)
            pv = 0.80 - i * 0.02  # approaching from above
            outs.append(pid.compute(pv))
        assert all(0.3 <= o <= 2.0 for o in outs)


class TestBudgetActuator:
    """Test 3: Actuation Layer — PID output to parameter mapping."""

    def test_neutral_output_gives_base_values(self):
        act = BudgetActuator()
        adj = act.compute_adjustment(
            pid_output=1.0, cost_rate=0.50,
            setpoint=0.50, trend=SpendingTrend.STABLE,
        )
        assert adj.budget_multiplier == pytest.approx(1.0)
        assert adj.threshold_multiplier == pytest.approx(1.0)

    def test_low_pid_tightens(self):
        act = BudgetActuator()
        adj = act.compute_adjustment(
            pid_output=0.5, cost_rate=2.0,
            setpoint=0.50, trend=SpendingTrend.STABLE,
        )
        assert adj.budget_multiplier < 1.0
        assert adj.threshold_multiplier < 1.0

    def test_high_pid_loosens(self):
        act = BudgetActuator()
        adj = act.compute_adjustment(
            pid_output=1.8, cost_rate=0.10,
            setpoint=0.50, trend=SpendingTrend.STABLE,
        )
        assert adj.budget_multiplier > 1.0
        assert adj.threshold_multiplier > 1.0

    def test_burst_further_tightens(self):
        normal = BudgetActuator().compute_adjustment(
            pid_output=0.7, cost_rate=2.0,
            setpoint=0.50, trend=SpendingTrend.STABLE,
        )
        burst = BudgetActuator().compute_adjustment(
            pid_output=0.7, cost_rate=2.0,
            setpoint=0.50, trend=SpendingTrend.BURST,
        )
        assert burst.threshold_multiplier < normal.threshold_multiplier
        assert burst.budget_multiplier < normal.budget_multiplier

    def test_accelerating_tightens_moderately(self):
        stable = BudgetActuator().compute_adjustment(
            pid_output=0.8, cost_rate=1.5,
            setpoint=0.50, trend=SpendingTrend.STABLE,
        )
        accel = BudgetActuator().compute_adjustment(
            pid_output=0.8, cost_rate=1.5,
            setpoint=0.50, trend=SpendingTrend.ACCELERATING,
        )
        assert accel.threshold_multiplier < stable.threshold_multiplier

    def test_extreme_values_clamped(self):
        act = BudgetActuator()
        lo = act.compute_adjustment(pid_output=0.01, cost_rate=100, setpoint=0.5, trend=SpendingTrend.BURST)
        hi = act.compute_adjustment(pid_output=10.0, cost_rate=0.001, setpoint=0.5, trend=SpendingTrend.DECELERATING)
        assert 0.25 <= lo.threshold_multiplier <= 3.0
        assert 0.25 <= lo.budget_multiplier <= 3.0
        assert 0.25 <= hi.threshold_multiplier <= 3.0
        assert 0.25 <= hi.budget_multiplier <= 3.0

    def test_reason_populated(self):
        act = BudgetActuator()
        adj = act.compute_adjustment(pid_output=0.6, cost_rate=1.0, setpoint=0.5, trend=SpendingTrend.STABLE)
        assert len(adj.reason) > 0


class TestCostControlLoop:
    """Test 4: Full Orchestration — end-to-end cost control cycle."""

    def _make_loop(self) -> CostControlLoop:
        return CostControlLoop(target_cost_per_min=0.50, kp=1.5, ki=0.08, kd=0.2, enabled=True)

    def test_disabled_returns_neutral(self):
        loop = self._make_loop()
        loop.enabled = False
        adj = loop.run(cost_usd=1.0, total_tokens=50000, total_calls=10)
        assert adj.budget_multiplier == 1.0
        assert "disabled" in adj.reason

    def test_enabled_returns_adjustment(self):
        loop = self._make_loop()
        adj = loop.run(cost_usd=1.0, total_tokens=50000, total_calls=10)
        assert isinstance(adj, BudgetAdjustment)
        assert 0.3 <= adj.budget_multiplier <= 2.0

    def test_multiple_cycles_accumulate(self):
        loop = self._make_loop()
        for i in range(8):
            loop.run(cost_usd=float(i) * 0.2, total_tokens=(i+1)*5000, total_calls=i+1)
        stats = loop.get_stats()
        assert stats["cycles_executed"] == 8

    def test_last_reading_updated(self):
        loop = self._make_loop()
        loop.run(cost_usd=0.75, total_tokens=30000, total_calls=5)
        assert loop.last_reading is not None
        assert loop.last_reading.cost_usd == 0.75

    def test_last_adjustment_updated(self):
        loop = self._make_loop()
        loop.run(cost_usd=2.0, total_tokens=80000, total_calls=12)
        assert loop.last_adjustment is not None
        assert loop.last_adjustment.cost_rate > 0

    def test_get_stats_full_hierarchy(self):
        loop = self._make_loop()
        loop.run(cost_usd=0.5, total_tokens=10000, total_calls=3)
        stats = loop.get_stats()
        assert "sensor" in stats
        assert "pid" in stats
        assert "adjustment" in stats
        assert "cycles_executed" in stats

    def test_reset_clears_everything(self):
        loop = self._make_loop()
        loop.run(1.0, 10000, 5)
        loop.run(2.0, 20000, 10)
        loop.reset()
        assert loop._cycle_count == 0
        assert loop._last_adjustment is None
        assert loop._last_reading is None
        assert len(loop.pid.output_history) == 0

    def test_high_cost_tightens_over_time(self):
        loop = self._make_loop()
        mults = []
        for i in range(6):
            adj = loop.run(cost_usd=0.5 + i * 1.0, total_tokens=10000 + i * 10000, total_calls=3+i*2)
            mults.append(adj.budget_multiplier)
        assert any(m < 0.95 for m in mults), f"Expected some tightening: {mults}"


class TestBudgetManagerIntegration:
    """Test 5: Integration with ToolResultBudgetManager."""

    def test_apply_to_budget_manager(self):
        loop = CostControlLoop(target_cost_per_min=0.50, enabled=True)
        cfg = AutoCompactConfig(threshold_ratio=0.85, circuit_breaker_limit=3, session_memory_enabled=False)
        compactor = ContextCompactor(context_window=10000, workspace="/tmp", estimate_fn=estimate_tokens, config=cfg)

        original_threshold = compactor._tool_budget._persist_threshold
        original_budget = compactor._tool_budget._budget

        loop.run(cost_usd=5.0, total_tokens=100000, total_calls=20)
        params = loop.apply_to_budget_manager(compactor._tool_budget)

        assert "persist_threshold" in params or params == {}
        if params:
            assert isinstance(params["persist_threshold"], int)

    def test_disabled_does_not_modify(self):
        loop = CostControlLoop(enabled=False)
        cfg = AutoCompactConfig(threshold_ratio=0.85, circuit_breaker_limit=3, session_memory_enabled=False)
        compactor = ContextCompactor(context_window=10000, workspace="/tmp", estimate_fn=estimate_tokens, config=cfg)

        loop.run(cost_usd=99.0, total_tokens=999999, total_calls=999)
        params = loop.apply_to_budget_manager(compactor._tool_budget)
        assert params == {}

    def test_no_adjustment_returns_empty(self):
        loop = CostControlLoop(enabled=True)
        cfg = AutoCompactConfig(threshold_ratio=0.85, circuit_breaker_limit=3, session_memory_enabled=False)
        compactor = ContextCompactor(context_window=10000, workspace="/tmp", estimate_fn=estimate_tokens, config=cfg)

        params = loop.apply_to_budget_manager(compactor._tool_budget)
        assert params == {}


class TestCostControlE2E:
    """End-to-end: simulate a full session's cost control behavior."""

    def test_session_with_increasing_cost(self):
        loop = CostControlLoop(target_cost_per_min=0.30, kp=2.0, ki=0.1, kd=0.3)
        adjustments = []
        for turn in range(10):
            cost = 0.1 + turn * 0.3
            tokens = 5000 + turn * 8000
            calls = 2 + turn
            adj = loop.run(cost_usd=cost, total_tokens=tokens, total_calls=calls)
            adjustments.append(adj.budget_multiplier)

        assert len(adjustments) == 10
        assert loop.get_stats()["cycles_executed"] == 10

    def test_session_with_stable_low_cost(self):
        loop = CostControlLoop(target_cost_per_min=1.0, kp=1.5, ki=0.08, kd=0.2)
        for _ in range(8):
            loop.run(cost_usd=0.05, total_tokens=2000, total_calls=1)
        stats = loop.get_stats()
        adj = stats.get("adjustment")
        if adj:
            assert adj["budget_mult"] >= 1.0, "Low cost should loosen budget"

    def test_burst_then_recovery(self):
        loop = CostControlLoop(target_cost_per_min=0.50)
        burst_adj = loop.run(cost_usd=10.0, total_tokens=200000, total_calls=50)
        recover_adj = loop.run(cost_usd=0.1, total_tokens=2000, total_calls=1)
        assert burst_adj.budget_multiplier < recover_adj.budget_multiplier

    def test_full_pipeline_with_compactor(self):
        cfg = AutoCompactConfig(threshold_ratio=0.85, circuit_breaker_limit=3, session_memory_enabled=False)
        compactor = ContextCompactor(context_window=5000, workspace="/tmp", estimate_fn=estimate_tokens, config=cfg)
        loop = CostControlLoop(target_cost_per_min=0.50, enabled=True)

        msgs = [{"role": "user", "content": "x" * 40} for _ in range(30)]

        loop.run(cost_usd=3.0, total_tokens=sum(estimate_tokens(m) for m in msgs), total_calls=15)
        loop.apply_to_budget_manager(compactor._tool_budget)

        result = compactor.process_request(msgs, enable_tool_budget=True, enable_auto_compact=False)
        assert result is not None
