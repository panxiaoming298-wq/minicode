"""Traceable deterministic preparation for a genuine-model repair probe.

This is a fixture producer, not a model adapter or an AgentLoop replacement.
It executes real file tools and a real candidate handoff, obtains actual failed
acceptance evidence, then persists REQUEST_REPAIR for the normal product entry.
"""
from __future__ import annotations

from pathlib import Path

from minicode.candidate_submission import execute_submission_batch
from minicode.delegation import SchedulerDelegationGateway
from minicode.path_safety import _resolved_comparison_path
from minicode.runtime_protocol import ContextManifest, PrimaryRepair, RuntimeCommandType, TaskContract, TaskEnvelope, new_id
from minicode.runtime_scheduler import TaskScheduler
from minicode.runtime_state import RuntimeReducer, RuntimeStateStore, command
from minicode.tool_execution import _execute_single_tool
from minicode.turn_events import AgentTurnOutcome
from minicode.unified_runtime import PlanCompiler, PlanValidator, ProfileResolver, RunCoordinator, TopologyResolver, _build_run_identity
from minicode.verifier_registry import CompletionGate, NodeResultVerifier, VerificationContext
from minicode.worker_runtime import AgentLoopWorkerRunner
from minicode.workspace_lock import WorkspaceWriteLease
from minicode.workspace_provider import snapshot_revision, workspace_snapshot

SEED_ID = "export-near-miss-fixture-v1"
SEED_FILES = {
    "batch_window.py": """def batch_window(total, cursor, limit):
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError('total')
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        raise ValueError('cursor')
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError('limit')
    start = min(cursor, total)
    stop = min(start + limit, total)
    return {'start': start, 'stop': stop, 'next_cursor': stop if stop < total else None}
""",
    # The intentionally missing negative retry_after_ms check is a fixture
    # defect. Never label this source as an observed model-generated candidate.
    "retry_delay.py": """def retry_delay(status, attempt, max_attempts, base_ms, cap_ms, retry_after_ms):
    if not isinstance(status, int) or isinstance(status, bool) or status < 100 or status > 599:
        raise ValueError('status')
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError('attempt')
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise ValueError('max_attempts')
    if not isinstance(base_ms, int) or isinstance(base_ms, bool) or base_ms < 0:
        raise ValueError('base_ms')
    if not isinstance(cap_ms, int) or isinstance(cap_ms, bool) or cap_ms < 0:
        raise ValueError('cap_ms')
    if retry_after_ms is not None and (not isinstance(retry_after_ms, int) or isinstance(retry_after_ms, bool)):
        raise ValueError('retry_after_ms')
    if status not in (408, 429, 500, 502, 503, 504) or attempt >= max_attempts:
        return None
    delay = attempt * base_ms
    if status in (429, 503) and retry_after_ms is not None:
        delay = max(delay, retry_after_ms)
    return min(delay, cap_ms)
""",
}
SEED_NOTICE = (
    "This is a targeted repair probe, not an autonomous end-to-end task-quality evaluation. "
    "The first candidate and its tool transcript are produced by a deterministic file fixture, "
    "not an LLM. Runtime must run the original acceptance and preserve its actual failure before "
    "resuming a genuine model for bounded repair. The fixture does not supply a repair solution "
    "or any consultation reports. Keep specifications and acceptance unchanged."
)


