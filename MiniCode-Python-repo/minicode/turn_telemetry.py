"""Measured per-turn timings shared with tool threads through context propagation."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any


@dataclass
class TurnTelemetry:
    clock: Callable[[], float] = time.perf_counter
    started_at: float = field(init=False)
    calls: dict[str, int] = field(default_factory=dict)
    durations: dict[str, float] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    def record(self, kind: str, seconds: float) -> None:
        with self._lock:
            self.calls[kind] = self.calls.get(kind, 0) + 1
            self.durations[kind] = self.durations.get(kind, 0.0) + max(0.0, seconds)

    def average(self, kind: str) -> float:
        with self._lock:
            return self.durations.get(kind, 0.0) / max(1, self.calls.get(kind, 0))

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "elapsed_seconds": self.elapsed_seconds,
                **{f"{kind}_calls": count for kind, count in self.calls.items()},
                **{
                    f"{kind}_seconds": seconds
                    for kind, seconds in self.durations.items()
                },
            }


_current: ContextVar[TurnTelemetry | None] = ContextVar("turn_telemetry", default=None)


def current_telemetry() -> TurnTelemetry | None:
    return _current.get()


@contextmanager
def telemetry_scope() -> Iterator[TurnTelemetry]:
    telemetry = TurnTelemetry()
    token = _current.set(telemetry)
    try:
        yield telemetry
    finally:
        _current.reset(token)


def measured(kind: str):
    """Measure whole attempts, including cancellation, errors and bounded waits."""

    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            telemetry = current_telemetry()
            if telemetry is None:
                return function(*args, **kwargs)
            start = telemetry.clock()
            try:
                return function(*args, **kwargs)
            finally:
                telemetry.record(kind, telemetry.clock() - start)

        return wrapped

    return decorate


def measured_turn(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if current_telemetry() is not None:
            return function(*args, **kwargs)
        with telemetry_scope() as telemetry:
            try:
                return function(*args, **kwargs)
            finally:
                runtime = kwargs.get("runtime")
                if isinstance(runtime, dict):
                    runtime["_lastTurnMetrics"] = telemetry.snapshot()

    return wrapped
