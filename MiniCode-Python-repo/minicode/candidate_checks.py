"""Primary-only access to registered checks, without submission or acceptance."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import time
from weakref import WeakSet

from minicode.registered_checks import register_configured_checks
from minicode.runtime_protocol import ReplayPolicy, ToolEffect, VerificationRecord, acceptance_criterion_ids
from minicode.tooling import ToolDefinition, ToolMetadata, ToolResult
from minicode.verifier_registry import VerificationContext, VerifierRegistry

_NATIVE_CHECK_HANDLERS = WeakSet()


def native_verification_timeout(tool):
    """Bounded allowance only for exact native check/submission handlers."""
    if tool is None:
        return None
    handler = getattr(tool, "run", None)
    try:
        if handler in _NATIVE_CHECK_HANDLERS:
            return 900.0
    except TypeError:
        pass  # An extension's callable need not be hashable or weak-referenceable.
    from minicode.candidate_submission import CandidateSubmission
    owner = getattr(handler, "__self__", None)
    if (isinstance(owner, CandidateSubmission) and owner._preflight is not None
        and getattr(handler, "__func__", None) is CandidateSubmission._submit):
        return 900.0
    return None


def create_candidate_checks_tool(*, authorize, runtime, reducer, invocation, cancellation, artifact_store=None,
                                 baseline_snapshot=None):
    def validate(value):
        if not isinstance(value, dict) or value:
            raise ValueError("run_acceptance_checks takes no arguments; Runtime owns the commands and candidate")
        return {}

    def run(_arguments, context):
        stop = context.cancellation_event if context.cancellation_event is not None else cancellation
        revision = authorize(context)
        registry = VerifierRegistry()
        records = [replace(record, scope="candidate-precheck")
                   for record in register_configured_checks(registry, runtime, reducer.contract,
                        workspace=Path(context.cwd), project_checks=True, baseline_snapshot=baseline_snapshot)]
        deadlines = [cap.deadline for cap in (reducer.contract.budget, invocation.budget) if cap.deadline is not None]
        deadline = min(deadlines) if deadlines else None
        verification = VerificationContext(contract=reducer.contract, plan=reducer.current_plan,
                                           workspace=Path(context.cwd), final=False,
                                           cancellation_event=stop, deadline=deadline,
                                           permissions=context.permissions, artifact_store=artifact_store)
        if not records:
            for verifier in registry.ordered(allow_model=False):
                remaining = deadline - time.time() if deadline is not None else None
                if stop.is_set() or (remaining is not None and remaining <= 0):
                    records.append(VerificationRecord(verifier="candidate-precheck-budget", passed=False,
                        scope="candidate-precheck", details="Canceled or deadline exhausted before registered check", deterministic=True))
                    break
                if remaining is not None:
                    verifier = replace(verifier, timeout=min(verifier.timeout, remaining))
                record = verifier.verify(verification)
                records.append(replace(record, scope="candidate-precheck"))
                if not record.passed:
                    break
        # Checks can write generated outputs in the current candidate. Both its
        # source revision and primary scope must remain current for the receipt.
        current = authorize(context)
        covered = {criterion for record in records if record.passed for criterion in record.covered_criteria_ids}
        required = set(acceptance_criterion_ids(reducer.contract.acceptance_criteria))
        all_covered = bool(required) and required <= covered
        # Built-in smoke checks deliberately do not certify business criteria.
        # Their pass is useful intermediate evidence; final coverage is still
        # independently enforced by CompletionGate.
        smoke_only = bool(records) and all(record.verifier == "npm-build-and-browser" for record in records)
        passed = (bool(records) and all(record.passed for record in records) and (required <= covered or smoke_only)
                  and current == revision and not stop.is_set()
                  and (deadline is None or time.time() < deadline))
        from minicode.verification_scope import observed_changes, select_project_scope
        selected = select_project_scope(context.cwd, reducer.contract,
            changed_paths=observed_changes(context.cwd, baseline_snapshot))
        not_required = (not records and not runtime.get("runtimeVerificationCommands")
                        and not selected.build and not selected.unit and current == revision
                        and not stop.is_set() and (deadline is None or time.time() < deadline))
        ok = passed or not_required
        from minicode.run_images import publish_project_check_images
        images, image_errors = publish_project_check_images(records, context, artifact_store)
        return ToolResult(ok=ok, verificationFailed=not ok,
            contextCandidateRevision=revision if ok else None,
            output=json.dumps({"status": "not_required" if not_required else "passed" if passed else "failed", "candidate_revision": revision,
                "check_scope": selected.to_dict(),
                "candidate_unchanged": current == revision, "all_criteria_covered": all_covered,
                "coverage_status": "not_declared" if not required else "complete" if all_covered else "incomplete",
                "checks": [asdict(record) for record in records], "counts_as_final_acceptance": False,
                "images": images, "image_errors": image_errors, "visualCheck": "not_checked",
                 "next": ("No automatic code/browser checks apply to these document-only changes. No criterion was certified; final acceptance remains independent."
                          if not_required else "Checks passed for this revision. Business criteria and interactions still require independent verification. Call submit_candidate when ready."
                          if passed else "No checks configured: automatic checks support npm projects with a build script producing dist/index.html. Other projects need registered verification commands."
                          if not records else "Repair the failed or uncovered requirements before submitting; no candidate was submitted.")},
                ensure_ascii=False))

    _NATIVE_CHECK_HANDLERS.add(run)
    return ToolDefinition(name="run_acceptance_checks",
        description=("Run registered checks in the current candidate workspace, with isolation only when configured. "
                     "Temporary data and caches use separate output directories; source changes invalidate checks. "
                     "Without configured checks, coordinator mode gives npm frontends "
                     "task-scoped dependency/build checks and browser checks when UI behavior or appearance is affected, "
                     "subject to command approval. Supports npm build producing dist/index.html; does not cover all interactions. "
                     "Saved images include IDs readable with view_image(image_id=...); inspect their pixels before visual findings. "
                     "To finish ready changes, prefer submit_candidate, which requests the same registered checks and handoff. "
                     "This diagnostic tool does not submit. No arguments or arbitrary commands. "
                     "After a pass, explicit submit_candidate and final verification/integration still follow. "
                     "May follow edits in an ordered batch, before submit_candidate LAST. Failed checks stop that batch."),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False}, validator=validate, run=run,
        metadata=ToolMetadata(name="run_acceptance_checks", description="Registered candidate checks",
            effect=ToolEffect.WORKSPACE_WRITE,
            replay_policy=ReplayPolicy.NEVER_AUTO_RETRY, context_result_kind="candidate_check"))
