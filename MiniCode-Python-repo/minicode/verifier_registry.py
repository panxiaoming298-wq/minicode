"""Deterministic-first verification and the integrated CompletionGate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Protocol

from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_artifacts import RuntimeArtifactStore
from minicode.runtime_protocol import (
    AgentResult,
    AgentResultStatus,
    DeliveryManifest,
    EvidenceRef,
    PlanVersion,
    TaskContract,
    VerificationLevel,
    VerificationRecord,
    acceptance_criterion_ids,
)
from minicode.workspace_provider import (
    snapshot_revision,
    workspace_snapshot,
)
from minicode.verification_workspace import VerificationWorkspaceSession
from minicode.verification_policy import verification_environment

VERIFIER_ORDER: tuple[str, ...] = (
    "exit_code_schema",
    "unit_test",
    "integration_test",
    "lint_format",
    "type_check",
    "static_analysis",
    "invariant",
    "browser_render",
    "model_judgment",
)


@dataclass(frozen=True, slots=True)
class VerificationContext:
    contract: TaskContract
    plan: PlanVersion
    workspace: Path
    result: AgentResult | None = None
    final: bool = False
    cancellation_event: Any | None = None
    deadline: float | None = None
    permissions: Any | None = None
    artifact_store: RuntimeArtifactStore | None = None
    _workspace_session: VerificationWorkspaceSession | None = field(default=None, repr=False, compare=False)

    def check_active(self) -> None:
        import time
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            raise RuntimeError("registered check canceled")
        if self.deadline is not None and time.time() >= self.deadline:
            raise RuntimeError("registered check deadline exhausted")

    def remaining_timeout(self, timeout: float) -> float:
        import time
        self.check_active()
        remaining = timeout if self.deadline is None else min(timeout, self.deadline - time.time())
        if remaining <= 0:
            raise RuntimeError("registered check deadline exhausted")
        return remaining


class Verifier(Protocol):
    name: str
    kind: str
    deterministic: bool

    def verify(self, context: VerificationContext) -> VerificationRecord: ...


@dataclass(slots=True)
class CallableVerifier:
    name: str
    kind: str
    callback: Callable[[VerificationContext], tuple[bool, str]]
    deterministic: bool = True
    covered_criteria_ids: tuple[str, ...] = ()
    evidence_factory: Callable[[VerificationContext], tuple[EvidenceRef, ...]] | None = None

    def verify(self, context: VerificationContext) -> VerificationRecord:
        passed, details = self.callback(context)
        return VerificationRecord(
            verifier=self.name,
            passed=bool(passed),
            scope="final" if context.final else f"node:{context.result.node_id if context.result else 'unknown'}",
            details=str(details),
            deterministic=self.deterministic,
            covered_criteria_ids=self.covered_criteria_ids,
            evidence_refs=(
                self.evidence_factory(context)
                if self.evidence_factory is not None
                else ()
            ),
        )


@dataclass(slots=True)
class CommandVerifier:
    """Run against the current workspace; isolation is an explicit option.

    Temporary data and caches get their own directories. Source changes still
    invalidate the receipt; checking never rolls back the user's files.
    """

    name: str
    kind: str
    command: tuple[str, ...]
    timeout: float = 300.0
    deterministic: bool = True
    covered_criteria_ids: tuple[str, ...] = ()
    temporary_root: Path | None = None
    require_command_permissions: bool = False
    isolate: bool = False

    def verify(self, context: VerificationContext) -> VerificationRecord:
        session = (context._workspace_session or VerificationWorkspaceSession(context)) if self.isolate else None
        handle = None
        class PreparationControl:
            def is_set(self):
                context.check_active()
                return False
        preparation = PreparationControl()
        try:
            context.check_active()
            workspace = context.workspace
            if session is not None:
                handle = session.acquire(self.temporary_root, f"verifier-{self.name}")
                workspace = handle.workspace
                before = handle.base_hashes
                if workspace_snapshot(workspace, reject_aliases=True, cancellation_event=preparation) != before:
                    raise RuntimeError("verification copy does not match its captured source revision")
            else:
                before = workspace_snapshot(workspace, reject_aliases=True, cancellation_event=preparation)
            revision = snapshot_revision(before)
            if self.require_command_permissions:
                from minicode.permissions import PermissionManager
                if not isinstance(context.permissions, PermissionManager):
                    raise RuntimeError("Registered checks need the active command permission manager")
                permissions = context.permissions.fork_for_operation(hashlib.sha256(
                    json.dumps([self.name, self.command, revision], ensure_ascii=False).encode()).hexdigest())
                permissions.ensure_command(self.command[0], list(self.command[1:]), str(workspace))
                context.check_active()
                if snapshot_revision(workspace_snapshot(context.workspace, reject_aliases=True,
                                                        cancellation_event=preparation)) != revision:
                    raise RuntimeError("workspace changed while approving verification")
            environment, output_directory = verification_environment(workspace)
            context.check_active()
            completed = _run_verification_command(
                list(self.command),
                cwd=str(workspace),
                env=environment,
                # Verification is non-interactive. Inheriting the entry's stdin
                # shares Web approval/input transport and can hang child launch
                # or let a checker consume messages intended for WorkerInbox.
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=context.remaining_timeout(self.timeout),
                check=False,
                cancellation_event=context.cancellation_event,
            )
            if session is not None:
                session.settled()
            output = "\n".join(
                part.strip()
                for part in (completed.stdout, completed.stderr)
                if part and part.strip()
            )
            details = f"exit_code={completed.returncode}"
            if output:
                details += "\n" + output[:8_000]
            details += f"\nworkspace_mode={'isolated' if self.isolate else 'direct'}\noutput_directory={output_directory}"
            source_unchanged = snapshot_revision(workspace_snapshot(
                context.workspace, reject_aliases=True, cancellation_event=preparation)) == revision
            context.check_active()
            if not source_unchanged:
                details += "\nintegrated workspace changed during verification"
            command_summary = json.dumps(self.command, ensure_ascii=False)
            evidence_payload = (
                f"{command_summary}\nexit_code={completed.returncode}\n{output}"
            )
            evidence = EvidenceRef(
                evidence_type="command-exit",
                resource=f"command:{command_summary[:240]}",
                content_hash=hashlib.sha256(
                    evidence_payload.encode("utf-8")
                ).hexdigest(),
                claim=f"verification command exited with {completed.returncode}",
                producer_node_id=self.name,
                base_revision=revision,
            )
            return VerificationRecord(
                verifier=self.name,
                passed=completed.returncode == 0 and source_unchanged,
                scope="final" if context.final else "node",
                details=details,
                deterministic=True,
                evidence_refs=(evidence,),
                covered_criteria_ids=self.covered_criteria_ids,
            )
        except Exception as error:  # noqa: BLE001 - verifier process failures fail closed.
            return VerificationRecord(
                verifier=self.name,
                passed=False,
                scope="final" if context.final else "node",
                details=f"command verifier failed ({type(error).__name__}): {error}",
                deterministic=True,
                covered_criteria_ids=self.covered_criteria_ids,
            )
        finally:
            if session is not None and context._workspace_session is None:
                session.close()


@dataclass(slots=True)
class IsolatedCommandVerifier(CommandVerifier):
    """Compatibility entry point for explicitly requested isolated checks."""
    isolate: bool = True


def _run_verification_command(argv, *, cancellation_event=None, timeout, check=False, **kwargs):
    """Use bounded output and the same owned process tree as native commands."""
    from minicode.tools.run_command import _run_with_bounded_pipes
    if cancellation_event is not None and cancellation_event.is_set():
        raise RuntimeError("registered check canceled before process start")
    if os.name == "nt":
        # The owned Windows launcher is a real process even when its requested
        # executable is missing. Reject that configuration before launch rather
        # than certifying the launcher's error as a completed acceptance check.
        cwd = kwargs.get("cwd", str(Path.cwd()))
        environment = kwargs.get("env") or os.environ
        executable = os.path.join(cwd, argv[0]) if os.path.dirname(argv[0]) else argv[0]
        search_path = os.pathsep.join((cwd, environment.get("PATH", os.defpath)))
        if shutil.which(executable, path=search_path) is None:
            raise FileNotFoundError(f"verification executable unavailable: {argv[0]}")
    result = _run_with_bounded_pipes(argv[0], list(argv[1:]), cwd=kwargs.get("cwd", str(Path.cwd())),
        timeout=timeout, cancellation_event=cancellation_event, env=kwargs.get("env"))
    if not result.commandExecutionSettled:
        raise RuntimeError("registered check termination/output drain unconfirmed: " + result.output)
    if result.failureKind == "canceled":
        raise RuntimeError("registered check canceled: " + result.output)
    if result.timedOut:
        raise subprocess.TimeoutExpired(argv, timeout, output=result.output)
    if check and result.commandExitCode:
        raise subprocess.CalledProcessError(result.commandExitCode, argv, output=result.output)
    return subprocess.CompletedProcess(argv, result.commandExitCode, result.output, "")


class VerifierRegistry:
    def __init__(self, verifiers: Iterable[Verifier] = ()) -> None:
        self._verifiers: list[Verifier] = []
        for verifier in verifiers:
            self.register(verifier)

    def register(self, verifier: Verifier) -> None:
        if verifier.kind not in VERIFIER_ORDER:
            raise ValueError(f"unsupported verifier kind: {verifier.kind}")
        self._verifiers.append(verifier)

    def ordered(self, *, allow_model: bool) -> tuple[Verifier, ...]:
        rank = {name: index for index, name in enumerate(VERIFIER_ORDER)}
        selected = [
            verifier
            for verifier in self._verifiers
            if allow_model or verifier.kind != "model_judgment"
        ]
        return tuple(sorted(selected, key=lambda verifier: (rank[verifier.kind], verifier.name)))

    def run(
        self,
        context: VerificationContext,
        *,
        allow_model: bool = False,
        stop_on_failure: bool = False,
    ) -> tuple[VerificationRecord, ...]:
        if not any(getattr(verifier, "isolate", False) for verifier in self.ordered(allow_model=allow_model)):
            return self._run(context, allow_model=allow_model, stop_on_failure=stop_on_failure)
        session = VerificationWorkspaceSession(context)
        try:
            records = self._run(replace(context, _workspace_session=session), allow_model=allow_model,
                                stop_on_failure=stop_on_failure)
        finally:
            session.close()
        if records and session.cleanup_warnings:
            records = (*records[:-1], replace(records[-1], details=records[-1].details + "\n" +
                "\n".join(session.cleanup_warnings)))
        return records

    def _run(self, context, *, allow_model, stop_on_failure):
        records: list[VerificationRecord] = []
        for verifier in self.ordered(allow_model=allow_model):
            try:
                context.check_active()
                record = verifier.verify(context)
                context.check_active()
            except Exception as error:  # noqa: BLE001 - verifier adapters fail closed.
                record = VerificationRecord(
                    verifier=verifier.name,
                    passed=False,
                    scope="final" if context.final else "node",
                    details=f"verifier crashed ({type(error).__name__}): {error}",
                    deterministic=verifier.deterministic,
                )
            records.append(record)
            if stop_on_failure and not record.passed:
                break
            try:
                context.check_active()
            except RuntimeError:
                break  # Cancellation/deadline stops the whole batch.
        return tuple(records)


def submitted_workspace_error(result: Mapping[str, Any], workspace: Path) -> str:
    """Recheck a Runtime-issued direct handoff; receipt text grants no authority."""
    refs = [ref for ref in result.get("evidence_refs", ()) if ref.get("evidence_type") == "workspace-submission"]
    if not refs:
        return ""
    if (len(refs) != 1 or result.get("node_id") != "primary" or refs[0].get("resource") != "."
        or refs[0].get("producer_node_id") != result.get("node_id")
        or refs[0].get("base_revision") != result.get("base_revision")):
        return "direct workspace submission has invalid owner binding"
    if refs[0].get("content_hash") != _workspace_snapshot_revision(Path(workspace)):
        return "workspace changed after explicit direct submission"
    return ""


class NodeResultVerifier:
    name = "structured-node-result"
    kind = "exit_code_schema"
    deterministic = True

    def __init__(self, artifact_store: RuntimeArtifactStore | None = None, *, strict: bool = False) -> None:
        self.artifact_store = artifact_store
        self.strict = strict

    def verify(self, context: VerificationContext) -> VerificationRecord:
        result = context.result
        reasons: list[str] = []
        if result is None:
            reasons.append("missing AgentResult")
        elif result.status is not AgentResultStatus.SUCCEEDED:
            reasons.append(f"worker status is {result.status.value}")
        if result is not None and self.artifact_store is not None:
            for artifact in result.artifact_refs:
                if not self.artifact_store.exists(artifact):
                    reasons.append(f"artifact unavailable or corrupt: {artifact.artifact_id}")
        if result is not None:
            submission_error = submitted_workspace_error(result.to_dict(), context.workspace)
            if submission_error:
                reasons.append(submission_error)
        if result is not None and result.consultation_summary_ref is not None:
            from minicode.consultation_reading import valid_summary_ref

            node = next((node for node in context.plan.nodes if node.node_id == result.node_id), None)
            if node is None or not valid_summary_ref(result, node) or self.artifact_store is None:
                reasons.append("invalid paged consultation decision binding")
        if self.strict and result is not None and not (result.evidence_refs or result.artifact_refs):
            reasons.append("strict node verification requires Evidence or Artifact")
        return VerificationRecord(
            verifier=self.name,
            passed=not reasons,
            scope=f"node:{result.node_id if result else 'unknown'}",
            details="; ".join(reasons) if reasons else "structured result is valid",
            deterministic=True,
            evidence_refs=result.evidence_refs if result else (),
        )


class CompletionGate:
    """Make the only final-delivery decision against integrated state."""

    _NON_TERMINAL: ClassVar[set[str]] = {
        "PENDING",
        "READY",
        "CLAIMED",
        "RUNNING",
        "WAITING_APPROVAL",
        "WAITING_INPUT",
        "WAITING_CHILDREN",
        "VERIFYING",
        "FAILED_RETRYABLE",
    }

    def evaluate(self, **kwargs) -> DeliveryManifest:
        return self._evaluate(**kwargs, require_integration=True)

    def check_candidate(self, **kwargs) -> tuple[bool, tuple[VerificationRecord, ...], tuple[str, ...]]:
        """Check candidate acceptance only; this never authorizes final delivery."""
        check = self._evaluate(**kwargs, require_integration=False)
        return check.completed, check.verification_records, check.reasons

    def _evaluate(
        self,
        *,
        contract: TaskContract,
        plan: PlanVersion,
        runtime_snapshot: dict,
        workspace: Path,
        verification_records: Iterable[VerificationRecord],
        level: VerificationLevel,
        output_summary: str,
        result_refs: Iterable[str] = (),
        artifact_refs=(),
        conflicts: Iterable[str] = (),
        unresolved_policy_violations: Iterable[str] = (),
        require_integration: bool,
    ) -> DeliveryManifest:
        restored_authority = None
        restore_error = None

        def restore_authority():
            nonlocal restored_authority, restore_error
            from minicode.runtime_state import RuntimeReducer
            if restore_error is not None:
                raise restore_error
            if restored_authority is None:
                try:
                    restored_authority = RuntimeReducer.restore(runtime_snapshot)
                except (ValueError, RuntimeError, TypeError, KeyError) as error:
                    restore_error = error
                    raise
            return restored_authority

        reasons: list[str] = []
        records = tuple(verification_records)
        state_schema = runtime_snapshot.get("schema_version")
        if state_schema == "minicode-runtime-state-v21":
            try:
                authority = restore_authority()
                if authority.contract != contract or authority.current_plan != plan:
                    raise ValueError("observed review differs from the current contract/plan")
                state_schema = runtime_snapshot.get("review_base_schema")
            except (ValueError, RuntimeError, TypeError, KeyError) as error:
                reasons.append("observed review authority is invalid: " + str(error))
        if state_schema == "minicode-runtime-state-v20":
            from minicode.runtime_state import RuntimeReducer
            try:
                collaboration_authority = restore_authority()
                if collaboration_authority.contract != contract or collaboration_authority.current_plan != plan:
                    raise ValueError("collaboration differs from the current contract/plan")
                state_schema = runtime_snapshot.get("agent_collaboration_base_schema")
            except (ValueError, RuntimeError, TypeError, KeyError) as error:
                reasons.append("collaboration authority is invalid: " + str(error))
        superseded_by_human: set[str] = set()
        if state_schema == "minicode-runtime-state-v19":
            state_schema = runtime_snapshot.get("user_steering_base_schema")
            try:
                from minicode.runtime_state import RuntimeReducer
                authority = restore_authority()
                human = authority._state.user_steering
                if authority.contract != contract or authority.current_plan != plan:
                    raise ValueError("human requirements differ from the current contract/plan")
                if not human["sealed"] or any(r["status"] in {"accepted", "applied"}
                                              for r in human["inputs"].values()):
                    raise ValueError("human inputs remain unresolved or unsealed")
                superseded_by_human = set(human["superseded_nodes"])
                human_record = VerificationRecord("human-steering", True, "final",
                    details=f"task revision {human['revision']}; all accepted human inputs resolved", deterministic=True)
            except (ValueError, RuntimeError, TypeError, KeyError) as error:
                human_record = VerificationRecord("human-steering", False, "final", details=str(error), deterministic=True)
                reasons.append("human steering authority is invalid: " + str(error))
            records = tuple(r for r in records if r.verifier != "human-steering") + (human_record,)
        if state_schema in {f"minicode-runtime-state-v{i}" for i in range(12, 19)}:
            state_schema = runtime_snapshot.get("agent_base_schema")
        if state_schema == "minicode-runtime-state-v11":
            state_schema = runtime_snapshot.get("orchestration_base_schema")
        artifact_refs = tuple(artifact_refs)
        result_refs = tuple(result_refs)
        if not Path(workspace).is_dir():
            reasons.append("integrated workspace is unavailable")
        if runtime_snapshot.get("current_plan_version") != plan.plan_version:
            reasons.append("PlanVersion is not current")
        if runtime_snapshot.get("contract_revision") != contract.contract_revision:
            reasons.append("TaskContract revision is not current")
        if runtime_snapshot.get("canceled"):
            reasons.append("Runtime was canceled")
        if runtime_snapshot.get("paused"):
            reasons.append("Runtime is paused")
        usage = runtime_snapshot.get("usage_summary", {})
        if contract.budget.max_tokens is not None and not usage.get("tokens_known", True):
            reasons.append("model token usage is incomplete")
        if contract.budget.max_cost is not None and not usage.get("cost_known", True):
            reasons.append("model cost is unknown")
        if contract.budget.max_tokens is not None and (
            int(runtime_snapshot.get("tokens_used", 0) or 0) > contract.budget.max_tokens
        ):
            reasons.append("max_tokens budget exceeded")
        if contract.budget.max_cost is not None and (
            float(runtime_snapshot.get("cost_used", 0.0) or 0.0) > contract.budget.max_cost
        ):
            reasons.append("max_cost budget exceeded")

        nodes = runtime_snapshot.get("nodes", {})
        for node_id, node in nodes.items():
            if node_id in superseded_by_human:
                continue
            submission_error = submitted_workspace_error(node.get("result") or {}, Path(workspace))
            if submission_error:
                reasons.append(f"{node_id}: {submission_error}")
        current_ids = {node.node_id for node in plan.nodes}
        missing_ids = sorted(current_ids - set(nodes))
        if missing_ids:
            reasons.append("current Plan nodes are missing: " + ", ".join(missing_ids))
        resolved_advisory: set[str] = set()
        discarded_builders: frozenset[str] = frozenset()
        if (runtime_snapshot.get("schema_version") in {f"minicode-runtime-state-v{i}" for i in range(6, 22)}
            or runtime_snapshot.get("candidate_quarantines") or runtime_snapshot.get("candidate_selections")
            or any(node.alternative_group or node.review_policy is not None for node in plan.nodes)):
            # Reuse the authority's restoration validation, including immutable
            # registration, delivered attempt and explicit primary decision.
            from minicode.runtime_state import RuntimeReducer

            try:
                restored = restore_authority()
                if restored.current_plan != plan:
                    raise ValueError("gate plan differs from the authoritative snapshot")
                blockers = set(restored.completion_blockers())
                assessment = restored.current_review_assessment
                if assessment:
                    from minicode.workspace_provider import snapshot_revision, workspace_snapshot
                    if snapshot_revision(workspace_snapshot(Path(workspace))) != assessment["candidate_revision"]:
                        reasons.append("delivery workspace differs from its observed review candidate")
                resolved_advisory = {node_id for node_id in current_ids if node_id not in blockers
                                     and nodes[node_id].get("state") == "FAILED_TERMINAL"}
                discarded_builders = (restored.discarded_builder_ids() | restored.replaced_builder_ids()
                    | restored.resolved_agent_failures(include_dismissed=True) | restored.dismissed_agent_ids())
                if restored.pending_candidate_quarantines():
                    reasons.append("unreconciled candidate side effects remain")
                if any(node.alternative_group and node.alternative_group not in runtime_snapshot.get("candidate_selections", {}) for node in plan.nodes):
                    reasons.append("alternative candidates require an explicit native selection")
            except (ValueError, RuntimeError, TypeError, KeyError) as error:
                reasons.append(f"advisory disposition is not valid: {error}")
        states = [
            str(nodes[node_id].get("state", ""))
            for node_id in current_ids
            if isinstance(nodes.get(node_id), dict)
            and node_id not in resolved_advisory and node_id not in discarded_builders
        ]
        active = [state for state in states if state in self._NON_TERMINAL]
        if active:
            reasons.append("workers/nodes are still active: " + ", ".join(sorted(set(active))))
        failed = [state for state in states if state in {"FAILED_TERMINAL", "CANCELED"}]
        if failed:
            reasons.append("terminal node failure/cancellation remains")
        if any(state != "SUCCEEDED" for state in states):
            reasons.append("not every current Plan node succeeded")
        if runtime_snapshot.get("candidate_first"):
            reviewed_snapshot = None
            for node in plan.nodes:
                if not node.is_writer or not node.node_id.startswith("builder-") or node.node_id in discarded_builders:
                    continue
                patch = runtime_snapshot.get("patch_records", {}).get("patch-" + node.node_id, {})
                checks = patch.get("checks", {})
                result = nodes.get(node.node_id, {}).get("result") or {}
                checked = bool(checks) and all(isinstance(check, Mapping) and check.get("passed") is True
                                               for check in checks.values())
                if (not checked and checks and all(isinstance(check, Mapping)
                        and check.get("status") == "not_configured" and check.get("passed") is False
                        and check.get("scope") == "registered-combination" and check.get("checks") == []
                        for check in checks.values())):
                    # No optional command was configured. Only a settled,
                    # revision-bound independent final review can cover this
                    # combination. The empty command list remains NOT passed;
                    # it never releases an early applied-and-checked dependency.
                    try:
                        if reviewed_snapshot is None:
                            reviewed_snapshot = workspace_snapshot(Path(workspace), reject_aliases=True)
                        checked = _independent_review_covers_patch(
                            patch=patch.get("patch", {}), snapshot=reviewed_snapshot,
                            nodes=nodes, plan=plan, contract=contract, records=records)
                    except (ValueError, RuntimeError, OSError):
                        checked = False
                if (patch.get("state") != "applied" or not checks
                    or not checked
                    or patch.get("patch", {}).get("owner_attempt_id") != result.get("attempt_id")):
                    reasons.append("Builder has no accepted, applied and checked patch: " + node.node_id)
        if require_integration:
            pending_writers = [node.node_id for node in plan.nodes if node.is_writer
                               and not (runtime_snapshot.get("candidate_first") and node.node_id.startswith("builder-"))
                               and nodes.get(node.node_id, {}).get("workspace_committed") is False]
            if pending_writers:
                reasons.append("writer workspace is not integrated: " + ", ".join(pending_writers))
            if runtime_snapshot.get("candidate_first"):
                if runtime_snapshot.get("integration_status") != "committed":
                    reasons.append("candidate integration is not durably committed")
                elif runtime_snapshot.get("integration_revision") != _workspace_snapshot_revision(Path(workspace)):
                    reasons.append("integrated workspace changed from the accepted candidate")

        conflict_list = tuple(conflicts)
        if conflict_list:
            reasons.append("unresolved workspace conflicts: " + ", ".join(conflict_list))
        policy_list = tuple(unresolved_policy_violations)
        if policy_list:
            reasons.append("unresolved Policy violations: " + ", ".join(policy_list))

        if (state_schema == "minicode-runtime-state-v10"
            or any((node.get("result") or {}).get("consultation_summary_ref") is not None for node in nodes.values())
            or any(event.get("event_type") == "ConsultationSummaryRead"
                or (event.get("payload") or {}).get("consultation_summary_ref") is not None
                for event in runtime_snapshot.get("events", []))):
            from minicode.runtime_state import RuntimeReducer

            try:
                read_authority = restore_authority()
                if read_authority.contract != contract or read_authority.current_plan != plan:
                    raise ValueError("paged consultation differs from authoritative contract")
                reading = read_authority.consultation_read_status()
                read_record = VerificationRecord("consultation-summary-reading", reading["satisfied"], "final",
                    details=json.dumps(reading, ensure_ascii=False, sort_keys=True), deterministic=True)
            except (ValueError, RuntimeError, TypeError, KeyError) as error:
                read_record = VerificationRecord("consultation-summary-reading", False, "final", details=str(error), deterministic=True)
            records = tuple(r for r in records if r.verifier != "consultation-summary-reading") + (read_record,)
            if not read_record.passed:
                reasons.append("Paged consultation decision has not been fully read: " + read_record.details)
        if (contract.required_consultation is not None or state_schema == "minicode-runtime-state-v8"
            or runtime_snapshot.get("required_consultation_enabled") is True):
            from minicode.runtime_state import RuntimeReducer

            try:
                process_authority = restore_authority()
                if (process_authority.contract != contract or process_authority.current_plan != plan
                    or state_schema not in {"minicode-runtime-state-v8", "minicode-runtime-state-v9", "minicode-runtime-state-v10"}):
                    raise ValueError("required consultation contract differs from authoritative state")
                process = process_authority.required_consultation_status()
                if process is None:
                    raise ValueError("required consultation contract is missing")
                process_record = VerificationRecord("required-consultation", process["satisfied"], "final",
                    details=json.dumps(process, ensure_ascii=False, sort_keys=True), deterministic=True)
            except (ValueError, RuntimeError, TypeError, KeyError) as error:
                process_record = VerificationRecord("required-consultation", False, "final",
                    details=f"invalid required consultation evidence: {error}", deterministic=True)
            # Recompute from state even if callers supply a purported pass.
            records = tuple(record for record in records if record.verifier != "required-consultation") + (process_record,)
            if not process_record.passed:
                reasons.append("TaskContract required consultation is not satisfied: " + process_record.details)
        command_revisions = {
            evidence.base_revision
            for record in records if record.passed and record.scope == "final"
            for evidence in record.evidence_refs if evidence.evidence_type == "command-exit"
        }
        if command_revisions and command_revisions != {_workspace_snapshot_revision(Path(workspace))}:
            reasons.append("verification command evidence is stale for the integrated workspace")
        if contract.expected_outputs:
            output_record = verify_expected_outputs(contract, Path(workspace))
            # Recheck at the gate, including when the caller supplied its own registry.
            records = tuple(r for r in records if r.verifier != "expected-outputs") + (output_record,)
            if not output_record.passed:
                reasons.append(output_record.details)
        if any(not record.passed for record in records):
            reasons.append("one or more final verification records failed")
        required_ids = acceptance_criterion_ids(contract.acceptance_criteria)
        covered_ids = {
            criterion_id
            for record in records
            if record.passed and record.scope == "final"
            for criterion_id in record.covered_criteria_ids
        }
        missing_criteria = [criterion_id for criterion_id in required_ids if criterion_id not in covered_ids]
        if missing_criteria:
            reasons.append(
                "TaskContract acceptance criteria are not covered: " + ", ".join(missing_criteria)
            )
        if level is VerificationLevel.STRICT:
            if not records:
                reasons.append("strict CompletionGate requires final verification records")
            non_self_evidence = tuple(
                evidence
                for record in records
                if record.passed
                for evidence in record.evidence_refs
                if evidence.evidence_type != "worker-assertion"
            )
            if not artifact_refs and not non_self_evidence:
                reasons.append(
                    "strict CompletionGate requires non-self-certifying Evidence or Artifact"
                )
            requires_integrated_proof = bool(contract.expected_outputs) or any(
                node.is_writer for node in plan.nodes
            )
            if requires_integrated_proof and not any(
                record.passed
                and record.deterministic
                and record.scope == "final"
                and any(
                    evidence.evidence_type != "worker-assertion"
                    for evidence in record.evidence_refs
                )
                for record in records
            ):
                reasons.append(
                    "Writer/expected outputs require deterministic integrated verification"
                )

        return DeliveryManifest(
            run_id=contract.envelope.run_id,
            plan_version=plan.plan_version,
            completed=not reasons,
            output_summary=output_summary,
            result_refs=result_refs,
            artifact_refs=artifact_refs,
            verification_records=records,
            reasons=tuple(reasons),
        )


def _independent_review_covers_patch(*, patch, snapshot, nodes, plan, contract, records) -> bool:
    """Final-only alternative to absent optional commands, never to failed tests."""
    final_definition = next((node for node in plan.nodes if node.node_id == "final-verifier"
        and node.role == "verifier" and "primary" in node.dependencies and not node.is_writer), None)
    final_node = nodes.get("final-verifier", {})
    final_result = final_node.get("result") or {}
    revision = snapshot_revision(snapshot)
    if (final_definition is None or final_node.get("state") != "SUCCEEDED"
        or final_result.get("status") != "succeeded"
        or final_result.get("node_id") != "final-verifier"
        or final_result.get("attempt_id") != final_node.get("current_attempt_id")
        or final_result.get("run_id") != contract.envelope.run_id
        or final_result.get("plan_version") != plan.plan_version
        or final_result.get("contract_revision") != contract.contract_revision
        or final_result.get("base_revision") != revision):
        return False
    required = set(acceptance_criterion_ids(contract.acceptance_criteria))
    summary_hash = hashlib.sha256(str(final_result.get("output_summary", "")).encode("utf-8")).hexdigest()
    authoritative = final_result.get("verification_records") or []
    reviewed = any(record.passed and record.scope == "final"
        and record.verifier == "model-verifier:final-verifier"
        and required <= set(record.covered_criteria_ids)
        and record.to_dict() in authoritative
        and any(ref.producer_node_id == "final-verifier" and ref.base_revision == revision
                and ref.content_hash == summary_hash for ref in record.evidence_refs)
        for record in records)
    hashes = patch.get("content_hashes")
    return (reviewed and patch.get("target_candidate_id") == "primary"
        and isinstance(hashes, Mapping) and bool(hashes)
        and set(hashes) == set(patch.get("changed_paths", ()))
        and all(snapshot.get(path) == digest for path, digest in hashes.items()))


def verify_expected_outputs(contract: TaskContract, workspace: Path) -> VerificationRecord:
    """Verify declared file outputs; presence does not imply behavioral correctness."""
    reasons: list[str] = []
    evidence: list[EvidenceRef] = []
    for declared in contract.expected_outputs:
        try:
            relative = Path(declared)
            path = workspace / relative
            if (
                not declared.strip()
                or any(part == ".." or ":" in part for part in relative.parts)
                or is_unsafe_stored_path(path, boundary=workspace, reject_hardlinks=True)
                or not path.is_file()
            ):
                reasons.append(f"expected output is missing or not a safe workspace file: {declared}")
                continue
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            evidence.append(EvidenceRef(
                evidence_type="file-content", resource=str(path.relative_to(workspace)).replace("\\", "/"),
                content_hash=digest, claim="declared output exists as a readable workspace file",
                producer_node_id="expected-outputs", base_revision=digest,
            ))
        except (OSError, ValueError, RuntimeError) as error:
            reasons.append(f"expected output cannot be verified: {declared} ({error})")
    return VerificationRecord(
        verifier="expected-outputs", passed=not reasons, scope="final",
        details="; ".join(reasons) if reasons else "declared output files exist; content behavior requires task checks",
        evidence_refs=tuple(evidence), deterministic=True,
    )


def workspace_snapshot_evidence(
    context: VerificationContext,
    *,
    producer: str = "integrated-runtime-state",
) -> tuple[EvidenceRef, ...]:
    snapshot = workspace_snapshot(context.workspace)
    revision = snapshot_revision(snapshot)
    return (
        EvidenceRef(
            evidence_type="workspace-snapshot",
            resource=str(context.workspace.resolve()),
            content_hash=revision,
            claim="workspace snapshot used by final verification",
            producer_node_id=producer,
            base_revision=revision,
        ),
    )


def _workspace_snapshot_revision(workspace: Path) -> str:
    return snapshot_revision(workspace_snapshot(workspace))
