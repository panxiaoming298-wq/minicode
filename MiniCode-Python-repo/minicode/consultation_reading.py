"""Explicit paged decisions: verified delivery plus current-activation reads.

Reading means native output reached the next model-input observation, not that
the model understood it. No prose, tool name alone, artifact existence, or old
attempt's page receipts satisfy this process; task acceptance stays separate.
"""
from __future__ import annotations

import hashlib
import json

from minicode.consultation_submission import PAGED_SUMMARY_BYTES_LIMIT, SUMMARY_BYTES_LIMIT, SUMMARY_MEDIA_TYPE
from minicode.runtime_protocol import AgentResultStatus, NodeState, RuntimeEventType, ToolEffect


def _verified_delivery(delivery, result, node, primary, events):
    if delivery is None or primary is None:
        return False
    from minicode.consultation_requirement import _passed

    data = delivery.payload
    suspended = next((e for e in events if e.run_id == result.run_id
        and e.event_type is RuntimeEventType.PRIMARY_SUSPENDED and e.sequence < delivery.sequence
        and e.payload.get("node_id") == "primary"
        and e.payload.get("continuation_id") == data.get("continuation_id")), None)
    if (suspended is None or result.node_id not in suspended.payload.get("children", [])
        or node.definition.to_dict() not in suspended.payload.get("definitions", [])
        or not any(a.attempt_id == data.get("attempt_id") and a.node_id == "primary" and a.run_id == result.run_id
                   for a in primary.attempts)):
        return False
    accepted = [e.sequence for e in events if e.run_id == result.run_id
        and e.event_type is RuntimeEventType.RESULT_ACCEPTED and suspended.sequence < e.sequence < delivery.sequence
        and e.payload.get("node_id") == result.node_id and e.payload.get("attempt_id") == result.attempt_id
        and e.payload.get("consultation_summary_ref") == result.consultation_summary_ref.to_dict()
        and e.payload.get("status") == result.status.value]
    verdict = (result.status is AgentResultStatus.FAILED_TERMINAL and result.failure is not None) or (
        accepted and any(e.run_id == result.run_id and max(accepted) < e.sequence < delivery.sequence
        and e.payload.get("node_id") == result.node_id
        and ((e.event_type is RuntimeEventType.VERIFICATION_ACCEPTED and _passed(e.payload.get("record")))
             or (e.event_type is RuntimeEventType.VERIFICATION_FAILED
                 and e.payload.get("attempt_id") == result.attempt_id)) for e in events))
    return bool(accepted and verdict
        and any(e.run_id == result.run_id and e.event_type is RuntimeEventType.PRIMARY_RESUMABLE
            and suspended.sequence < e.sequence < delivery.sequence
            and e.payload.get("continuation_id") == data.get("continuation_id") for e in events)
        and any(e.run_id == result.run_id and e.event_type is RuntimeEventType.ATTEMPT_STARTED
            and suspended.sequence < e.sequence < delivery.sequence and e.payload.get("node_id") == "primary"
            and e.payload.get("attempt_id") == data.get("attempt_id") for e in events))


def valid_summary_ref(result, node) -> bool:
    ref = result.consultation_summary_ref
    return (ref is not None and result.node_id.startswith("consult-")
        and node.role in {"explorer", "planner", "reviewer", "verifier"}
        and node.allowed_effects == (ToolEffect.READ_ONLY,) and not result.changed_resources
        and result.status in {AgentResultStatus.SUCCEEDED, AgentResultStatus.FAILED_TERMINAL} and ref in result.artifact_refs
        and sum(a.artifact_id == ref.artifact_id for a in result.artifact_refs) == 1
        and ref.media_type == SUMMARY_MEDIA_TYPE and type(ref.size) is int
        and SUMMARY_BYTES_LIMIT < ref.size <= PAGED_SUMMARY_BYTES_LIMIT)


