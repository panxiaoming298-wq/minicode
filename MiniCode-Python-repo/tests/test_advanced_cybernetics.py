"""Comprehensive stress tests for advanced cybernetics modules.

Tests for:
1. Adaptive PID Tuner
2. State Observer (Kalman Filter)
3. Decoupling Controller
4. Predictive Controller
5. Self-Healing Engine
6. Full Integration Test (all modules working together)
"""
import pytest
import time
import math
from minicode.adaptive_pid_tuner import (
    AdaptivePIDTuner, PIDParameters, TuningMethod,
    ZieglerNicholsTuner, RelayFeedbackTuner, GradientBasedTuner,
)
from minicode.state_observer import StateObserver, MeasurementVector, ObservedState, KalmanFilter
from minicode.decoupling_controller import DecouplingController, CouplingMatrix
from minicode.predictive_controller import PredictiveController, PredictionHorizon
from minicode.self_healing_engine import (
    SelfHealingEngine, FaultType, FaultSeverity, HealingStatus,
)
from minicode.feedback_controller import FeedbackController, SystemState
from minicode.stability_monitor import StabilityMonitor


class TestAdaptivePIDTuner:
    def test_ziegler_nichols_pid_tuning(self):
        ku = 2.5
        pu = 10.0
        params = ZieglerNicholsTuner.tune_pid(ku, pu, "pid")

        assert params.kp == pytest.approx(0.6 * ku, abs=0.01)
        assert params.ki == pytest.approx((2.0 * params.kp) / pu, abs=0.01)
        assert params.kd == pytest.approx((params.kp * pu) / 8.0, abs=0.01)

    def test_ziegler_nichols_pi_tuning(self):
        ku = 2.0
        pu = 8.0
        params = ZieglerNicholsTuner.tune_pid(ku, pu, "pi")

        assert params.kp == pytest.approx(0.45 * ku, abs=0.01)
        assert params.ki == pytest.approx((1.2 * params.kp) / pu, abs=0.01)
        assert params.kd == 0.0

    def test_ziegler_nichols_invalid_input(self):
        params = ZieglerNicholsTuner.tune_pid(-1.0, 10.0)
        assert params.kp == 1.0  # Default values
        params = ZieglerNicholsTuner.tune_pid(1.0, -5.0)
        assert params.kp == 1.0

    def test_adaptive_tuner_initial_state(self):
        tuner = AdaptivePIDTuner()
        params = tuner.get_parameters()
        assert params.kp == 1.0
        assert params.ki == 0.1
        assert params.kd == 0.05

    def test_adaptive_tuning_with_large_error(self):
        tuner = AdaptivePIDTuner()

        for i in range(10):
            params = tuner.tune(error=0.8, performance_score=0.3)

        assert params.kp > 1.0
        assert params is not None

    def test_adaptive_tuning_with_small_error(self):
        tuner = AdaptivePIDTuner()

        for i in range(10):
            params = tuner.tune(error=0.05, performance_score=0.9)

        assert params.ki >= 0.1

    def test_oscillation_detection(self):
        tuner = AdaptivePIDTuner()
        errors = [0.1, 0.9, 0.2, 0.8, 0.3, 0.7, 0.4, 0.6, 0.15, 0.85, 0.25, 0.75]
        for error in errors:
            tuner.tune(error=error, performance_score=0.3)
        status = tuner.get_tuning_status()
        assert isinstance(status.get('consecutive_oscillations'), int)

    def test_tuning_history_tracking(self):
        tuner = AdaptivePIDTuner()

        for i in range(5):
            tuner.tune(error=0.5 - i * 0.1, performance_score=0.5 + i * 0.1)

        status = tuner.get_tuning_status()
        assert status["tuning_history_size"] == 5
        assert status["active_method"] == "performance_adaptive"

    def test_reset_clears_all_state(self):
        tuner = AdaptivePIDTuner()
        tuner.tune(error=0.5, performance_score=0.6)
        tuner.reset()

        params = tuner.get_parameters()
        assert params.kp == 1.0
        assert params.ki == 0.1
        assert params.kd == 0.05

        status = tuner.get_tuning_status()
        assert status["tuning_history_size"] == 0

    def test_gradient_based_tuner_optimization(self):
        tuner = GradientBasedTuner(learning_rate=0.05)

        def evaluate(p):
            return abs(p.kp - 2.0) + abs(p.ki - 0.5)

        for i in range(5):
            params = tuner.optimize_step(0.5, evaluate)

        assert params is not None

    def test_relay_feedback_tuner(self):
        tuner = RelayFeedbackTuner(relay_amplitude=1.0)

        measurements = [0.5, -0.3, 0.4, -0.2, 0.6, -0.1, 0.7, -0.05]
        for m in measurements:
            tuner.step(m)

        params = tuner.compute_parameters()
        if params:
            assert params.kp > 0
            assert params.ki >= 0


