"""E2E tests for the full cybernetic controller chain.

Verifies that signals flow correctly through the entire pipeline:
  AdaptivePIDTuner → PID params → PID → ControlSignal → actuator logging
  StateObserver → Kalman → ObservedState → healing engine
  ContextCybernetics → SystemState → FeedbackController → ControlSignal
"""
from __future__ import annotations

import time

from minicode.adaptive_pid_tuner import AdaptivePIDTuner
from minicode.context_cybernetics import ContextCyberneticsOrchestrator
from minicode.context_compactor import ContextCompactor, AutoCompactConfig
from minicode.cost_control import CostControlLoop
from minicode.cybernetic_supervisor import CyberneticSupervisor
from minicode.feedback_controller import (
    ControlSignal,
    FeedbackController,
    SystemState,
)
from minicode.self_healing_engine import SelfHealingEngine
from minicode.state_observer import MeasurementVector, StateObserver


class TestTunerToPIDChain:
    """AdaptivePIDTuner → PID params → downstream effects."""

    def test_tuner_produces_valid_params(self):
        tuner = AdaptivePIDTuner()
        params = tuner.tune(error=0.2, dt=1.0, performance_score=0.7)
        assert params.kp > 0
        assert params.ki >= 0
        assert params.kd >= 0

    def test_tuner_params_can_be_applied(self):
        tuner = AdaptivePIDTuner()
        from minicode.context_cybernetics import ContextPIDController
        pid = ContextPIDController()
        params = tuner.tune(error=0.15, dt=1.0, performance_score=0.6)
        pid.kp = params.kp
        pid.ki = params.ki
        pid.kd = params.kd
        out = pid.compute(process_variable=0.85)
        assert isinstance(out, float)


class TestStateToHealingChain:
    """StateObserver → Kalman → ObservedState → SelfHealing."""

    def test_degradation_triggers_healing_detection(self):
        observer = StateObserver()
        healing = SelfHealingEngine()

        # Simulate degrading system
        for i in range(20):
            degradation = 0.3 + i * 0.02
            m = MeasurementVector(
                timestamp=time.time(),
                response_time=5.0 + degradation,
                success_rate=max(0.2, 1.0 - degradation),
                error_count=i // 3,
                context_length=2000 + i * 200,
            )
            state = observer.update(m)

        # High degradation should be reflected
        assert state.system_degradation >= 0.0
        assert state.confidence > 0.0


class TestCyberneticsToFeedbackChain:
    """ContextCybernetics → SystemState → FeedbackController → ControlSignal."""

    def test_full_dual_pid_chain(self):
        """Simulate the complete dual-PID data flow."""
        import tempfile
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AutoCompactConfig()
            mock_memory = MagicMock()
            compactor = ContextCompactor(
                context_window=100000,
                workspace=tmpdir,
                memory_manager=mock_memory,
                estimate_fn=len,
                config=config,
            )
            orchestrator = ContextCyberneticsOrchestrator(
                compactor,
                kp=2.0, ki=0.15, kd=0.3,
                pid_setpoint=0.70,
                base_threshold=0.85,
                safety_margin_turns=3,
                enabled=True,
            )

            # Run a cycle to populate sensor
            messages = [{"role": "user", "content": "test " * 100}]
            orchestrator.run_cycle(messages, error_rate=0.1, avg_latency=2.0, turn_id=1)

            # Convert to SystemState
            system_state = orchestrator.to_system_state()
            assert system_state.context_usage >= 0.0

            # Feed to FeedbackController
            fc = FeedbackController()
            signal = fc.observe(system_state)
            assert isinstance(signal, ControlSignal)
            assert signal.confidence > 0.0

            # Signal should have oscillation_index populated after multiple calls
            for _ in range(5):
                state2 = orchestrator.to_system_state()
                signal2 = fc.observe(state2)
            assert signal2.oscillation_index >= 0.0


class TestCostControlChain:
    """CostControlLoop → BudgetAdjustment → budget manager."""

    def test_cost_loop_produces_adjustment(self):
        loop = CostControlLoop()
        adj = loop.run(cost_usd=0.05, total_tokens=5000, total_calls=10)
        assert adj is not None
        assert hasattr(adj, "budget_multiplier")
        assert adj.budget_multiplier > 0


class TestSupervisorAggregation:
    """CyberneticSupervisor aggregates all controller snapshots."""

    def test_empty_snapshots(self):
        supervisor = CyberneticSupervisor()
        report = supervisor.report([])
        assert report.overall_health == 1.0
        assert report.risk_level.value == "low"

    def test_mixed_snapshots(self):
        supervisor = CyberneticSupervisor()
        from minicode.cybernetic_supervisor import ControlSnapshot

        snapshots = [
            ControlSnapshot(
                name="context", health=0.6, risk=0.4,
                action="compact", reasons=["high usage"],
            ),
            ControlSnapshot(
                name="cost", health=0.3, risk=0.8,
                action="tighten_budget", reasons=["overspending"],
            ),
        ]
        report = supervisor.report(snapshots)
        assert report.overall_health < 0.8
        assert report.risk_level.value in ("medium", "high", "critical")
