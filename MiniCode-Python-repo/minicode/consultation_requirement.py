"""Evaluate the explicit consultation process against authoritative history.

This proves bounded successful read-only results were delivered, not that their
natural-language recommendations are correct. Task-specific evidence/acceptance
checks remain separate. No cached success flag or Worker prose grants a waiver.
"""
from __future__ import annotations

from collections.abc import Mapping

from minicode.runtime_protocol import AgentResultStatus, NodeState, RuntimeEventType, ToolEffect, VerificationRecord


def _passed(record):
    return record.passed if isinstance(record, VerificationRecord) else isinstance(record, Mapping) and record.get("passed") is True


def consultation_requirement_status(contract, nodes, events):
    requirement = contract.required_consultation if contract else None
    if requirement is None:
        return None
    run_id = contract.envelope.run_id
    primary = nodes.get("primary")
    batches, receipt_ids = [], []
    history = [event for event in events if event.run_id == run_id]
    for delivery in history:
        if delivery.event_type is not RuntimeEventType.CONSULTATION_DELIVERED:
            continue
        payload = delivery.payload
        suspension = next((event for event in history if event.event_type is RuntimeEventType.PRIMARY_SUSPENDED
            and event.sequence < delivery.sequence and event.payload.get("node_id") == "primary"
            and event.payload.get("continuation_id") == payload.get("continuation_id")), None)
        children = payload.get("children", {})
        if (payload.get("node_id") != "primary" or primary is None or suspension is None
            or not isinstance(children, dict) or set(children) != set(suspension.payload.get("children", []))
            or not any(attempt.attempt_id == payload.get("attempt_id") and attempt.run_id == run_id
                       and attempt.node_id == "primary" for attempt in primary.attempts)
            or not any(event.event_type is RuntimeEventType.PRIMARY_RESUMABLE
                       and suspension.sequence < event.sequence < delivery.sequence
                       and event.payload.get("continuation_id") == payload.get("continuation_id") for event in history)
            or not any(event.event_type is RuntimeEventType.ATTEMPT_STARTED
                       and suspension.sequence < event.sequence < delivery.sequence
                       and event.payload.get("node_id") == "primary"
                       and event.payload.get("attempt_id") == payload.get("attempt_id") for event in history)):
            continue
        qualified = set()
        for node_id, attempt_id in children.items():
            node = nodes.get(node_id)
            result = node.result if node else None
            accepted = [event.sequence for event in history if event.event_type is RuntimeEventType.RESULT_ACCEPTED
                        and suspension.sequence < event.sequence < delivery.sequence
                        and event.payload.get("node_id") == node_id and event.payload.get("attempt_id") == attempt_id
                        and event.payload.get("status") == "succeeded"]
            if (node is None or not node_id.startswith("consult-") or node.state is not NodeState.SUCCEEDED
                or node.definition.criticality != "required" or node.definition.dependencies
                or node.definition.allowed_effects != (ToolEffect.READ_ONLY,)
                or node.definition.role not in {"explorer", "planner", "reviewer", "verifier"}
                or node.definition.to_dict() not in suspension.payload.get("definitions", [])
                or result is None or result.status is not AgentResultStatus.SUCCEEDED
                or result.node_id != node_id or result.run_id != run_id or result.changed_resources
                or result.contract_revision != contract.contract_revision
                or result.attempt_id != attempt_id or node.current_attempt_id != attempt_id
                or not any(attempt.attempt_id == attempt_id and attempt.run_id == run_id
                           and attempt.node_id == node_id and attempt.state is NodeState.SUCCEEDED for attempt in node.attempts)
                or not accepted
                or not any(event.event_type is RuntimeEventType.VERIFICATION_ACCEPTED
                           and max(accepted) < event.sequence < delivery.sequence
                           and event.payload.get("node_id") == node_id
                           and _passed(event.payload.get("record")) for event in history)):
                continue
            qualified.add(node_id)
        batches.append(qualified)
        receipt_ids.append(delivery.event_id)
    qualified = max(batches, key=len, default=set()) if requirement.single_batch else set().union(*batches)
    satisfied = len(qualified) >= requirement.min_helpers
    declined = not satisfied and any(event.event_type is RuntimeEventType.CONSULTATION_BUDGET_DECLINED
        and event.payload.get("node_id") == "primary" and event.payload.get("scope") == "before_dispatch" for event in history)
    reason = ("" if satisfied else f"Required consultation unmet: {len(qualified)}/{requirement.min_helpers} "
              "successful required read-only helpers delivered" + (" in one batch" if requirement.single_batch else "")
              + ("; consultation budget was declined; no further primary requests or tools are allowed" if declined else ""))
    return {"satisfied": satisfied, "blocked": declined, "reason": reason,
            "min_helpers": requirement.min_helpers, "qualified_nodes": sorted(qualified),
            "delivery_event_ids": receipt_ids, "before_workspace_write": requirement.before_workspace_write,
            "single_batch": requirement.single_batch}
