from __future__ import annotations

import threading
from dataclasses import replace

from minicode.runtime_protocol import (
    AgentInvocationSpec,
    AgentResult,
    AgentResultStatus,
    BudgetEnvelope,
    ContextManifest,
    ExecutionProfile,
    PlanNode,
    PlanVersion,
    RequestedMode,
    SelectedRoute,
    ToolEffect,
    VerificationLevel,
    VerificationRecord,
    WriteIsolation,
)
from minicode.runtime_scheduler import TaskScheduler
from minicode.runtime_state import RuntimeReducer


def _profile() -> ExecutionProfile:
    return ExecutionProfile(
        requested_mode=RequestedMode.ULTRA,
        selected_route=SelectedRoute.ULTRA_READ_FANOUT,
        reasoning_budget="high",
        planner_budget=0,
        verifier_budget=1,
        max_active_workers=2,
        max_agents=4,
        max_tokens=None,
        max_cost=None,
        deadline=None,
        retry_budget=0,
        max_delegation_depth=1,
        verification_level=VerificationLevel.STRICT,
        write_isolation=WriteIsolation.TEMPORARY_WORKSPACE,
    )


def _plan() -> PlanVersion:
    read = (ToolEffect.READ_ONLY,)
    return PlanVersion(
        run_id="run-1",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode("explorer-a", "a", "explorer", allowed_effects=read),
            PlanNode("explorer-b", "b", "reviewer", allowed_effects=read),
            PlanNode(
                "writer",
                "write",
                "writer",
                dependencies=("explorer-a", "explorer-b"),
                allowed_effects=(ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE),
            ),
            PlanNode(
                "verifier",
                "verify",
                "verifier",
                dependencies=("writer",),
                allowed_effects=read,
            ),
        ),
    )


class BarrierWorker:
    def __init__(self) -> None:
        self.barrier = threading.Barrier(2)
        self.explorers_reached: list[str] = []
        self.lock = threading.Lock()
        self.started_order: list[str] = []

    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        with self.lock:
            self.started_order.append(invocation.node_id)
        if invocation.node_id.startswith("explorer"):
            with self.lock:
                self.explorers_reached.append(invocation.node_id)
            self.barrier.wait(timeout=5)
        if invocation.node_id == "writer":
            assert set(self.explorers_reached) == {"explorer-a", "explorer-b"}
        return AgentResult(
            run_id=invocation.run_id,
            plan_version=invocation.plan_version,
            node_id=invocation.node_id,
            attempt_id=invocation.attempt_id,
            status=AgentResultStatus.SUCCEEDED,
            base_revision="rev",
            output_summary="ok",
        )


def _context(node: PlanNode, attempt_id: str, budget: BudgetEnvelope) -> ContextManifest:
    return ContextManifest(
        contract_revision=1,
        plan_version=1,
        node_id=node.node_id,
        base_revision="rev",
        included_files={},
        dependency_result_refs=node.dependencies,
        tool_capability_scope=node.allowed_effects,
        budget_allocation=budget,
    )


def _verify(result: AgentResult) -> VerificationRecord:
    return VerificationRecord("deterministic", True, f"node:{result.node_id}")


def test_scheduler_runs_independent_reads_in_parallel_then_writer_then_verifier() -> None:
    reducer = RuntimeReducer(_plan())
    scheduler = TaskScheduler(reducer, _profile())
    worker = BarrierWorker()
    report = scheduler.execute(worker, context_builder=_context, node_verifier=_verify)

    assert report.completed
    assert report.peak_active_workers == 2
    assert report.peak_active_writers == 1
    assert report.active_leases == 0
    assert set(worker.started_order[:2]) == {"explorer-a", "explorer-b"}
    assert worker.started_order.index("writer") > worker.started_order.index("explorer-a")
    assert worker.started_order.index("verifier") > worker.started_order.index("writer")
    assert all(node["state"] == "SUCCEEDED" for node in reducer.snapshot()["nodes"].values())


class CancelWorker:
    def __init__(self, cancellation: threading.Event) -> None:
        self.cancellation = cancellation
        self.started = threading.Event()

    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        self.started.set()
        self.cancellation.wait(timeout=5)
        return AgentResult(
            run_id=invocation.run_id,
            plan_version=invocation.plan_version,
            node_id=invocation.node_id,
            attempt_id=invocation.attempt_id,
            status=AgentResultStatus.CANCELED,
            base_revision="rev",
            output_summary="canceled",
        )


