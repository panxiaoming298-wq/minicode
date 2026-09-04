"""Central approval and side-effect policy for Runtime V2."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from minicode.runtime_protocol import (
    ApprovalGrant,
    ApprovalRequest,
    ExecutionFailure,
    ExecutionFailureKind,
    ReplayPolicy,
    ToolEffect,
    operation_digest,
)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    allowed: bool
    failure: ExecutionFailure | None = None


class ApprovalAuthority:
    """Validate grants against every operation-bound approval field.

    PermissionManager remains the UI/worker-side prompt adapter.  This class is
    the shared semantic authority, preventing per-worker approval drift.
    """

    def __init__(self, *, now=None) -> None:
        self._now = now or time.time
        self._used: set[str] = set()
        self._lock = threading.Lock()

    def validate(
        self,
        request: ApprovalRequest,
        grant: ApprovalGrant | None,
        *,
        run_id: str,
        node_id: str,
        attempt_id: str,
        tool_call_id: str,
        cwd: str,
        arguments: Mapping[str, Any],
        environment_scope: Mapping[str, Any],
        base_revision: str,
    ) -> ApprovalDecision:
        now = self._now()
        if grant is None:
            return ApprovalDecision(
                False,
                ExecutionFailure(
                    ExecutionFailureKind.POLICY_DENIED,
                    "tool call requires approval",
                ),
            )
        if now >= request.expires_at or (grant.expires_at is not None and now >= grant.expires_at):
            return ApprovalDecision(
                False,
                ExecutionFailure(
                    ExecutionFailureKind.APPROVAL_EXPIRED,
                    "approval expired",
                ),
            )
        expected = operation_digest(
            run_id=run_id,
            node_id=node_id,
            attempt_id=attempt_id,
            tool_call_id=tool_call_id,
            cwd=cwd,
            arguments=arguments,
            environment_scope=environment_scope,
            base_revision=base_revision,
        )
        if (
            grant.approval_id != request.approval_id
            or grant.operation_digest != request.operation_digest
            or expected != request.operation_digest
        ):
            return ApprovalDecision(
                False,
                ExecutionFailure(
                    ExecutionFailureKind.POLICY_DENIED,
                    "approval does not match operation digest, arguments, cwd, scope, or base revision",
                ),
            )
        with self._lock:
            if request.single_use and request.approval_id in self._used:
                return ApprovalDecision(
                    False,
                    ExecutionFailure(
                        ExecutionFailureKind.POLICY_DENIED,
                        "single-use approval already consumed",
                    ),
                )
            if request.single_use:
                self._used.add(request.approval_id)
        return ApprovalDecision(True)


def may_auto_retry(
    replay_policy: ReplayPolicy,
    *,
    result_is_ambiguous: bool,
    idempotency_key: str | None = None,
) -> bool:
    if replay_policy is ReplayPolicy.SAFE:
        return True
    if replay_policy is ReplayPolicy.IDEMPOTENT_WITH_KEY:
        return bool(idempotency_key)
    if replay_policy is ReplayPolicy.VERIFY_BEFORE_RETRY:
        return not result_is_ambiguous
    return False


def effect_allowed(effect: ToolEffect, allowed: frozenset[ToolEffect]) -> ApprovalDecision:
    if effect in allowed:
        return ApprovalDecision(True)
    return ApprovalDecision(
        False,
        ExecutionFailure(
            ExecutionFailureKind.POLICY_DENIED,
            f"tool effect {effect.value!r} is outside the Worker capability scope",
        ),
    )