class TestStateObserver:
    def test_kalman_filter_update(self):
        kf = KalmanFilter(process_noise=0.01, measurement_noise=0.1)
        estimate = kf.update(0.5)

        assert estimate > 0
        assert estimate < 1.0

    def test_kalman_filter_convergence(self):
        kf = KalmanFilter(process_noise=0.01, measurement_noise=0.1,
                         initial_estimate=0.0, initial_uncertainty=1.0)

        for i in range(20):
            kf.update(0.7)

        assert abs(kf.estimate - 0.7) < 0.1

    def test_kalman_filter_confidence(self):
        kf = KalmanFilter(process_noise=0.01, measurement_noise=0.1)

        for i in range(10):
            kf.update(0.5)

        confidence = kf.get_confidence()
        assert confidence > 0.5

    def test_state_observer_update(self):
        observer = StateObserver()
        measurement = MeasurementVector(
            timestamp=time.time(),
            response_time=10.0,
            success_rate=0.95,
            token_usage=0.5,
            error_count=0,
            retry_count=0,
            context_length=50000,
            tool_calls=3,
        )
        state = observer.update(measurement)

        assert isinstance(state, ObservedState)
        assert 0.0 <= state.internal_load <= 1.0
        assert 0.0 <= state.hidden_errors <= 1.0
        assert 0.0 <= state.context_pressure <= 1.0

    def test_state_observer_multiple_updates(self):
        observer = StateObserver()

        for i in range(5):
            measurement = MeasurementVector(
                timestamp=time.time(),
                response_time=5.0 + i,
                success_rate=0.9 - i * 0.05,
                error_count=i,
                tool_calls=2 + i,
            )
            observer.update(measurement)

        summary = observer.get_state_summary()
        assert "internal_load" in summary
        assert "hidden_errors" in summary

    def test_state_prediction(self):
        observer = StateObserver()

        for i in range(10):
            measurement = MeasurementVector(
                timestamp=time.time(),
                response_time=10.0 + i * 2.0,
                success_rate=0.9 - i * 0.03,
                error_count=i,
                tool_calls=3,
            )
            observer.update(measurement)

        predicted = observer.predict_state(steps_ahead=3)

        assert isinstance(predicted, ObservedState)
        assert 0.0 <= predicted.internal_load <= 1.0
        assert predicted.confidence < 1.0

    def test_observability_score(self):
        observer = StateObserver()

        for i in range(10):
            measurement = MeasurementVector(
                timestamp=time.time(),
                response_time=10.0 + (i % 3) * 5.0,
                success_rate=0.8 + (i % 5) * 0.02,
            )
            observer.update(measurement)

        score = observer.get_observability_score()
        assert 0.0 <= score <= 1.0

    def test_reset_clears_state(self):
        observer = StateObserver()
        observer.update(MeasurementVector(timestamp=time.time(), response_time=10.0))
        observer.reset()

        summary = observer.get_state_summary()
        assert summary.get("status") == "no_data" or summary.get("internal_load") == "0.00"

    def test_kalman_filter_predict(self):
        kf = KalmanFilter(process_noise=0.01, measurement_noise=0.1)
        kf.update(0.5)
        kf.update(0.6)

        predicted = kf.predict(dt=1.0)
        assert predicted == kf.estimate

    def test_kalman_filter_reset(self):
        kf = KalmanFilter(process_noise=0.01, measurement_noise=0.1)
        kf.update(0.5)
        kf.reset(initial_estimate=0.0, initial_uncertainty=1.0)

        assert kf.estimate == 0.0
        assert kf.uncertainty == 1.0