class SeedFileWorker:
    """One concrete fixture invocation, using real tools and fenced submission."""

    def __init__(self, runner, runtime, tools, messages):
        self.runner, self.runtime, self.tools, self.messages = runner, runtime, tools, messages
        self.gateway = None
        self.invocations = []
        self.receipts = []

    def __getattr__(self, name):
        return getattr(self.runner, name)

    def run(self, invocation):
        if self.invocations or invocation.node_id != "primary":
            raise ValueError("the deterministic seed may execute only once; model repair uses the product entry")
        self.invocations.append(invocation)
        handle = self.runner.temporary_provider.create(self.runner.cwd, owner_token=invocation.attempt_id)
        self.runner._active_workspaces[invocation.node_id] = (invocation.attempt_id, handle)
        self.runner.runtime_context = self.gateway
        submission = self.runner._candidate_submission(invocation, handle)
        if submission is None:
            raise ValueError("repair seed requires native isolated candidate submission")
        registry = self.tools.with_additional_tools([submission.tool()])
        scoped_runtime = {**self.runtime, "_runtimeContext": self.gateway,
            "_runtimeNodeId": invocation.node_id, "_runtimeAttemptId": invocation.attempt_id,
            "_runtimeActiveRunId": invocation.run_id, "_runtimeIdempotencyKey": invocation.idempotency_key,
            "_runtimeBaseRevision": invocation.context_manifest.base_revision,
            "_allowedToolEffects": [e.value for e in invocation.allowed_effects],
            "_cancellationEvent": self.runner.cancellation_event}
        calls = [{"id": f"seed-write-{index}", "toolName": "write_file", "input": {"path": name, "content": content}}
                 for index, (name, content) in enumerate(SEED_FILES.items(), 1)]
        calls.append({"id": "seed-submit", "toolName": "submit_candidate", "input": {
            "summary": "Deterministic fixture candidate; known to require original Runtime acceptance. Not model output or a success claim."}})
        results = execute_submission_batch(calls, registry, lambda call: _execute_single_tool(
            call, registry, str(handle.workspace), None, None, scoped_runtime, None, 1, None, None))
        if not all(result.ok for _, result in results):
            raise ValueError("seed tool execution failed: " + "; ".join(result.output for _, result in results if not result.ok))
        transcript = list(self.messages)
        for call, result in results:
            transcript.extend([
                {"role": "assistant_tool_call", "toolUseId": call["id"], "toolName": call["toolName"], "input": call["input"]},
                {"role": "tool_result", "toolUseId": call["id"], "toolName": call["toolName"], "content": result.output, "isError": not result.ok},
            ])
            self.receipts.append({"origin": "deterministic_fixture", "call": call, "ok": result.ok, "output": result.output})
        outcome = AgentTurnOutcome(tuple(transcript), "candidate_submitted")
        # Zero model usage is factual: no model adapter is called in preparation.
        result = self.runner._result_from_outcome(invocation, outcome, "", handle.changed_resources(),
            {"tokens": 0, "cost": 0.0}, submission=submission)
        result = self.runner.checkpoint_completed_primary(invocation, result, handle, transcript,
                                                          expected_revision=submission.consumed_revision)
        self.runner._active_workspaces.pop(invocation.node_id)
        self.runner._pending_workspaces[self.runner._result_key(result)] = (self.runner.temporary_provider, handle)
        self.runner.hydrate_result(result)
        return result


