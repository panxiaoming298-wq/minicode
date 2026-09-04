from __future__ import annotations

from dataclasses import dataclass

from minicode.runtime_protocol import (
    AgentResult,
    AgentResultStatus,
    PlanNode,
    PlanVersion,
    RuntimeCommandType,
    TaskAmendment,
    ToolEffect,
)
from minicode.runtime_state import RuntimeReducer, RuntimeStateStore, command


@dataclass
class FakeClock:
    value: float = 100.0

    def now(self) -> float:
        return self.value

    def advance(self, amount: float) -> None:
        self.value += amount


def _plan(*, max_attempts: int = 1) -> PlanVersion:
    return PlanVersion(
        run_id="run-1",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode(
                node_id="node-1",
                objective="inspect",
                role="explorer",
                allowed_effects=(ToolEffect.READ_ONLY,),
                max_attempts=max_attempts,
            ),
        ),
    )


def _claim_and_start(reducer: RuntimeReducer, *, lease_seconds: float = 10.0):
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.CLAIM_NODE,
            {
                "node_id": "node-1",
                "plan_version": 1,
                "lease_owner": "worker-a",
                "lease_seconds": lease_seconds,
            },
        )
    )
    attempt = reducer.current_attempt("node-1")
    assert attempt is not None
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.START_ATTEMPT,
            {"node_id": "node-1", "attempt_id": attempt.attempt_id},
        )
    )
    return attempt


def _result(attempt, *, plan_version: int = 1) -> AgentResult:
    return AgentResult(
        run_id="run-1",
        plan_version=plan_version,
        node_id="node-1",
        attempt_id=attempt.attempt_id,
        status=AgentResultStatus.SUCCEEDED,
        base_revision="rev",
        output_summary="ok",
    )


def test_reducer_accepts_one_current_result_and_deduplicates_command() -> None:
    reducer = RuntimeReducer(_plan())
    attempt = _claim_and_start(reducer)
    submit = command(
        reducer.run_id,
        RuntimeCommandType.SUBMIT_RESULT,
        {"result": _result(attempt), "fencing_token": attempt.lease.fencing_token.value},
        command_id="submit-once",
    )
    first = reducer.apply(submit)
    second = reducer.apply(submit)

    assert first == second
    assert [event["event_type"] for event in reducer.snapshot()["events"]].count("ResultAccepted") == 1
    assert reducer.node_state("node-1").value == "VERIFYING"


def test_reducer_rejects_stale_fencing_and_expired_lease() -> None:
    clock = FakeClock()
    reducer = RuntimeReducer(_plan(), clock=clock)
    attempt = _claim_and_start(reducer, lease_seconds=5)
    stale = reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.SUBMIT_RESULT,
            {"result": _result(attempt), "fencing_token": attempt.lease.fencing_token.value + 1},
        )
    )
    assert stale[0].payload["reason"] == "stale fencing token"

    clock.advance(6)
    expired = reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.SUBMIT_RESULT,
            {"result": _result(attempt), "fencing_token": attempt.lease.fencing_token.value},
        )
    )
    assert expired[0].payload["reason"] == "expired lease"
    assert reducer.node_result("node-1") is None


def test_superseded_plan_result_cannot_pollute_current_state() -> None:
    reducer = RuntimeReducer(_plan())
    attempt = _claim_and_start(reducer)
    amendment = TaskAmendment(
        amendment_id="amend-1",
        run_id="run-1",
        base_contract_revision=1,
        next_contract_revision=2,
        changed_fields={"request": "new"},
        affected_node_ids=("node-1",),
    )
    next_plan = PlanVersion(
        run_id="run-1",
        plan_version=2,
        contract_revision=2,
        nodes=_plan().nodes,
        supersedes=1,
    )
    reducer.install_plan(next_plan, amendment)
    events = reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.SUBMIT_RESULT,
            {"result": _result(attempt), "fencing_token": attempt.lease.fencing_token.value},
        )
    )
    assert events[0].event_type.value == "ResultRejected"
    assert "non-current plan" in events[0].payload["reason"]