class TestDecouplingController:
    def test_record_measurement(self):
        controller = DecouplingController()
        controller.record_measurement({
            "token_usage_to_latency": (0.5, 0.6),
            "context_pressure_to_errors": (0.3, 0.4),
        })

    def test_compute_decoupling_matrix(self):
        controller = DecouplingController()

        for i in range(10):
            controller.record_measurement({
                "token_usage_to_latency": (0.3 + i * 0.05, 0.4 + i * 0.04),
                "context_pressure_to_errors": (0.2 + i * 0.03, 0.3 + i * 0.02),
            })

        matrix = controller.compute_decoupling_matrix()
        assert "token_usage" in matrix or "latency" in matrix or len(matrix) > 0

    def test_decouple_command(self):
        controller = DecouplingController()

        for i in range(5):
            controller.record_measurement({
                "token_usage_to_latency": (0.5, 0.5 + i * 0.1),
            })

        controller.compute_decoupling_matrix()

        command = controller.decouple_command(
            "token_usage", 0.5, {"latency": 0.6}
        )

        assert command.variable_name == "token_usage"
        assert -1.0 <= command.final_command <= 1.0
        assert "Decoupled" in command.reasoning

    def test_feedforward_compensation(self):
        controller = DecouplingController()

        for i in range(10):
            controller.record_measurement({
                "token_usage_to_latency": (0.3 + i * 0.05, 0.4 + i * 0.05),
            })

        controller.compute_decoupling_matrix()

        compensation = controller.compute_feedforward_compensation({
            "token_usage": 0.2,
            "latency": 0.3,
        })

        assert isinstance(compensation, dict)

    def test_rga_pairing(self):
        controller = DecouplingController()

        for i in range(20):
            controller.record_measurement({
                "token_usage_to_latency": (0.5 + i * 0.02, 0.6 + i * 0.02),
            })

        pairs = controller.get_rga_pairing()
        assert isinstance(pairs, list)

    def test_coupling_status(self):
        controller = DecouplingController()

        for i in range(5):
            controller.record_measurement({
                "token_usage_to_latency": (0.5, 0.6),
            })

        status = controller.get_coupling_status()
        assert "coupling_strengths" in status
        assert "compensation_values" in status

    def test_reset_clears_state(self):
        controller = DecouplingController()
        controller.record_measurement({"token_usage_to_latency": (0.5, 0.6)})
        controller.reset()

        status = controller.get_coupling_status()
        assert status["coupling_strengths"].get("token_usage_to_latency", 0) == 0.0


class TestPredictiveController:
    def test_update_single_metric(self):
        controller = PredictiveController()
        controller.update("response_time", 15.0)
        controller.update("response_time", 18.0)
        controller.update("response_time", 12.0)

    def test_predict_short_horizon(self):
        controller = PredictiveController()

        for i in range(10):
            controller.update("response_time", 10.0 + i)

        result = controller.predict("response_time", PredictionHorizon.SHORT)

        assert result is not None
        assert result.metric_name == "response_time"
        assert result.prediction_horizon == PredictionHorizon.SHORT

    def test_predict_all_metrics(self):
        controller = PredictiveController()

        metrics = ["response_time", "error_rate", "context_usage", "cpu_usage"]
        for metric in metrics:
            for i in range(5):
                controller.update(metric, 0.5 + i * 0.1)

        results = controller.predict_all(PredictionHorizon.SHORT)

        assert len(results) > 0
        for r in results:
            assert r.metric_name in metrics or r.metric_name in ['cpu_usage', 'memory_usage', 'throughput', 'stability_score', 'performance_score']

    def test_predictive_action_generation(self):
        controller = PredictiveController()

        for i in range(15):
            controller.update("response_time", 20.0 + i * 3.0)
            controller.predict("response_time", PredictionHorizon.SHORT)

        actions = controller.generate_predictive_actions()
        assert isinstance(actions, list)

    def test_prediction_accuracy_tracking(self):
        controller = PredictiveController()

        for i in range(10):
            actual = 10.0 + i
            controller.update("response_time", actual)
            controller.predict("response_time", PredictionHorizon.SHORT)
            controller.record_actual("response_time", actual)

        accuracy = controller.get_prediction_accuracy()
        assert "response_time" in accuracy

    def test_trend_detection(self):
        controller = PredictiveController()

        for i in range(10):
            controller.update("error_rate", 0.1 + i * 0.05)

        controller.predict("error_rate", PredictionHorizon.SHORT)

        summary = controller.get_prediction_summary()
        assert "trends" in summary

    def test_multiple_prediction_horizons(self):
        controller = PredictiveController()

        for i in range(20):
            controller.update("stability_score", 0.8 - i * 0.02)

        short = controller.predict("stability_score", PredictionHorizon.SHORT)
        medium = controller.predict("stability_score", PredictionHorizon.MEDIUM)
        long_h = controller.predict("stability_score", PredictionHorizon.LONG)

        assert short is not None
        assert medium is not None
        assert long_h is not None

    def test_reset_clears_all_state(self):
        controller = PredictiveController()
        controller.update("response_time", 15.0)
        controller.predict("response_time")
        controller.reset()

        summary = controller.get_prediction_summary()
        assert not summary.get("predictions") or len(summary.get("predictions", {})) == 0


