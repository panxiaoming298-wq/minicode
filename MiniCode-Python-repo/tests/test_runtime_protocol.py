from __future__ import annotations

from minicode.runtime_protocol import (
    ApprovalRequest,
    BudgetEnvelope,
    ExecutionProfile,
    RequestedMode,
    SelectedRoute,
    TaskContract,
    TaskEnvelope,
    ToolEffect,
    VerificationLevel,
    WriteIsolation,
    operation_digest,
)


def test_runtime_contracts_are_versioned_and_json_serializable(tmp_path) -> None:
    envelope = TaskEnvelope(
        run_id="run-1",
        request="inspect",
        workspace=str(tmp_path),
        requested_profile=RequestedMode.ULTRA,
    )
    contract = TaskContract(
        envelope=envelope,
        acceptance_criteria=("evidence exists",),
        budget=BudgetEnvelope(max_agents=4, max_active_workers=2),
    )
    profile = ExecutionProfile(
        requested_mode=RequestedMode.ULTRA,
        selected_route=SelectedRoute.ULTRA_READ_FANOUT,
        reasoning_budget="high",
        planner_budget=0,
        verifier_budget=1,
        max_active_workers=2,
        max_agents=4,
        max_tokens=20_000,
        max_cost=1.0,
        deadline=None,
        retry_budget=1,
        max_delegation_depth=1,
        verification_level=VerificationLevel.STRICT,
        write_isolation=WriteIsolation.TEMPORARY_WORKSPACE,
    )

    assert contract.to_dict()["envelope"]["requested_profile"] == "ultra"
    assert contract.to_dict()["schema_version"] == "minicode-runtime-v2"
    assert profile.to_dict()["selected_route"] == "ultra-read-fanout"
    assert profile.budget.max_active_workers == 2
    assert ToolEffect.READ_ONLY.value in {"read_only"}


def test_operation_digest_binds_every_operation_field(tmp_path) -> None:
    base = {
        "run_id": "run-1",
        "node_id": "node-1",
        "attempt_id": "attempt-1",
        "tool_call_id": "call-1",
        "cwd": str(tmp_path),
        "arguments": {"path": "a.txt", "content": "one"},
        "environment_scope": {"PATH": "bounded"},
        "base_revision": "rev-1",
    }
    digest = operation_digest(**base)
    for field, value in (
        ("run_id", "run-2"),
        ("node_id", "node-2"),
        ("attempt_id", "attempt-2"),
        ("tool_call_id", "call-2"),
        ("cwd", str(tmp_path / "other")),
        ("arguments", {"path": "a.txt", "content": "two"}),
        ("environment_scope", {"PATH": "different"}),
        ("base_revision", "rev-2"),
    ):
        changed = dict(base)
        changed[field] = value
        assert operation_digest(**changed) != digest

    request = ApprovalRequest.create(**base, expires_at=9999999999.0)
    assert request.operation_digest == digest

