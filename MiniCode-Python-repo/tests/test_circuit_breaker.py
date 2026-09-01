"""Tests for minicode.circuit_breaker — compaction failure protection."""

from __future__ import annotations

import threading

import pytest

from minicode.circuit_breaker import (
    CircuitBreakerConfig,
    CompactionCircuitBreaker,
    get_compaction_circuit_breaker,
)


# ── Basic state machine ──────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_initial_state(self) -> None:
        cb = CompactionCircuitBreaker()
        assert cb.is_allowed() is True
        state = cb.get_state()
        assert state.consecutive_failures == 0
        assert state.is_open is False

    def test_single_failure_does_not_open(self) -> None:
        cb = CompactionCircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        assert cb.is_allowed() is True
        assert cb.get_state().consecutive_failures == 1

    def test_three_failures_opens_breaker(self) -> None:
        cb = CompactionCircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_allowed() is False
        state = cb.get_state()
        assert state.is_open is True
        assert state.consecutive_failures == 3

    def test_success_resets_counter(self) -> None:
        cb = CompactionCircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.get_state().consecutive_failures == 0
        assert cb.is_allowed() is True

    def test_manual_reset(self) -> None:
        cb = CompactionCircuitBreaker(CircuitBreakerConfig(failure_threshold=2))
        cb.record_failure()
        cb.record_failure()
        assert cb.is_allowed() is False
        cb.reset()
        assert cb.is_allowed() is True
        assert cb.get_state().consecutive_failures == 0

    def test_auto_reset_after_timeout(self) -> None:
        import time

        cb = CompactionCircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=2,
                auto_reset_seconds=0.01,  # very short for testing
            )
        )
        cb.record_failure()
        cb.record_failure()
        assert cb.is_allowed() is False

        time.sleep(0.05)  # wait for auto-reset
        assert cb.is_allowed() is True

    def test_success_counter(self) -> None:
        cb = CompactionCircuitBreaker()
        cb.record_success()
        cb.record_success()
        state = cb.get_state()
        assert state.total_successes == 2

    def test_total_failure_counter(self) -> None:
        cb = CompactionCircuitBreaker()
        cb.record_failure()
        cb.record_failure()
        assert cb.get_state().total_failures == 2


# ── Singleton ────────────────────────────────────────────────────────────

class TestSingleton:
    def test_get_breaker_returns_same(self) -> None:
        cb1 = get_compaction_circuit_breaker()
        cb2 = get_compaction_circuit_breaker()
        assert cb1 is cb2


# ── Thread safety ────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_failures(self) -> None:
        cb = CompactionCircuitBreaker(CircuitBreakerConfig(failure_threshold=50))
        results: list[Exception | None] = []

        def fail_10_times() -> None:
            try:
                for _ in range(10):
                    cb.record_failure()
                results.append(None)
            except Exception as e:
                results.append(e)

        threads = [threading.Thread(target=fail_10_times) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r is None for r in results), f"Thread errors: {results}"
        assert cb.get_state().total_failures == 50

    def test_concurrent_reset_and_record(self) -> None:
        cb = CompactionCircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_allowed() is False

        results: list[Exception | None] = []

        def resetter() -> None:
            try:
                cb.reset()
                results.append(None)
            except Exception as e:
                results.append(e)

        def failer() -> None:
            try:
                cb.record_failure()
                results.append(None)
            except Exception as e:
                results.append(e)

        t1 = threading.Thread(target=resetter)
        t2 = threading.Thread(target=failer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert all(r is None for r in results)