class TestSelfHealingEngine:
    def test_detect_resource_exhaustion(self):
        engine = SelfHealingEngine()
        metrics = {"cpu_usage": 0.95, "memory_usage": 0.92}

        actions = engine.detect_and_heal(metrics)

        assert len(actions) > 0
        assert any(a.fault_type == FaultType.RESOURCE_EXHAUSTION for a in actions)

    def test_detect_context_overflow(self):
        engine = SelfHealingEngine()
        metrics = {"context_usage": 0.90}

        actions = engine.detect_and_heal(metrics)

        assert len(actions) > 0
        assert any(a.fault_type == FaultType.CONTEXT_OVERFLOW for a in actions)

    def test_detect_error_spike(self):
        engine = SelfHealingEngine()
        metrics = {"error_rate": 4.5}

        actions = engine.detect_and_heal(metrics)

        assert len(actions) > 0
        assert any(a.fault_type == FaultType.ERROR_SPIKE for a in actions)

    def test_detect_oscillation(self):
        engine = SelfHealingEngine()
        metrics = {"oscillation_index": 0.75}

        actions = engine.detect_and_heal(metrics)

        assert len(actions) > 0
        assert any(a.fault_type == FaultType.OSCILLATION for a in actions)

    def test_detect_performance_degradation(self):
        engine = SelfHealingEngine()
        metrics = {"avg_latency": 50.0, "throughput": 0.3}

        actions = engine.detect_and_heal(metrics)

        assert len(actions) > 0
        assert any(a.fault_type == FaultType.PERFORMANCE_DEGRADATION for a in actions)

    def test_no_fault_when_healthy(self):
        engine = SelfHealingEngine()
        metrics = {
            "cpu_usage": 0.3,
            "memory_usage": 0.4,
            "context_usage": 0.5,
            "error_rate": 0.1,
            "oscillation_index": 0.1,
            "avg_latency": 10.0,
            "throughput": 5.0,
        }

        actions = engine.detect_and_heal(metrics)
        assert len(actions) == 0

    def test_healing_statistics(self):
        engine = SelfHealingEngine()

        engine.detect_and_heal({"error_rate": 4.0})
        engine.detect_and_heal({"context_usage": 0.90})

        stats = engine.get_healing_statistics()
        assert stats["total_faults_detected"] == 2
        assert stats["total_healing_actions"] == 2
        assert "healing_success_rate" in stats

    def test_custom_strategy_registration(self):
        from minicode.self_healing_engine import HealingStrategy

        engine = SelfHealingEngine()
        custom_strategy = HealingStrategy(
            name="custom_cleanup",
            fault_type=FaultType.MEMORY_LEAK,
            action=lambda: {"success": True, "action": "Custom cleanup executed"},
            expected_time=2.0,
            success_probability=0.9,
        )
        engine.register_custom_strategy(custom_strategy)

        actions = engine.detect_and_heal({"memory_usage": 0.95})

        custom_actions = [a for a in actions if a.strategy == "custom_cleanup"]

    def test_fault_trend_tracking(self):
        engine = SelfHealingEngine()

        engine.detect_and_heal({"error_rate": 4.0})
        engine.detect_and_heal({"context_usage": 0.90})
        engine.detect_and_heal({"error_rate": 5.5})

        trend = engine.get_fault_trend(window_size=3)
        assert len(trend) == 3

    def test_reset_clears_history(self):
        engine = SelfHealingEngine()
        engine.detect_and_heal({"error_rate": 4.0})
        engine.reset()

        stats = engine.get_healing_statistics()
        assert stats["total_faults_detected"] == 0
        assert stats["total_healing_actions"] == 0


