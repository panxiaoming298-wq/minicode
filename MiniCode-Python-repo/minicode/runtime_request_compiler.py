"""Select a complete, conservatively quoted Runtime request before admission.

Every candidate uses the same provider parameters, tool surface, native proof
rules and closure advisory. Compilation is not a reservation or a new source of
execution authority. Only the selected view's paging state is published.
"""
from dataclasses import dataclass
from typing import Any

from minicode.runtime_state import ReducerError
from minicode.runtime_artifacts import ArtifactIntegrityError
from minicode.workspace_provider import WorkspaceConflict
from minicode.usage_ledger import BudgetUnavailable


_STATE = ("_contents", "_pinned_delivery", "_pinned_source_receipts", "_candidate_stage_active",
    "projected_tool_inputs", "projected_consultation_inputs", "projected_consultation_receipts",
    "projected_consultation_previews", "projected_candidate_metadata_pages", "projected_delivery_audit_pages")


def _snapshot(context):
    return {**{key: dict(value) if isinstance(value := getattr(context, key), dict) else value for key in _STATE},
        "_legacy_stage_pins": dict(context._stage.pinned_source_receipts) if context._stage is not None else None}


def _restore(context, state):
    for key, value in state.items():
        if key == "_legacy_stage_pins":
            if context._stage is not None:
                context._stage.pinned_source_receipts = dict(value)
        else:
            setattr(context, key, dict(value) if isinstance(value, dict) else value)


@dataclass
class CompiledRequest:
    body: dict[str, Any]
    stage: dict[str, Any] | None
    closure: dict[str, Any] | None
    projected_results: int
    projected_deliveries: int
    before_advice_input: int
    diagnostic: dict[str, Any] | None


def compile_primary_request(context, body, *, endpoint, quote, desired_output, finish, measure):
    """Compare full final bodies, not an intermediate stage's bytes-saved claim.

    Full native reports stay readable in every alternative. Normally their full
    text stays inline. A primary's explicit, natively observed working context
    may instead select model-authored notes; that is disclosed as an assessment,
    not a lossless summary or verified semantic completeness. Unowned previews
    cannot compete as if they were complete decisions.
    Quotes may change while compiling; the caller MUST atomically reserve again.
    """
    initial = _snapshot(context)
    choices = []
    comparison_allowed = True
    try:
        working_memory = context.has_primary_working_context()
        names = (["task_working_memory"] if working_memory else []) + ["working_set", "packed_working_set", "retained_history", "original_history"]
        for name in names:
            if choices and not comparison_allowed:
                break
            _restore(context, initial)
            view, stage = context.prepare_stage_view(body, endpoint=endpoint,
                primary=name != "original_history", retain_batches=name == "retained_history",
                pack_runtime=name in {"packed_working_set", "task_working_memory"}, use_working_memory=name == "task_working_memory")
            if name != "original_history" and stage is None:
                continue
            if not choices and stage is not None:
                comparison_allowed = bool(stage.get("primary_working_context")) or any(
                    stage.get(key, {}).get("full_decisions_preserved") is True for key in (
                        "execution_working_set", "pending_candidate_working_set", "candidate_working_set"))
            view, results, deliveries = context.project(view, quote=quote, desired_output=desired_output,
                preserve_decision_pages=working_memory or (name == "original_history" and bool(choices)))
            before_advice = measure(view)
            view, closure = finish(view, working_context=context.primary_working_context_advice(stage))
            estimate = measure(view)
            try:
                grant = quote(view)["output_reservation"]
            except BudgetUnavailable:
                grant = None
            choices.append({"name": name, "body": view, "stage": stage, "closure": closure,
                "results": results, "deliveries": deliveries, "before_advice": before_advice,
                "state": _snapshot(context), "estimate": estimate, "grant": grant})
        # All alternatives preserve hard constraints and readable full reports;
        # only explicit current primary notes can replace inline report text.
        # Prefer the largest output grant, then the smallest final
        # conservative input. Stable ties preserve the established working set.
        selected = min(choices, key=lambda c: (-(c["grant"] if c["grant"] is not None else -1), c["estimate"]))
        invalidated = False
        if selected["stage"] is not None and len(choices) > 1:
            try:
                invalidated = context._primary_phase._current_revision() != selected["stage"]["workspace_revision"]
                assessment = selected["stage"].get("primary_working_context")
                if assessment:
                    invalidated = invalidated or not context._primary_phase._working_context.is_current(assessment["sha256"])
                advice = (selected["closure"] or {}).get("working_context")
                if advice:
                    invalidated = invalidated or context.primary_working_context_advice(selected["stage"]) != advice
            except (ValueError, TypeError, OSError, ArtifactIntegrityError, WorkspaceConflict, ReducerError):
                invalidated = True
            if invalidated:
                selected = next(c for c in choices if c["name"] == "original_history")
        _restore(context, selected["state"])
        diagnostic = None if len(choices) == 1 else {
            "policy": "final-request-context-selection-v1", "selected": selected["name"],
            "selection_basis": "largest_quoted_output_then_smallest_final_input",
            "desired_output_reservation": desired_output,
            "candidates": [{"view": c["name"], "final_input_reservation": c["estimate"],
                "before_advice_input_reservation": c["before_advice"], "quoted_output_reservation": c["grant"],
                "full_output_quoted": c["grant"] is not None and c["grant"] >= desired_output,
                "closure_advice_included": bool(c["closure"] and c["closure"]["message_included"])} for c in choices],
            "live_revision_invalidated": invalidated,
            "full_decisions_preserved": not bool(selected["stage"] and selected["stage"].get("primary_working_context")),
            "full_decisions_available": True,
            "primary_assessment_used": bool(selected["stage"] and selected["stage"].get("primary_working_context")),
            "quotes_are_reservations": False, "atomic_admission_required": True,
            "counts_as_acceptance": False, "canonical_history_changed": False}
        return CompiledRequest(selected["body"], selected["stage"], selected["closure"],
            selected["results"], selected["deliveries"], selected["before_advice"], diagnostic)
    except BaseException:
        _restore(context, initial)
        raise