def test_runtime_snapshot_round_trips_after_restart(tmp_path) -> None:
    path = tmp_path / "runtime-state.json"
    store = RuntimeStateStore(path)
    reducer = RuntimeReducer(_plan(), store=store)
    attempt = _claim_and_start(reducer)
    payload = store.load()
    assert payload is not None

    restored = RuntimeReducer.restore(payload, store=store)
    restored_attempt = restored.current_attempt("node-1")
    assert restored_attempt is not None
    assert restored_attempt.attempt_id == attempt.attempt_id
    assert restored.snapshot()["events"] == reducer.snapshot()["events"]


def test_waiting_approval_survives_restart_and_resumes_through_command(tmp_path) -> None:
    store = RuntimeStateStore(tmp_path / "approval-state.json")
    reducer = RuntimeReducer(_plan(), store=store)
    attempt = _claim_and_start(reducer)
    waiting = AgentResult(
        run_id="run-1",
        plan_version=1,
        node_id="node-1",
        attempt_id=attempt.attempt_id,
        status=AgentResultStatus.WAITING_APPROVAL,
        base_revision="rev",
        output_summary="approval required",
    )
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.SUBMIT_RESULT,
            {"result": waiting, "fencing_token": attempt.lease.fencing_token.value},
        )
    )
    restored = RuntimeReducer.restore(store.load() or {})
    assert restored.node_state("node-1").value == "WAITING_APPROVAL"
    restored.apply(
        command(
            restored.run_id,
            RuntimeCommandType.APPROVE_TOOL_CALL,
            {"approval_id": "approval-1", "node_id": "node-1"},
        )
    )
    assert restored.node_state("node-1").value == "READY"
    assert restored.current_attempt("node-1") is None


def test_amendment_reuses_unaffected_success_and_replaces_only_affected_node() -> None:
    nodes = (
        PlanNode("affected", "old objective", "explorer"),
        PlanNode("reusable", "stable objective", "explorer"),
    )
    reducer = RuntimeReducer(
        PlanVersion("run-amend", 1, 1, nodes)
    )
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.CLAIM_NODE,
            {"node_id": "affected", "plan_version": 1},
        )
    )
    affected_attempt = reducer.current_attempt("affected")
    assert affected_attempt is not None
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.START_ATTEMPT,
            {"node_id": "affected", "attempt_id": affected_attempt.attempt_id},
        )
    )
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.SUBMIT_RESULT,
            {
                "result": AgentResult(
                    run_id="run-amend",
                    plan_version=1,
                    node_id="affected",
                    attempt_id=affected_attempt.attempt_id,
                    status=AgentResultStatus.WAITING_INPUT,
                    base_revision="rev",
                    output_summary="need amended objective",
                ),
                "fencing_token": affected_attempt.lease.fencing_token.value,
            },
        )
    )
    assert reducer.node_state("affected").value == "WAITING_INPUT"
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.CLAIM_NODE,
            {"node_id": "reusable", "plan_version": 1},
        )
    )
    attempt = reducer.current_attempt("reusable")
    assert attempt is not None
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.START_ATTEMPT,
            {"node_id": "reusable", "attempt_id": attempt.attempt_id},
        )
    )
    reusable_result = AgentResult(
        run_id="run-amend",
        plan_version=1,
        node_id="reusable",
        attempt_id=attempt.attempt_id,
        status=AgentResultStatus.SUCCEEDED,
        base_revision="rev",
        output_summary="valid reusable evidence",
    )
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.SUBMIT_RESULT,
            {
                "result": reusable_result,
                "fencing_token": attempt.lease.fencing_token.value,
            },
        )
    )
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.ACCEPT_VERIFICATION,
            {"node_id": "reusable"},
        )
    )
    amendment = TaskAmendment(
        amendment_id="amend-2",
        run_id="run-amend",
        base_contract_revision=1,
        next_contract_revision=2,
        changed_fields={"request": "new objective"},
        affected_node_ids=("affected",),
    )
    next_nodes = (
        PlanNode("affected", "new objective", "explorer"),
        nodes[1],
    )
    reducer.install_plan(
        PlanVersion("run-amend", 2, 2, next_nodes, supersedes=1),
        amendment,
    )

    assert reducer.node_state("affected").value == "READY"
    assert reducer.node_result("affected") is None
    assert reducer.node_state("reusable").value == "SUCCEEDED"
    assert reducer.node_result("reusable") == reusable_result
    assert set(reducer.snapshot()["nodes"]) == {"affected", "reusable"}