def prepare_repair_seed(*, root: Path, runtime: dict, tools, messages: list[dict], artifact_store) -> dict:
    """Create one new failed fixture candidate, never rewrite a prior run/state."""
    from minicode.runtime_live_cases import EXPORT
    from minicode.runtime_live_eval import content_hash, write_report

    root = root.resolve()
    data_boundary = Path(__file__).resolve().parents[1] / "Package/AgentRuntime/Data"
    if not root.is_relative_to(data_boundary.resolve()) or root == data_boundary.resolve():
        raise ValueError("seed preparation must stay inside task-specific Runtime Data")
    workspace = root / "workspace"
    if (runtime.get("runtimeOrchestration") != "primary" or runtime.get("runtimeDurableConsultation") is not True
        or runtime.get("runtimeResume") or runtime.get("requiredConsultation") is not None
        or runtime.get("runtimeMaxRepairRounds") != 1 or not runtime.get("runtimeRunId")
        or not messages or messages[-1].get("role") != "user" or SEED_NOTICE not in messages[-1].get("content", "")):
        raise ValueError("seed preparation requires an explicit new one-repair primary probe with disclosed fixture provenance")
    if Path(runtime["runtimeStatePath"]).resolve() != root / "state.json":
        raise ValueError("seed state must belong to this probe root")
    if (Path(runtime["runtimeTemporaryRoot"]).resolve() != root / "temporary"
        or Path(runtime["runtimeLockRoot"]).resolve() != root / "locks"
        or _resolved_comparison_path(artifact_store.root.resolve())
            != _resolved_comparison_path(root / "artifacts")):
        raise ValueError("seed artifacts, locks and temporary workspace must belong to this probe")
    original = workspace_snapshot(workspace)
    if content_hash({name: (workspace / name).read_text(encoding="utf-8") for name in EXPORT.initial_files}) != content_hash(EXPORT.initial_files):
        raise ValueError("seed preparation requires the original immutable export fixture")
    request = messages[-1]["content"]
    mode = ProfileResolver.requested_mode(runtime)
    topology = TopologyResolver().decide(requested_mode=mode, request=request, runtime=runtime, messages=messages)
    profile = ProfileResolver().resolve(runtime=runtime, selected_route=topology.selected_route, request=request)
    contract = TaskContract(TaskEnvelope(runtime["runtimeRunId"], request, str(workspace), mode),
        constraints=tuple(runtime["taskConstraints"]), acceptance_criteria=tuple(runtime["acceptanceCriteria"]),
        expected_outputs=tuple(runtime["expectedOutputs"]), budget=profile.budget)
    plan = PlanCompiler().compile(contract, profile, topology)
    PlanValidator().validate(plan, profile, requires_workspace_write=True)
    if len(plan.nodes) != 1 or mode.value not in {"deep", "ultra"}:
        raise ValueError("seed preparation requires one isolated Deep/Ultra primary")
    identity = _build_run_identity(contract=contract, profile=profile, topology=topology,
                                    messages=messages, cwd=str(workspace), runtime=runtime)
    with WorkspaceWriteLease(workspace, run_id=contract.envelope.run_id, lock_root=Path(runtime["runtimeLockRoot"])):
        store = RuntimeStateStore(root / "state.json")
        store.reserve_new()
        reducer = RuntimeReducer(plan, store=store, contract=contract, identity=identity,
            continuations_enabled=True, advisory_enabled=True, closure_reserve_enabled=True,
            shared_consultation_budget=True, continuation_limit=2, repair_limit=1, candidate_first=True,
            review_baseline=original if profile.dynamic_review else None,
            review_reasoning_effort=runtime.get("runtimeReasoningEffort") or runtime.get("deepseekReasoningEffort") or None,
            review_max_agents=profile.max_agents)
        runner = AgentLoopWorkerRunner(model=None, tools=tools, messages=messages, cwd=str(workspace),
            runtime=runtime, artifact_store=artifact_store, temporary_root=Path(runtime["runtimeTemporaryRoot"]),
            isolate_writes=True, candidate_first=True)
        worker = SeedFileWorker(runner, runtime, tools, messages)
        scheduler = TaskScheduler(reducer, profile, cancellation_event=runner.cancellation_event,
            review_snapshot=lambda result: workspace_snapshot(runner.workspace_for_node(result.node_id)))
        verifier = NodeResultVerifier(artifact_store, strict=True)

        def context(node, attempt_id, budget):
            snapshot = workspace_snapshot(runner.workspace_for_node(node.node_id))
            return ContextManifest(1, 1, node.node_id, snapshot_revision(snapshot), snapshot,
                                   tool_capability_scope=node.allowed_effects, budget_allocation=budget)

        def verify(result):
            return verifier.verify(VerificationContext(contract, reducer.current_plan,
                                    runner.workspace_for_node(result.node_id), result=result))

        worker.gateway = SchedulerDelegationGateway(scheduler, worker, context, verify)
        report = scheduler.execute(worker, context_builder=context, node_verifier=verify)
        result = reducer.node_result("primary")
        if not report.completed or result is None or result.session_checkpoint is None:
            raise ValueError("seed did not produce an accepted candidate handoff")
        candidate_revision = runner.validate_candidate(result)
        candidate = runner.workspace_for_node("primary")
        records = RunCoordinator._final_verification(contract=contract, plan=plan, workspace=candidate,
            report=report, runtime=runtime, results=[result], level=profile.verification_level, candidate_first=True)
        delivery = CompletionGate().evaluate(contract=contract, plan=plan, runtime_snapshot=reducer.snapshot(),
            workspace=candidate, verification_records=records, level=profile.verification_level,
            output_summary=result.output_summary, result_refs=("primary",), artifact_refs=result.artifact_refs)
        reducer.set_final_verification_records(delivery.verification_records)
        decision = reducer.decide_primary_repair(candidate_revision)
        if delivery.completed or not decision.allowed:
            raise ValueError("seed must have actual repairable acceptance failure: " + decision.reason)
        repair = PrimaryRepair(new_id("repair"), reducer.run_id, "primary", result.attempt_id, 1, 1,
            candidate_revision, result.session_checkpoint, tuple(r for r in delivery.verification_records if not r.passed))
        runner.validate_primary_repair(repair)
        reducer.apply(command(reducer.run_id, RuntimeCommandType.REQUEST_REPAIR, {"repair": repair}))
        snapshot = store.load_required()
        if workspace_snapshot(workspace) != original or snapshot["usage_ledger"]["calls"]:
            raise ValueError("fixture preparation changed original files or recorded model usage")
        provenance = {"schema": SEED_ID, "origin": "deterministic_file_fixture_not_model_output",
            "model_calls": 0, "fixture_source_sha256": content_hash(SEED_FILES),
            "run_id": reducer.run_id, "seed_attempt_id": result.attempt_id, "repair_id": repair.repair_id,
            "candidate_revision": candidate_revision, "candidate_workspace": str(candidate),
            "original_workspace_unchanged": True, "candidate_delivery_completed": delivery.completed,
            "failure_records": [r.to_dict() for r in repair.diagnostics], "checkpoint": result.session_checkpoint.to_dict(),
            "original_snapshot": original, "candidate_snapshot": workspace_snapshot(candidate),
            "receipt_count": len(worker.receipts), "initial_state_sha256": content_hash(snapshot)}
        write_report(root / "seed-state.json", snapshot)
        write_report(root / "seed-tools.json", worker.receipts)
        write_report(root / "seed.json", provenance)
        return provenance
