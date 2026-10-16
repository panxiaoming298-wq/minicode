"""Register the same configured deterministic checks for prechecks and final acceptance."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path
import time

from minicode.runtime_protocol import VerificationRecord, acceptance_criterion_ids
from minicode.turn_telemetry import measured
from minicode.verifier_registry import CommandVerifier
from minicode.verification_policy import verification_isolated


def register_configured_checks(registry, runtime, contract, *, workspace=None, project_checks=False,
                               require_command_permissions=False, changed_paths=None, baseline_snapshot=None):
    configuration_failures: list[VerificationRecord] = []
    verification_isolated(runtime)
    criterion_ids = acceptance_criterion_ids(contract.acceptance_criteria)
    configured = runtime.get("runtimeVerificationCommands", ())
    # Web continuation persists unset optional fields as null. Treat that as
    # no configured checks, while still rejecting malformed non-list values.
    if configured is None:
        configured = ()
    if not isinstance(configured, (list, tuple)):
        raise TypeError("runtimeVerificationCommands must be a list")
    for index, item in enumerate(configured):
        if not isinstance(item, Mapping):
            raise TypeError("each runtime verification command must be a mapping")
        raw_command = item.get("command")
        if "browser" not in item and (not isinstance(raw_command, (list, tuple)) or not raw_command):
            raise TypeError("verification command must be a non-empty argument list")
        covers = item.get("covers")
        covered_ids: tuple[str, ...] = ()
        if contract.acceptance_criteria:
            invalid_reason = ""
            if not isinstance(covers, (list, tuple)) or not covers:
                invalid_reason = (
                    "verification command must declare non-empty covers "
                    "using 1-based acceptance-criterion numbers"
                )
            else:
                parsed_covers: list[int] = []
                for value in covers:
                    if not isinstance(value, int) or isinstance(value, bool):
                        invalid_reason = "verification command covers must contain integers"
                        break
                    number = value
                    if number < 1 or number > len(criterion_ids):
                        invalid_reason = (
                            "verification command covers contains an out-of-range "
                            f"criterion number: {number}"
                        )
                        break
                    parsed_covers.append(number)
                if not invalid_reason:
                    covered_ids = tuple(
                        criterion_ids[number - 1]
                        for number in dict.fromkeys(parsed_covers)
                    )
            if invalid_reason:
                configuration_failures.append(
                    VerificationRecord(
                        verifier=str(
                            item.get("name")
                            or f"verification-command-{index + 1}"
                        ),
                        passed=False,
                        scope="final",
                        details=invalid_reason,
                        deterministic=True,
                    )
                )
                continue
        if "browser" in item:
            from minicode.browser_verification import BrowserScenarioVerifier, browser_declaration
            if raw_command is not None or item.get("kind", "browser_render") != "browser_render":
                raise ValueError("Registered browser checks cannot supply an arbitrary command/kind")
            declaration = browser_declaration(item["browser"])
            name = str(item.get("name") or f"browser-check-{index + 1}")
            import re
            import math
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
                raise ValueError("Registered browser check needs a bounded name")
            timeout = item.get("timeout", 300.0)
            if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 300:
                raise ValueError("Registered browser timeout must be finite and at most 300 seconds")
            registry.register(BrowserScenarioVerifier(name="browser-scenario-" + name, declaration=declaration,
                required_numbers=tuple(criterion_ids.index(identifier) + 1 for identifier in covered_ids),
                covered_criteria_ids=covered_ids, timeout=timeout,
                temporary_root=Path(str(runtime["runtimeTemporaryRoot"])) if runtime.get("runtimeTemporaryRoot") else None,
                isolate=verification_isolated(runtime, item)))
            continue
        registry.register(
            CommandVerifier(
                name=str(item.get("name") or f"verification-command-{index + 1}"),
                kind=str(item.get("kind") or "integration_test"),
                command=tuple(str(part) for part in raw_command),
                timeout=float(item.get("timeout", 300.0)),
                covered_criteria_ids=covered_ids,
                temporary_root=Path(str(runtime["runtimeTemporaryRoot"])) if runtime.get("runtimeTemporaryRoot") else None,
                require_command_permissions=require_command_permissions,
                isolate=verification_isolated(runtime, item),
            )
        )
    if project_checks:
        from minicode.project_verification import register_project_check
        if baseline_snapshot is not None and workspace is not None:
            from minicode.verification_scope import observed_changes
            changed_paths = observed_changes(workspace, baseline_snapshot)
        register_project_check(registry, runtime, workspace, contract, changed_paths=changed_paths)
    return tuple(configuration_failures)


def run_combination_checks(*, workspace, runtime, reducer, cancellation, permissions=None, project_checks=False,
                           artifact_store=None, baseline_snapshot=None):
    """Execute registered commands using the configured workspace policy."""
    from minicode.verifier_registry import VerificationContext, VerifierRegistry
    from minicode.workspace_provider import snapshot_revision, workspace_snapshot

    if workspace is None:
        return {"passed": False, "scope": "registered-combination", "reason": "primary candidate unavailable"}
    revision = snapshot_revision(workspace_snapshot(Path(workspace)))
    registry = VerifierRegistry()
    records = [replace(record, scope="combination")
               for record in register_configured_checks(registry, runtime, reducer.contract,
                    workspace=workspace, project_checks=project_checks, baseline_snapshot=baseline_snapshot)]
    context = VerificationContext(contract=reducer.contract, plan=reducer.current_plan,
                                  workspace=Path(workspace), final=False, cancellation_event=cancellation,
                                  deadline=reducer.contract.budget.deadline, permissions=permissions,
                                  artifact_store=artifact_store)
    deadline = reducer.contract.budget.deadline
    if not records:
        for verifier in registry.ordered(allow_model=False):
            remaining = deadline - time.time() if deadline is not None else None
            if cancellation.is_set() or (remaining is not None and remaining <= 0):
                records.append(VerificationRecord(verifier="combination-budget", passed=False,
                    scope="combination", details="Canceled or deadline exhausted", deterministic=True))
                break
            if remaining is not None:
                verifier = replace(verifier, timeout=min(verifier.timeout, remaining))
            records.append(replace(verifier.verify(context), scope="combination"))
            if not records[-1].passed:
                break
    unchanged = revision == snapshot_revision(workspace_snapshot(Path(workspace)))
    complete = unchanged and not cancellation.is_set() and (deadline is None or time.time() < deadline)
    passed = bool(records) and all(r.passed for r in records) and complete
    status = "not_configured" if not records and complete else "passed" if passed else "failed"
    return {"passed": passed, "status": status,
            "reason": "No deterministic combination commands are configured; independent final verification remains required."
                      if status == "not_configured" else "",
            "scope": "registered-combination", "candidate_revision": revision,
            "checks": [asdict(r) for r in records], "candidate_unchanged": unchanged,
            "candidate_wide": True,
            "counts_as_final_acceptance": False}


@measured("candidate_review_checks")
def observe_candidate_checks(*, workspace, candidate_revision, authorize, runtime, reducer, invocation, cancellation,
                             permissions=None, artifact_store=None, require_command_permissions=False,
                             baseline_snapshot=None, changed_paths=None):
    """Run registered commands as review evidence, without accepting a candidate.

    Commands execute through the configured verifier. A live candidate
    authorization is required before and after every process; model data cannot
    select commands, paths, coverage or turn these observations into acceptance.
    """
    from minicode.verifier_registry import VerificationContext, VerifierRegistry
    from minicode.workspace_provider import WorkspaceConflict

    # This revision identifies the preceding native source observation. Merely
    # reporting absent/invalid check configuration does not observe files and
    # must not masquerade as evidence that the candidate is unchanged.
    revision = candidate_revision
    scope = "candidate-review-check"
    if isinstance(runtime.get("_finalVerifierRegistry"), VerifierRegistry):
        return {"status": "not_observed_custom_registry", "candidate_revision": revision,
                "checks": [], "counts_as_final_acceptance": False}
    registry = VerifierRegistry()
    records = [replace(record, scope=scope)
               for record in register_configured_checks(registry, runtime, reducer.contract,
                    workspace=workspace, project_checks=True, require_command_permissions=require_command_permissions,
                    baseline_snapshot=baseline_snapshot, changed_paths=changed_paths)]
    observed = False
    deadlines = [cap.deadline for cap in (reducer.contract.budget, invocation.budget) if cap.deadline is not None]
    deadline = min(deadlines) if deadlines else None
    verification = VerificationContext(contract=reducer.contract, plan=reducer.current_plan,
                                       workspace=Path(workspace), final=False, cancellation_event=cancellation,
                                       deadline=deadline, permissions=permissions, artifact_store=artifact_store)
    if not records:
        for verifier in registry.ordered(allow_model=False):
            if authorize() != revision:
                raise WorkspaceConflict("candidate changed before review check")
            remaining = deadline - time.time() if deadline is not None else None
            if cancellation.is_set() or (remaining is not None and remaining <= 0):
                records.append(VerificationRecord(verifier="candidate-review-budget", passed=False,
                    scope=scope, details="Canceled or deadline exhausted before registered check", deterministic=True))
                break
            if remaining is not None:
                verifier = replace(verifier, timeout=min(verifier.timeout, remaining))
            records.append(replace(verifier.verify(verification), scope=scope))
            if authorize() != revision:
                raise WorkspaceConflict("candidate changed during review check")
            observed = True
            if not records[-1].passed:
                break
    covered = {criterion for record in records if record.passed for criterion in record.covered_criteria_ids}
    required = set(acceptance_criterion_ids(reducer.contract.acceptance_criteria))
    all_covered = bool(required) and required <= covered
    complete = not cancellation.is_set() and (deadline is None or time.time() < deadline)
    from minicode.verification_scope import observed_changes, select_project_scope
    decision = select_project_scope(workspace, reducer.contract,
        changed_paths=observed_changes(workspace, baseline_snapshot) if baseline_snapshot is not None else changed_paths)
    not_required = not records and not runtime.get("runtimeVerificationCommands") and not decision.build and not decision.unit and complete
    status = ("not_required" if not_required else "not_configured" if not records else "passed" if complete and all(r.passed for r in records) else "failed")
    return {"status": status, "candidate_revision": revision,
            **({"candidate_unchanged": True} if observed else {}),
            "all_criteria_covered": all_covered,
            "coverage_status": "not_declared" if not required else "complete" if all_covered else "incomplete",
            "checks": [asdict(record) for record in records], "counts_as_final_acceptance": False}
