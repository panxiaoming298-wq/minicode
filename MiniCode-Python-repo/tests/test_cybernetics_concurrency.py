"""Concurrent stress + fuzz tests for cybernetics controllers.

Verifies that controllers handle concurrent access without race conditions,
and that PID/Kalman filters are robust against edge-case inputs.
"""
from __future__ import annotations

import math
import threading
import time

import pytest

from minicode.feedback_controller import FeedbackController, PIDController, SystemState
from minicode.state_observer import KalmanFilter, MeasurementVector, StateObserver
from minicode.context_cybernetics import ContextPIDController


# ── CONCURRENT STRESS ────────────────────────────────────────────────

class TestConcurrentFeedbackController:
    """Multiple threads calling observe() concurrently."""

    def test_concurrent_observe(self):
        fc = FeedbackController()
        errors = []

        def worker(thread_id: int):
            try:
                for _ in range(50):
                    state = SystemState(
                        success_rate=0.7 + thread_id * 0.05,
                        error_frequency=0.1 + thread_id * 0.02,
                    )
                    fc.observe(state)
                    fc.record_pattern_effectiveness(f"pat_{thread_id}", True)
            except Exception as e:
                errors.append(f"thread {thread_id}: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent errors: {errors}"

    def test_concurrent_pid_compute(self):
        pid = PIDController(kp=1.5, ki=0.2, kd=0.1)
        errors = []

        def worker():
            try:
                for i in range(100):
                    pid.compute(setpoint=0.8, measured=0.5 + i * 0.01, dt=0.1)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_concurrent_kalman_update(self):
        kf = KalmanFilter(process_noise=0.01, measurement_noise=0.1)
        errors = []

        def worker():
            try:
                for _ in range(100):
                    kf.update(measurement=0.5)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ── FUZZ TESTS ───────────────────────────────────────────────────────

class TestPIDEdgeCases:
    """PID controller with extreme inputs."""

    def test_zero_dt(self):
        pid = PIDController(kp=1.0, ki=0.1, kd=0.1)
        # dt is capped at 0.001 internally
        out = pid.compute(setpoint=1.0, measured=0.0, dt=0.0)
        assert isinstance(out, float)

    def test_negative_dt(self):
        pid = PIDController(kp=1.0, ki=0.1, kd=0.1)
        out = pid.compute(setpoint=1.0, measured=0.0, dt=-1.0)
        assert isinstance(out, float)

    def test_very_large_error(self):
        pid = PIDController(kp=1.0, ki=0.1, kd=0.1)
        out = pid.compute(setpoint=0.0, measured=1e9, dt=1.0)
        # Should be clamped by output limits
        assert -1.0 <= out <= 1.0

    def test_very_large_setpoint(self):
        pid = PIDController(kp=1.0, ki=0.1, kd=0.1)
        out = pid.compute(setpoint=1e9, measured=0.0, dt=1.0)
        assert isinstance(out, float)

    def test_zero_gains(self):
        pid = PIDController(kp=0.0, ki=0.0, kd=0.0)
        out = pid.compute(setpoint=1.0, measured=0.5, dt=1.0)
        assert out == 0.0

    def test_saturation_detection(self):
        pid = PIDController(kp=100.0, ki=0.0, kd=0.0, output_min=-0.3, output_max=0.3)
        for _ in range(5):
            out = pid.compute(setpoint=1.0, measured=0.0, dt=1.0)
        # Output should be clamped at max
        assert out == 0.3


class TestContextPIDEdgeCases:
    """ContextPIDController edge cases."""

    def test_zero_usage_first_call(self):
        pid = ContextPIDController(kp=2.0, ki=0.15, kd=0.3)
        out = pid.compute(process_variable=0.0)
        assert out == 0.0  # First call returns 0

    def test_below_setpoint_negative_output(self):
        pid = ContextPIDController(kp=2.0, ki=0.15, kd=0.3)
        pid.compute(process_variable=0.5)
        out = pid.compute(process_variable=0.3)
        # Below setpoint(0.70) — should not push for compaction
        assert out <= 0.0

    def test_above_setpoint_positive_output(self):
        pid = ContextPIDController(kp=2.0, ki=0.15, kd=0.3)
        pid.compute(process_variable=0.5)
        out = pid.compute(process_variable=0.95)
        # Above setpoint(0.70) — should push for compaction
        assert out > 0.0


class TestKalmanEdgeCases:
    """Kalman filter with extreme noise conditions."""

    def test_zero_measurement_noise(self):
        kf = KalmanFilter(process_noise=0.01, measurement_noise=0.0)
        est = kf.update(measurement=0.5)
        assert 0.0 <= est <= 1.0

    def test_zero_process_noise(self):
        kf = KalmanFilter(process_noise=0.0, measurement_noise=0.1)
        est = kf.update(measurement=0.5)
        assert 0.0 <= est <= 1.0

    def test_extreme_measurement_noise(self):
        kf = KalmanFilter(process_noise=0.01, measurement_noise=1e6)
        est = kf.update(measurement=0.5)
        assert isinstance(est, float)

    def test_convergence(self):
        kf = KalmanFilter(
            process_noise=0.01,
            measurement_noise=0.1,
            initial_estimate=0.0,
            initial_uncertainty=1.0,
        )
        for _ in range(20):
            kf.update(measurement=0.8)
        # After many measurements, estimate should be near 0.8
        final = kf.update(measurement=0.8)
        assert abs(final - 0.8) < 0.3

    def test_sudden_jump(self):
        kf = KalmanFilter(process_noise=0.1, measurement_noise=0.1)
        for _ in range(10):
            kf.update(measurement=0.3)
        # Sudden jump
        est = kf.update(measurement=0.9)
        assert 0.3 < est < 0.9  # Kalman should move toward new value


class TestStateObserverEdgeCases:
    """Full StateObserver with extreme inputs."""

    def test_all_zero_measurement(self):
        observer = StateObserver()
        m = MeasurementVector(timestamp=time.time(), response_time=0.0,
                              success_rate=0.0, context_length=0,
                              error_count=0, tool_calls=0)
        state = observer.update(m)
        assert state.confidence >= 0.0

    def test_perfect_measurement(self):
        observer = StateObserver()
        m = MeasurementVector(timestamp=time.time(), response_time=1.0,
                              success_rate=1.0, context_length=100,
                              error_count=0, tool_calls=5)
        state = observer.update(m)
        assert 0.0 <= state.internal_load <= 1.0
        assert 0.0 <= state.system_degradation <= 1.0

    def test_multiple_updates_converge(self):
        observer = StateObserver()
        for _ in range(30):
            m = MeasurementVector(
                timestamp=time.time(), response_time=2.0,
                success_rate=0.9, context_length=500,
                error_count=1, tool_calls=3,
            )
            observer.update(m)
        state = observer.update(m)
        assert state.confidence > 0.3  # Should gain confidence with more data
