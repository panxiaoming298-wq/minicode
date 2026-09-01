"""Circuit breaker for compaction failures.

Prevents infinite retry loops when consecutive compaction attempts fail.
Inspired by Claude Code's auto-compact circuit breaker (max 3 consecutive
failures → permanently block until manually reset).

Design:
  - Counter increments on each compaction failure
  - At threshold (default 3), compaction is blocked
  - Reset can happen manually or on any successful compaction
  - Blocked state is sticky until explicit reset
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class CircuitBreakerConfig:
    """Configuration for the compaction circuit breaker."""

    # Number of consecutive failures before the breaker opens.
    failure_threshold: int = 3

    # After the breaker opens, how long to wait before auto-resetting
    # (seconds).  0 = never auto-reset (manual only).
    auto_reset_seconds: float = 0.0


@dataclass
class CircuitBreakerState:
    """Snapshot of the internal breaker state for inspection."""

    consecutive_failures: int = 0
    is_open: bool = False
    total_failures: int = 0
    total_successes: int = 0
    last_failure_time: float | None = None
    last_success_time: float | None = None
    opened_at: float | None = None


class CompactionCircuitBreaker:
    """Tracks compaction attempts and blocks after consecutive failures."""

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitBreakerState()
        self._lock = threading.Lock()

    # ── public API ──────────────────────────────────────────────────────────

    def record_success(self) -> None:
        """Mark a successful compaction attempt."""
        with self._lock:
            self._state.consecutive_failures = 0
            self._state.total_successes += 1
            self._state.last_success_time = time.time()

    def record_failure(self) -> None:
        """Mark a failed compaction attempt.  May open the breaker."""
        with self._lock:
            self._state.consecutive_failures += 1
            self._state.total_failures += 1
            self._state.last_failure_time = time.time()
            if self._state.consecutive_failures >= self.config.failure_threshold:
                self._state.is_open = True
                self._state.opened_at = time.time()

    def is_allowed(self) -> bool:
        """Check whether compaction is currently allowed.

        Returns:
            True if compaction can proceed, False if it's blocked.
        """
        with self._lock:
            if not self._state.is_open:
                return True
            # Check auto-reset
            if (
                self.config.auto_reset_seconds > 0
                and self._state.opened_at is not None
                and time.time() - self._state.opened_at >= self.config.auto_reset_seconds
            ):
                self._reset()
                return True
            return False

    def reset(self) -> None:
        """Manually reset the breaker to closed state."""
        with self._lock:
            self._reset()

    def get_state(self) -> CircuitBreakerState:
        """Return a snapshot of the current state."""
        with self._lock:
            return CircuitBreakerState(
                consecutive_failures=self._state.consecutive_failures,
                is_open=self._state.is_open,
                total_failures=self._state.total_failures,
                total_successes=self._state.total_successes,
                last_failure_time=self._state.last_failure_time,
                last_success_time=self._state.last_success_time,
                opened_at=self._state.opened_at,
            )

    def _reset(self) -> None:
        """Internal reset (caller must hold _lock)."""
        self._state.consecutive_failures = 0
        self._state.is_open = False
        self._state.opened_at = None


# ── Module-level convenience ─────────────────────────────────────────────────

_default_breaker: CompactionCircuitBreaker | None = None


def get_compaction_circuit_breaker() -> CompactionCircuitBreaker:
    """Get or create the module-level compaction circuit breaker."""
    global _default_breaker
    if _default_breaker is None:
        _default_breaker = CompactionCircuitBreaker()
    return _default_breaker