def reading_status(run_id, nodes, events, contract_revision):
    primary = nodes.get("primary")
    attempt_id = primary.current_attempt_id if primary else None
    started = max((e.sequence for e in events if e.run_id == run_id
        and e.event_type is RuntimeEventType.ATTEMPT_STARTED and e.payload.get("node_id") == "primary"
        and e.payload.get("attempt_id") == attempt_id), default=-1)
    obligations = []
    for event in events:
        data = event.payload
        if event.run_id != run_id or event.event_type is not RuntimeEventType.RESULT_ACCEPTED or data.get("consultation_summary_ref") is None:
            continue
        node = nodes.get(data.get("node_id"))
        if node is None or node.current_attempt_id != data.get("attempt_id"):
            continue
        actual = node.result.consultation_summary_ref if node.result is not None else None
        if actual is None or actual.to_dict() != data["consultation_summary_ref"]:
            obligations.append({"child_id": data.get("node_id"), "child_attempt_id": data.get("attempt_id"),
                "artifact_ref": data["consultation_summary_ref"], "valid": False, "delivery_event_id": None,
                "read_through": 0, "complete": False})
    for node_id, node in nodes.items():
        result = node.result
        if result is None or result.consultation_summary_ref is None:
            continue
        ref = result.consultation_summary_ref
        valid = (valid_summary_ref(result, node.definition) and node.state in {NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL}
            and result.run_id == run_id and result.attempt_id == node.current_attempt_id
            and result.contract_revision == contract_revision)
        delivery = None
        for event in reversed(events):
            if (event.run_id == run_id and event.event_type is RuntimeEventType.CONSULTATION_DELIVERED
                and event.payload.get("node_id") == "primary"
                and event.payload.get("children", {}).get(node_id) == result.attempt_id):
                delivery = event
                break
        verified = _verified_delivery(delivery, result, node, primary, events)
        ranges = []
        if valid and verified:
            for event in events:
                data = event.payload
                if (event.event_type is RuntimeEventType.CONSULTATION_SUMMARY_READ and event.run_id == run_id
                    and event.sequence > delivery.sequence and data.get("delivery_event_id") == delivery.event_id
                    and started >= 0 and event.sequence > started
                    and data.get("node_id") == "primary" and data.get("attempt_id") == attempt_id
                    and data.get("child_id") == node_id and data.get("child_attempt_id") == result.attempt_id
                    and data.get("artifact_ref") == ref.to_dict()
                    and data.get("contract_revision") == contract_revision
                    and isinstance(data.get("tool_call_id"), str) and data["tool_call_id"]
                    and isinstance(data.get("receipt_sha256"), str) and len(data["receipt_sha256"]) == 64
                    and all(c in "0123456789abcdef" for c in data["receipt_sha256"])
                    and type(data.get("start")) is int and type(data.get("end")) is int
                    and 0 <= data["start"] < data["end"] <= ref.size):
                    ranges.append((data["start"], data["end"]))
        cursor = 0
        for start, end in sorted(ranges):
            if start > cursor:
                break
            cursor = max(cursor, end)
        obligations.append({"child_id": node_id, "child_attempt_id": result.attempt_id,
            "artifact_ref": ref.to_dict(), "valid": bool(valid and verified),
            "delivery_event_id": delivery.event_id if delivery else None,
            "read_through": cursor, "complete": bool(valid and verified and cursor == ref.size)})
    pending = [item for item in obligations if not item["complete"]]
    return {"satisfied": not pending, "obligations": obligations,
        "reason": ("Unread consultation decision: use runtime_artifact_read for every byte page, then observe its "
                   "complete tool receipt in a new model round before writing, delegating, or submitting a result."
                   if pending else "")}


def _input(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class ConsultationReadTracker:
    """Activation-local native receipts; deliberately never checkpointed."""
    def __init__(self, *, authorize, commit, artifacts):
        self.authorize, self.commit, self.artifacts = authorize, commit, artifacts
        self.issued, self.settled, self.observed = {}, {}, set()

    def issued_page(self, context, ref, arguments, output):
        self.authorize(context, ref)
        call_id = context.tool_call_id
        if call_id in self.issued:
            self.issued[call_id] = None  # Reused native IDs cannot regain authority.
        else:
            self.issued[call_id] = (ref, _input(arguments), output)

    def record(self, call, result):
        call_id = call.get("id")
        self.settled.pop(call_id, None)
        issued = self.issued.get(call_id)
        if (issued is not None and call.get("toolName") == "runtime_artifact_read"
            and isinstance(call.get("input"), dict) and _input(call["input"]) == issued[1]
            and result.ok is True and result.output == issued[2]
            and not any((result.awaitUser, result.waitKind, result.timedOut, result.ambiguousSideEffect,
                result.runtimeYield, result.runtimeStopReason, result.inputRejected, result.backgroundTask,
                result.verificationFailed, result.failureKind))):
            # Native validator may clamp end/default start; bind the exact
            # original model arguments only after concrete result handling.
            self.settled[call_id] = (issued, _input(call["input"]))

    def observe(self, messages):
        calls, receipts = {}, {}
        duplicates = set()
        for index, message in enumerate(messages):
            role, call_id = message.get("role"), message.get("toolUseId")
            if role not in {"assistant_tool_call", "tool_result"}:
                continue
            target = calls if role == "assistant_tool_call" else receipts
            if not isinstance(call_id, str) or call_id in target:
                duplicates.add(call_id)
            target[call_id] = (index, message)
        # An incomplete tool exchange is not a new model observation.
        if set(calls) != set(receipts) or duplicates:
            return
        for call_id, (issued, arguments) in self.settled.items():
            if call_id in self.observed or call_id not in calls:
                continue
            ci, call = calls[call_id]
            ri, receipt = receipts[call_id]
            ref, _, output = issued
            if (ci >= ri or call.get("toolName") != "runtime_artifact_read"
                or receipt.get("toolName") != "runtime_artifact_read" or receipt.get("isError") is not False
                or receipt.get("content") != output or _input(call.get("input")) != arguments):
                continue
            data = json.loads(output)
            page = data["range"]
            raw = self.artifacts.get_bytes(ref)
            # Only complete, directly visible UTF-8 pages can fulfill reading.
            if (data.get("encoding") != "utf-8" or data["content"].encode("utf-8") != raw[page["start"]:page["end"]]):
                continue
            self.commit(ref, page["start"], page["end"], call_id,
                hashlib.sha256(output.encode("utf-8")).hexdigest())
            self.observed.add(call_id)
