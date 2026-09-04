from __future__ import annotations

from dataclasses import dataclass

from minicode.runtime_policy import ApprovalAuthority, may_auto_retry
from minicode.runtime_protocol import (
    ApprovalGrant,
    ApprovalRequest,
    ReplayPolicy,
    ToolEffect,
)
from minicode.permissions import PermissionManager
from minicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult


@dataclass
class FakeClock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value


def _request(tmp_path) -> ApprovalRequest:
    return ApprovalRequest.create(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        tool_call_id="call-1",
        cwd=str(tmp_path),
        arguments={"path": "a.txt", "content": "one"},
        environment_scope={"network": "none"},
        base_revision="rev-1",
        expires_at=200.0,
    )


def _operation(request: ApprovalRequest) -> dict:
    return {
        "run_id": request.run_id,
        "node_id": request.node_id,
        "attempt_id": request.attempt_id,
        "tool_call_id": request.tool_call_id,
        "cwd": request.cwd,
        "arguments": request.arguments,
        "environment_scope": request.environment_scope,
        "base_revision": request.base_revision,
    }


def test_approval_rejects_changed_operation_and_single_use(tmp_path) -> None:
    clock = FakeClock()
    authority = ApprovalAuthority(now=clock)
    request = _request(tmp_path)
    grant = ApprovalGrant(request.approval_id, request.operation_digest)
    operation = _operation(request)

    changed = dict(operation)
    changed["arguments"] = {"path": "a.txt", "content": "different"}
    assert not authority.validate(request, grant, **changed).allowed
    changed = dict(operation)
    changed["cwd"] = str(tmp_path / "other")
    assert not authority.validate(request, grant, **changed).allowed
    changed = dict(operation)
    changed["base_revision"] = "rev-2"
    assert not authority.validate(request, grant, **changed).allowed

    assert authority.validate(request, grant, **operation).allowed
    assert not authority.validate(request, grant, **operation).allowed


def test_expired_approval_is_structured_and_never_valid(tmp_path) -> None:
    clock = FakeClock(201.0)
    authority = ApprovalAuthority(now=clock)
    request = _request(tmp_path)
    grant = ApprovalGrant(request.approval_id, request.operation_digest)
    decision = authority.validate(request, grant, **_operation(request))
    assert not decision.allowed
    assert decision.failure is not None
    assert decision.failure.kind.value == "ApprovalExpired"


def test_replay_policy_never_retries_ambiguous_non_idempotent_call() -> None:
    assert may_auto_retry(ReplayPolicy.SAFE, result_is_ambiguous=True)
    assert may_auto_retry(
        ReplayPolicy.IDEMPOTENT_WITH_KEY,
        result_is_ambiguous=True,
        idempotency_key="same-key",
    )
    assert not may_auto_retry(
        ReplayPolicy.IDEMPOTENT_WITH_KEY,
        result_is_ambiguous=True,
        idempotency_key=None,
    )
    assert not may_auto_retry(ReplayPolicy.VERIFY_BEFORE_RETRY, result_is_ambiguous=True)
    assert not may_auto_retry(ReplayPolicy.NEVER_AUTO_RETRY, result_is_ambiguous=True)


def test_governed_permission_allow_once_is_bound_to_full_operation_digest(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "minicode.permissions.MINI_CODE_PERMISSIONS_PATH",
        tmp_path / "permissions.json",
    )
    seen_digests: list[str] = []

    def prompt(request):
        seen_digests.append(request["operationDigest"])
        return {"decision": "allow_once"}

    permissions = PermissionManager(str(tmp_path), prompt=prompt)

    def run_probe(value, context):
        context.permissions.ensure_command(
            "probe",
            [value["payload"]],
            context.cwd,
            force_prompt_reason="approval digest test",
        )
        return ToolResult(ok=True, output="approved")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="approval_probe",
                description="exercise operation-bound approval",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=run_probe,
            )
        ]
    )
    context = ToolContext(
        cwd=str(tmp_path),
        permissions=permissions,
        allowed_effects=frozenset({ToolEffect.PRIVILEGED}),
        run_id="run-1",
        node_id="writer",
        attempt_id="attempt-1",
        tool_call_id="call-1",
        base_revision="base-1",
        idempotency_key="stable-key",
    )

    assert registry.execute("approval_probe", {"payload": "one"}, context).ok
    assert registry.execute("approval_probe", {"payload": "changed"}, context).ok
    assert len(seen_digests) == 2
    assert seen_digests[0] != seen_digests[1]
