from __future__ import annotations

import os
import threading

import pytest

from minicode.runtime_protocol import (
    AgentInvocationSpec,
    AgentResult,
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
from minicode.tooling import ToolRegistry
from minicode.worker_runtime import AgentLoopWorkerRunner
from minicode.workspace_provider import (
    TemporaryWorkspaceProvider,
    WorkspaceConflict,
    workspace_snapshot,
)


def test_memory_authority_directory_is_never_copied_or_integrated(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "tracked.txt").write_text("base", encoding="utf-8")
    memory = source / ".mini-code-memory-v2"
    memory.mkdir()
    (memory / "state.db").write_bytes(b"authority")

    provider = TemporaryWorkspaceProvider(tmp_path / "isolated")
    handle = provider.create(source, owner_token="owner")
    try:
        assert ".mini-code-memory-v2/state.db" not in workspace_snapshot(source)
        assert not (handle.workspace / ".mini-code-memory-v2").exists()
        (memory / "state.db").write_bytes(b"new-authority")
        assert provider.integrate(handle) == ()
    finally:
        provider.cleanup(handle)


def test_temporary_writer_integrates_changes_and_cleans_up(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("before", encoding="utf-8")
    temporary_root = tmp_path / "runtime-temp"
    provider = TemporaryWorkspaceProvider(temporary_root)
    handle = provider.create(source, owner_token="attempt-1")
    isolated_container = handle.workspace.parent
    (handle.workspace / "a.txt").write_text("after", encoding="utf-8")

    assert provider.integrate(handle) == ("a.txt",)
    assert (source / "a.txt").read_text(encoding="utf-8") == "after"
    provider.cleanup(handle)
    assert not isolated_container.exists()


def test_base_drift_stops_apply_and_preserves_user_changes(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.txt").write_text("base", encoding="utf-8")
    provider = TemporaryWorkspaceProvider(tmp_path / "runtime-temp")
    handle = provider.create(source, owner_token="attempt-2")
    (handle.workspace / "target.txt").write_text("writer", encoding="utf-8")
    (source / "target.txt").write_text("user concurrent edit", encoding="utf-8")

    with pytest.raises(WorkspaceConflict, match="base revision changed"):
        provider.integrate(handle)
    assert (source / "target.txt").read_text(encoding="utf-8") == "user concurrent edit"
    provider.cleanup(handle)


def test_unrelated_user_change_is_not_overwritten(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "writer.txt").write_text("base", encoding="utf-8")
    (source / "user.txt").write_text("old", encoding="utf-8")
    provider = TemporaryWorkspaceProvider(tmp_path / "runtime-temp")
    handle = provider.create(source, owner_token="attempt-3")
    (handle.workspace / "writer.txt").write_text("writer", encoding="utf-8")
    (source / "user.txt").write_text("new user content", encoding="utf-8")

    assert provider.integrate(handle) == ("writer.txt",)
    assert (source / "writer.txt").read_text(encoding="utf-8") == "writer"
    assert (source / "user.txt").read_text(encoding="utf-8") == "new user content"
    provider.cleanup(handle)


def test_writer_cannot_replace_file_with_directory_and_trigger_source_deletion(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = source / "target.txt"
    target.write_text("must survive", encoding="utf-8")
    provider = TemporaryWorkspaceProvider(tmp_path / "runtime-temp")
    handle = provider.create(source, owner_token="attempt-directory-replacement")
    isolated_target = handle.workspace / "target.txt"
    isolated_target.unlink()
    isolated_target.mkdir()
    (isolated_target / "nested.txt").write_text("not a regular replacement", encoding="utf-8")

    with pytest.raises(WorkspaceConflict, match="non-regular isolated path"):
        provider.integrate(handle)

    assert target.read_text(encoding="utf-8") == "must survive"
    provider.cleanup(handle)


def test_writer_link_alias_is_rejected_before_integration(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.txt").write_text("target base", encoding="utf-8")
    (source / "other.txt").write_text("other base", encoding="utf-8")
    provider = TemporaryWorkspaceProvider(tmp_path / "runtime-temp")
    handle = provider.create(source, owner_token="attempt-link-alias")
    isolated_target = handle.workspace / "target.txt"
    isolated_target.unlink()
    os.link(handle.workspace / "other.txt", isolated_target)

    with pytest.raises(WorkspaceConflict, match="link aliases"):
        provider.integrate(handle)

    assert (source / "target.txt").read_text(encoding="utf-8") == "target base"
    assert (source / "other.txt").read_text(encoding="utf-8") == "other base"
    provider.cleanup(handle)


def test_multi_file_integration_failure_rolls_back_applied_files(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a-base", encoding="utf-8")
    (source / "b.txt").write_text("b-base", encoding="utf-8")
    provider = TemporaryWorkspaceProvider(tmp_path / "runtime-temp")
    handle = provider.create(source, owner_token="attempt-rollback")
    (handle.workspace / "a.txt").write_text("a-writer", encoding="utf-8")
    (handle.workspace / "b.txt").write_text("b-writer", encoding="utf-8")
    original_replace = os.replace

    def fail_second_destination(source_path, destination_path):
        if tmp_path.__class__(destination_path).name == "b.txt":
            raise OSError("injected second-file commit failure")
        original_replace(source_path, destination_path)

    monkeypatch.setattr("minicode.workspace_provider.os.replace", fail_second_destination)
    with pytest.raises(WorkspaceConflict, match="rolled back"):
        provider.integrate(handle)

    assert (source / "a.txt").read_text(encoding="utf-8") == "a-base"
    assert (source / "b.txt").read_text(encoding="utf-8") == "b-base"
    assert not list(source.glob("*.minicode-*.tmp"))
    provider.cleanup(handle)


def test_writer_cancel_before_integration_cleans_workspace_and_preserves_source(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("base", encoding="utf-8")
    temporary_root = tmp_path / "runtime-temp"
    cancellation = threading.Event()
    started = threading.Event()

    def fake_agent_loop(**kwargs):
        started.set()
        assert cancellation.wait(timeout=5)
        workspace = tmp_path.__class__(kwargs["cwd"])
        (workspace / "writer-output.txt").write_text("must not integrate", encoding="utf-8")
        return [*kwargs["messages"], {"role": "assistant", "content": "writer stopped"}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runner = AgentLoopWorkerRunner(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "write"}],
        cwd=str(source),
        runtime={},
        cancellation_event=cancellation,
        isolate_writes=True,
        temporary_root=temporary_root,
    )
    invocation = AgentInvocationSpec(
        run_id="run-cancel",
        plan_version=1,
        node_id="writer",
        attempt_id="attempt-1",
        role="writer",
        objective="write",
        context_manifest=ContextManifest(
            contract_revision=1,
            plan_version=1,
            node_id="writer",
            base_revision="rev",
            included_files={},
        ),
        allowed_effects=(ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE),
        budget=BudgetEnvelope(),
    )
    result_box = {}
    thread = threading.Thread(target=lambda: result_box.setdefault("result", runner.run(invocation)))
    thread.start()
    assert started.wait(timeout=5)
    cancellation.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result_box["result"].status.value == "canceled"
    assert not (source / "writer-output.txt").exists()
    assert temporary_root.is_dir()
    assert list(temporary_root.iterdir()) == []


class _FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def now(self) -> float:
        return self.value


def _writer_profile() -> ExecutionProfile:
    return ExecutionProfile(
        requested_mode=RequestedMode.ULTRA,
        selected_route=SelectedRoute.DEEP_SINGLE,
        reasoning_budget="high",
        planner_budget=0,
        verifier_budget=1,
        max_active_workers=1,
        max_agents=1,
        max_tokens=None,
        max_cost=None,
        deadline=None,
        retry_budget=1,
        max_delegation_depth=1,
        verification_level=VerificationLevel.STRICT,
        write_isolation=WriteIsolation.TEMPORARY_WORKSPACE,
    )


def _writer_plan(*, run_id: str, max_attempts: int = 1) -> PlanVersion:
    return PlanVersion(
        run_id=run_id,
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode(
                "writer",
                "write output",
                "writer",
                allowed_effects=(ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE),
                max_attempts=max_attempts,
            ),
        ),
    )


def _writer_context(
    node: PlanNode,
    attempt_id: str,
    budget: BudgetEnvelope,
) -> ContextManifest:
    return ContextManifest(
        contract_revision=1,
        plan_version=1,
        node_id=node.node_id,
        base_revision="rev",
        included_files={},
        budget_allocation=budget,
    )


def _writer_verify(result: AgentResult) -> VerificationRecord:
    return VerificationRecord("workspace", True, f"node:{result.node_id}")


def test_expired_writer_result_is_discarded_before_workspace_commit(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    temporary_root = tmp_path / "runtime-temp"
    clock = _FakeClock()

    def fake_agent_loop(**kwargs):
        workspace = tmp_path.__class__(kwargs["cwd"])
        (workspace / "writer-output.txt").write_text("stale", encoding="utf-8")
        clock.value = 102.0
        return [*kwargs["messages"], {"role": "assistant", "content": "done"}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runner = AgentLoopWorkerRunner(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "write"}],
        cwd=str(source),
        runtime={},
        isolate_writes=True,
        temporary_root=temporary_root,
    )
    reducer = RuntimeReducer(_writer_plan(run_id="run-expired"), clock=clock)
    report = TaskScheduler(
        reducer,
        _writer_profile(),
        clock=clock,
        lease_seconds=1.0,
    ).execute(runner, context_builder=_writer_context, node_verifier=_writer_verify)

    assert not report.completed
    assert reducer.node_state("writer").value == "FAILED_TERMINAL"
    assert not (source / "writer-output.txt").exists()
    assert list(temporary_root.iterdir()) == []


def test_cleanup_failure_does_not_turn_committed_success_into_retry(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    temporary_root = tmp_path / "runtime-temp"
    agent_calls = 0

    def fake_agent_loop(**kwargs):
        nonlocal agent_calls
        agent_calls += 1
        workspace = tmp_path.__class__(kwargs["cwd"])
        (workspace / "writer-output.txt").write_text("committed", encoding="utf-8")
        return [*kwargs["messages"], {"role": "assistant", "content": "done"}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runner = AgentLoopWorkerRunner(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "write"}],
        cwd=str(source),
        runtime={},
        isolate_writes=True,
        temporary_root=temporary_root,
    )
    original_cleanup = runner.temporary_provider.cleanup
    cleanup_calls = 0

    def transient_cleanup(handle):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise PermissionError("temporary Windows lock")
        original_cleanup(handle)

    monkeypatch.setattr(runner.temporary_provider, "cleanup", transient_cleanup)
    reducer = RuntimeReducer(
        _writer_plan(run_id="run-cleanup", max_attempts=2)
    )
    report = TaskScheduler(reducer, _writer_profile()).execute(
        runner,
        context_builder=_writer_context,
        node_verifier=_writer_verify,
    )

    assert report.completed
    assert agent_calls == 1
    assert cleanup_calls == 2
    assert (source / "writer-output.txt").read_text(encoding="utf-8") == "committed"
    assert list(temporary_root.iterdir()) == []