def test_parent_cancel_propagates_without_sleep_and_releases_leases() -> None:
    cancellation = threading.Event()
    plan = PlanVersion(
        run_id="run-cancel",
        plan_version=1,
        contract_revision=1,
        nodes=(PlanNode("node", "wait", "explorer"),),
    )
    reducer = RuntimeReducer(plan)
    profile = _profile()
    profile = replace(
        profile,
        selected_route=SelectedRoute.DEEP_SINGLE,
        max_agents=1,
        max_active_workers=1,
    )
    scheduler = TaskScheduler(reducer, profile, cancellation_event=cancellation)
    worker = CancelWorker(cancellation)
    report_box = {}

    thread = threading.Thread(
        target=lambda: report_box.setdefault(
            "report",
            scheduler.execute(worker, context_builder=_context, node_verifier=_verify),
        )
    )
    thread.start()
    assert worker.started.wait(timeout=5)
    scheduler.cancel("test cancellation")
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert report_box["report"].canceled
    assert report_box["report"].active_leases == 0
    assert scheduler.active_workers == 0
    assert scheduler.active_writers == 0
    assert reducer.snapshot()["nodes"]["node"]["state"] == "CANCELED"


class NonCooperativeCancelWorker:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        self.started.set()
        self.release.wait(timeout=5)
        return AgentResult(
            run_id=invocation.run_id,
            plan_version=invocation.plan_version,
            node_id=invocation.node_id,
            attempt_id=invocation.attempt_id,
            status=AgentResultStatus.SUCCEEDED,
            base_revision="rev",
            output_summary="late",
        )


def test_cancel_has_wall_clock_boundary_for_noncooperative_worker() -> None:
    cancellation = threading.Event()
    plan = PlanVersion(
        run_id="run-hard-cancel",
        plan_version=1,
        contract_revision=1,
        nodes=(PlanNode("node", "ignore cancel", "explorer"),),
    )
    reducer = RuntimeReducer(plan)
    profile = replace(
        _profile(),
        selected_route=SelectedRoute.DEEP_SINGLE,
        max_agents=1,
        max_active_workers=1,
    )
    scheduler = TaskScheduler(
        reducer,
        profile,
        cancellation_event=cancellation,
        cancellation_grace_seconds=0.0,
    )
    worker = NonCooperativeCancelWorker()
    scheduler_done = threading.Event()
    report_box = {}

    def execute() -> None:
        report_box["report"] = scheduler.execute(
            worker,
            context_builder=_context,
            node_verifier=_verify,
        )
        scheduler_done.set()

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert worker.started.wait(timeout=1)
        scheduler.cancel("hard boundary test")
        assert scheduler_done.wait(timeout=1)
        assert report_box["report"].canceled
        assert report_box["report"].active_leases == 0
        assert any(
            "cancellation grace expired" in reason
            for reason in report_box["report"].reasons
        )
    finally:
        worker.release.set()
        thread.join(timeout=1)


class CrashThenSucceedWorker:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("crash after claim")
        return AgentResult(
            run_id=invocation.run_id,
            plan_version=invocation.plan_version,
            node_id=invocation.node_id,
            attempt_id=invocation.attempt_id,
            status=AgentResultStatus.SUCCEEDED,
            base_revision="rev",
            output_summary="recovered",
        )


def test_worker_crash_creates_new_attempt_with_bounded_retry() -> None:
    plan = PlanVersion(
        run_id="run-retry",
        plan_version=1,
        contract_revision=1,
        nodes=(PlanNode("node", "retry", "explorer", max_attempts=2),),
    )
    reducer = RuntimeReducer(plan)
    profile = replace(
        _profile(),
        selected_route=SelectedRoute.DEEP_SINGLE,
        max_agents=1,
        max_active_workers=1,
        retry_budget=1,
    )
    scheduler = TaskScheduler(reducer, profile)
    worker = CrashThenSucceedWorker()
    report = scheduler.execute(worker, context_builder=_context, node_verifier=_verify)

    assert report.completed
    assert worker.calls == 2
    attempts = reducer.snapshot()["nodes"]["node"]["attempts"]
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
    assert attempts[0]["attempt_id"] != attempts[1]["attempt_id"]