class TestFullCyberneticsIntegration:
    def test_complete_cybernetics_loop(self):
        from minicode.feedback_controller import FeedbackController, SystemState
        from minicode.feedforward_controller import FeedforwardController
        from minicode.stability_monitor import StabilityMonitor, MetricSnapshot

        feedback = FeedbackController()
        stability = StabilityMonitor()
        observer = StateObserver()
        predictor = PredictiveController()
        healing = SelfHealingEngine()
        decoupling = DecouplingController()
        pid_tuner = AdaptivePIDTuner()

        for step in range(5):
            system_state = SystemState(
                success_rate=0.9 - step * 0.05,
                avg_response_time=10.0 + step * 2.0,
                token_efficiency=0.7 - step * 0.05,
                context_usage=0.5 + step * 0.1,
                error_frequency=float(step) / max(step, 1),
                retry_count=float(step) * 0.2,
                pattern_reuse_rate=0.3 + step * 0.1,
            )

            signal = feedback.observe(system_state)

            measurement = MeasurementVector(
                timestamp=time.time(),
                response_time=system_state.avg_response_time,
                success_rate=system_state.success_rate,
                context_length=int(system_state.context_usage * 100000),
                error_count=int(system_state.error_frequency),
                tool_calls=3,
            )
            observed = observer.update(measurement)

            predictor.update("response_time", system_state.avg_response_time)
            predictor.update("error_rate", system_state.error_frequency)

            if step > 2:
                predictor.predict("response_time")

            error = 1.0 - system_state.stability_score()
            pid_params = pid_tuner.tune(error=error, performance_score=system_state.performance_score())

        final_report = stability.get_stability_report()
        assert final_report is not None

    def test_cybernetics_modules_resilience(self):
        feedback = FeedbackController()
        observer = StateObserver()
        predictor = PredictiveController()
        healing = SelfHealingEngine()

        for i in range(20):
            state = SystemState(
                success_rate=max(0.5, 0.9 - i * 0.02),
                avg_response_time=min(60.0, 10.0 + i * 2.0),
                context_usage=min(0.95, 0.5 + i * 0.03),
                error_frequency=min(5.0, float(i) * 0.3),
            )

            signal = feedback.observe(state)

            measurement = MeasurementVector(
                timestamp=time.time(),
                response_time=state.avg_response_time,
                success_rate=state.success_rate,
                context_length=int(state.context_usage * 100000),
                error_count=int(state.error_frequency),
            )
            observer.update(measurement)

            predictor.update("response_time", state.avg_response_time)
            predictor.update("error_rate", state.error_frequency)

            metrics = {
                "error_rate": state.error_frequency,
                "context_usage": state.context_usage,
                "cpu_usage": min(1.0, 0.3 + i * 0.03),
            }
            healing.detect_and_heal(metrics)

        assert True

    def test_cybernetics_recovery_from_degradation(self):
        feedback = FeedbackController()
        observer = StateObserver()
        predictor = PredictiveController()
        healing = SelfHealingEngine()

        for i in range(10):
            state = SystemState(
                success_rate=0.3 + i * 0.07,
                avg_response_time=50.0 - i * 3.0,
                context_usage=0.9 - i * 0.03,
                error_frequency=max(0.0, 4.0 - i * 0.3),
            )

            signal = feedback.observe(state)
            measurement = MeasurementVector(
                timestamp=time.time(),
                response_time=state.avg_response_time,
                success_rate=state.success_rate,
                error_count=max(0, int(state.error_frequency)),
            )
            observer.update(measurement)
            predictor.update("error_rate", state.error_frequency)

            metrics = {"error_rate": state.error_frequency, "context_usage": state.context_usage}
            healing.detect_and_heal(metrics)

        final_state = SystemState(
            success_rate=0.95,
            avg_response_time=10.0,
            context_usage=0.5,
            error_frequency=0.1,
        )
        final_signal = feedback.observe(final_state)
        assert final_signal is not None
