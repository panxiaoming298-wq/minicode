"""Deadline and continuation metadata shared by retrieval adapters."""

from __future__ import annotations

import time
from contextlib import nullcontext
from functools import wraps


class SearchResults(list):
    """List-compatible results with continuation measured before filtering."""

    def __init__(self, values=(), *, has_more: bool = False):
        super().__init__(values)
        self.has_more = has_more


def has_more(results, limit: int) -> bool:
    # Older adapters cannot prove exhaustion when they filled their window.
    return bool(getattr(results, "has_more", len(results) >= limit))


def remaining_seconds(deadline: float | None) -> float:
    remaining = float("inf") if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("memory retrieval deadline exceeded")
    return remaining


def authority_deadline(authority, deadline: float | None):
    if deadline is None:
        return nullcontext()
    remaining_seconds(deadline)
    scope = getattr(authority, "query_deadline", None)
    return scope(deadline) if callable(scope) else nullcontext()


def bounded_search(method):
    @wraps(method)
    def search(self, request):
        with authority_deadline(self, request.deadline_monotonic):
            return method(self, request)

    return search
