"""Clock-based memory ageing shared by file and relational stores."""

from __future__ import annotations

import math

DAY_SECONDS = 86_400.0
DECAY_PERIOD_SECONDS = 30 * DAY_SECONDS
ARCHIVE_AFTER_SECONDS = 90 * DAY_SECONDS
MAINTENANCE_INTERVAL_SECONDS = 3_600.0


def decay_schedule(
    *,
    now: float,
    last_activity: float,
    last_decay: float,
    period_seconds: float = DECAY_PERIOD_SECONDS,
) -> tuple[int, float]:
    """Return elapsed periods and their boundary, never the polling time.

    Persisting the boundary makes repeated calls, restarts and clock rollback
    harmless. New activity starts a new grace period without changing content
    timestamps or pretending that maintenance was a user access.
    """
    if not math.isfinite(period_seconds) or period_seconds <= 0:
        raise ValueError("decay period must be finite and positive")
    if not all(math.isfinite(value) for value in (now, last_activity, last_decay)):
        raise ValueError("memory lifecycle timestamps must be finite")
    anchor = max(last_activity, last_decay)
    periods = max(0, int((now - anchor) // period_seconds))
    return periods, anchor + periods * period_seconds if periods else last_decay


def validate_decay_factor(factor: float) -> None:
    if not math.isfinite(factor) or not 0 < factor <= 1:
        raise ValueError("decay factor must be in (0, 1]")
