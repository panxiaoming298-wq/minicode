"""Deterministic-first verification and the integrated CompletionGate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable, Iterable, Protocol

from minicode.runtime_artifacts import RuntimeArtifactStore
from minicode.runtime_protocol import (
    AgentResult,
    AgentResultStatus,
    DeliveryManifest,
    PlanVersion,
    TaskContract,
    VerificationLevel,
    VerificationRecord,
)
from minicode.workspace_provider import TemporaryWorkspaceProvider


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

    def verify(self, context: VerificationContext) -> VerificationRecord:
        passed, details = self.callback(context)
        return VerificationRecord(
            verifier=self.name,
            passed=bool(passed),
            scope="final" if context.final else f"node:{context.result.node_id if context.result else 'unknown'}",
            details=str(details),
            deterministic=self.deterministic,
        )


@dataclass(slots=True)
class IsolatedCommandVerifier:
    """Run a deterministic verifier against a copy of integrated state.

    Test/lint/type tools may create caches even when conceptually read-only.
    The temporary workspace keeps those incidental writes out of the user's
    integrated tree while preserving the exact file snapshot being accepted.
    """

    name: str
    kind: str
    command: tuple[str, ...]
    timeout: float = 300.0
    deterministic: bool = True

    def verify(self, context: VerificationContext) -> VerificationRecord:
        provider = TemporaryWorkspaceProvider()
        handle = provider.create(
            context.workspace,
            owner_token=f"verifier-{self.name}",
        )
        try:
            completed = subprocess.run(
                list(self.command),
                cwd=str(handle.workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(0.1, self.timeout),
                check=False,
            )
            output = "\n".join(
                part.strip()
                for part in (completed.stdout, completed.stderr)
                if part and part.strip()
            )
            details = f"exit_code={completed.returncode}"
            if output:
                details += "\n" + output[:8_000]
            return VerificationRecord(
                verifier=self.name,
                passed=completed.returncode == 0,
                scope="final" if context.final else "node",
                details=details,
                deterministic=True,
            )
        except Exception as error:
            return VerificationRecord(
                verifier=self.name,
                passed=False,
                scope="final" if context.final else "node",
                details=f"command verifier failed ({type(error).__name__}): {error}",
                deterministic=True,
            )
        finally:
            provider.cleanup(handle)


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
        stop_on_failure: bool = True,
    ) -> tuple[VerificationRecord, ...]:
        records: list[VerificationRecord] = []
        for verifier in self.ordered(allow_model=allow_model):
            try:
                record = verifier.verify(context)
            except Exception as error:  # fail closed: verifier crashes never pass
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
        return tuple(records)


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

    _NON_TERMINAL = {
        "PENDING",
        "READY",
        "CLAIMED",
        "RUNNING",
        "WAITING_APPROVAL",
        "WAITING_INPUT",
        "VERIFYING",
        "FAILED_RETRYABLE",
    }

    def evaluate(
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
    ) -> DeliveryManifest:
        reasons: list[str] = []
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

        nodes = runtime_snapshot.get("nodes", {})
        current_ids = {node.node_id for node in plan.nodes}
        missing_ids = sorted(current_ids - set(nodes))
        if missing_ids:
            reasons.append("current Plan nodes are missing: " + ", ".join(missing_ids))
        states = [
            str(nodes[node_id].get("state", ""))
            for node_id in current_ids
            if isinstance(nodes.get(node_id), dict)
        ]
        active = [state for state in states if state in self._NON_TERMINAL]
        if active:
            reasons.append("workers/nodes are still active: " + ", ".join(sorted(set(active))))
        failed = [state for state in states if state in {"FAILED_TERMINAL", "CANCELED"}]
        if failed:
            reasons.append("terminal node failure/cancellation remains")
        if any(state != "SUCCEEDED" for state in states):
            reasons.append("not every current Plan node succeeded")

        conflict_list = tuple(conflicts)
        if conflict_list:
            reasons.append("unresolved workspace conflicts: " + ", ".join(conflict_list))
        policy_list = tuple(unresolved_policy_violations)
        if policy_list:
            reasons.append("unresolved Policy violations: " + ", ".join(policy_list))

        records = tuple(verification_records)
        if any(not record.passed for record in records):
            reasons.append("one or more final verification records failed")
        if level is VerificationLevel.STRICT:
            if not records:
                reasons.append("strict CompletionGate requires final verification records")
            if contract.acceptance_criteria and not records:
                reasons.append("TaskContract acceptance criteria are not covered")
            if not artifact_refs and not any(
                node.get("result", {}).get("evidence_refs")
                for node in nodes.values()
                if isinstance(node, dict) and isinstance(node.get("result"), dict)
            ):
                reasons.append("strict CompletionGate requires Artifact or Evidence")

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
