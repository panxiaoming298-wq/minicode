"""Bounded recovery policy and projections; never task acceptance authority."""
from __future__ import annotations

import errno
from collections.abc import Mapping, Sequence


def transient_store_error(error: BaseException) -> bool:
    return isinstance(error, (PermissionError, BlockingIOError, TimeoutError)) or (
        isinstance(error, OSError) and error.errno in {errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.ETIMEDOUT})


def child_recovery(node_id: str, state: str, effects: Sequence[str], attempts: Sequence[Mapping]) -> dict | None:
    """Only an accepted success for this same node resolves its earlier attempts."""
    if not node_id.startswith(("consult-", "agent-", "delegated-")) or tuple(effects) != ("read_only",):
        return None
    failed = [attempt["attempt_id"] for attempt in attempts
              if attempt.get("state") in {"FAILED_RETRYABLE", "FAILED_TERMINAL"}
              and isinstance(attempt.get("attempt_id"), str)]
    if not failed:
        return None
    status = ("recovered" if state == "SUCCEEDED" else "retrying" if state in {
        "READY", "CLAIMED", "RUNNING", "VERIFYING", "FAILED_RETRYABLE"} else "exhausted")
    return {"status": status, "failedAttemptIds": failed}
