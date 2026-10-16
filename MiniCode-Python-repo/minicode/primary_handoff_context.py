"""Exact, native-proved history selection at an execution or checked handoff.

This module selects data, never runs a tool or grants acceptance. The caller
owns provider semantics, live revision checks, native receipts and the archive.
"""
from collections.abc import Mapping
from copy import deepcopy
import json

POLICY = "native-checked-candidate-working-set-v1"
EXECUTION_POLICY = "native-consulted-execution-working-set-v1"
PENDING_POLICY = "native-unchecked-candidate-working-set-v1"
SOURCE_BYTES_LIMIT = 8192
READS = frozenset({"read_file", "runtime_artifact_read", "runtime_context_read"})


def select_handoff(messages, *, proven, check_id, revision, decisions, decision_texts,
                   completed, delivery_views, pending_candidate=None, working_context_calls=()):
    """Return exact retired batches plus a smaller candidate-oriented history.

    Only closed contiguous batches with exact native inputs/results may retire.
    All system/user instructions and assistant prose stay. Paged decisions move
    verbatim into their authenticated delivery, not into a generated summary.
    """
    pending = pending_candidate is not None
    execution = check_id is None and not pending
    if pending and (check_id is not None or pending_candidate["workspace_revision"] != revision
        or pending_candidate["check_status"] != "not_run_for_current_revision"
        or pending_candidate["counts_as_acceptance"] is not False):
        return None
    boundaries, replacements, promoted = {}, {}, {}
    expected = {d["artifact_ref"]["artifact_id"]: d for d in decisions}
    for identifier, record in completed.items():
        original, _, receipt, _, delivery = record
        locations = [i for i, m in enumerate(messages) if m.get("role") == "user" and m.get("content") == delivery]
        if len(locations) != 1 or delivery not in delivery_views:
            continue
        index = locations[0]
        compact = delivery_views[delivery]
        if not isinstance(compact, str) or "\n" not in compact:
            return None
        payload = json.loads(compact.split("\n", 1)[1])
        if not isinstance(payload, dict) or not isinstance(payload.get("children"), list):
            return None
        for child in payload["children"]:
            if not isinstance(child, dict) or not isinstance(child.get("result"), dict):
                return None
            result = child.get("result") or {}
            ref = result.get("consultation_summary_ref")
            if ref is not None and not isinstance(ref, dict):
                return None
            if (execution or pending) and (child.get("state") != "SUCCEEDED" or not isinstance(result.get("output_summary"), str)
                or not result["output_summary"].strip() or (ref is not None and ref.get("artifact_id") not in expected)):
                return None
            if ref is None or ref.get("artifact_id") not in expected:
                continue
            key = ref["artifact_id"]
            decision = expected[key]
            if (key in promoted or ref != decision["artifact_ref"] or child["node_id"] != decision["child_id"]
                or child["state"] != "SUCCEEDED" or key not in decision_texts):
                return None
            result["output_summary"] = decision_texts[key]
            promoted[key] = identifier
        boundaries[identifier] = (index, original, receipt)
        replacements[index] = compact.split("\n", 1)[0] + "\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if set(promoted) != set(expected):
        return None  # Never hide a required decision with no current full delivery.

    last_batch = max(i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("tool_calls"))
    retired, batches, consulted, reads, executions = set(), [], set(), set(), []
    sources = {}
    page_calls = {identifier for d in decisions for identifier in d["native_read_calls"]}
    for position, assistant in enumerate(messages):
        if assistant.get("role") != "assistant" or not assistant.get("tool_calls"):
            continue
        batch = assistant["tool_calls"]
        end = position + 1 + len(batch)
        receipts = messages[position + 1:end]
        if len(receipts) != len(batch):
            continue
        native = []
        for call, receipt in zip(batch, receipts):
            function = call.get("function")
            if (set(call) != {"id", "type", "function"} or call["type"] != "function"
                or not isinstance(function, Mapping) or set(function) != {"name", "arguments"}
                or set(receipt) != {"role", "tool_call_id", "content"}
                or receipt["role"] != "tool" or receipt["tool_call_id"] != call["id"]):
                break
            identifier = call["id"]
            record = proven.get(identifier)
            exchange = boundaries.get(identifier)
            if (exchange is not None and function["name"] == "consult_agents"
                and function["arguments"] == exchange[1] and receipt["content"] == exchange[2] and end <= exchange[0]):
                native.append((identifier, "consultation", function, receipt))
            elif (record and identifier in working_context_calls and record["tool"] == "record_working_context"
                  and record["kind"] == "read"):
                native.append((identifier, "working_context", function, receipt))
            elif record and record["tool"] in READS and record["kind"] == "read":
                native.append((identifier, "read", function, receipt))
            elif pending and record and record["kind"] == "write":
                native.append((identifier, "write", function, receipt))
            elif not execution and not pending and position == last_batch and record and record["kind"] in {"write", "check"}:
                native.append((identifier, record["kind"], function, receipt))
            else:
                break
        else:
            batches.append({"message_range": [position, end], "messages": deepcopy(messages[position:end])})
            retired.update(range(position, end))
            for identifier, kind, function, receipt in native:
                if kind == "consultation":
                    consulted.add(identifier)
                elif kind == "read":
                    reads.add(identifier)
                    if (execution or pending) and identifier not in page_calls:
                        # Keep every distinct source/range verbatim. Repeated
                        # reads of the same input at the unchanged base need
                        # only their latest observation; all copies archive.
                        key = identifier if pending else (function["name"], function["arguments"])
                        sources[key] = {"call_id": identifier, "tool": function["name"],
                            "input": json.loads(function["arguments"]), "receipt": receipt["content"]}
                elif kind != "working_context":
                    arguments = json.loads(function["arguments"])
                    # Exact successful write/check receipts, not a model summary.
                    executions.append({"call_id": identifier, "tool": function["name"], "kind": kind,
                        "receipt": receipt["content"],
                        "input": arguments if kind == "check" else {k: v for k, v in arguments.items()
                            if k not in {"content", "old_string", "new_string"}}})
    check = next((e for e in executions if e["call_id"] == check_id and e["kind"] == "check"), None)
    if (not batches or (check is None and not execution and not pending) or not set(promoted.values()) <= consulted
        or not page_calls <= reads or not any(b["message_range"][0] == last_batch for b in batches)):
        return None
    source_observations = list(sources.values())
    if ((execution and not consulted) or ((execution or pending)
        and len(json.dumps(source_observations, ensure_ascii=False).encode("utf-8")) > SOURCE_BYTES_LIMIT)):
        return None  # No arbitrary source truncation to meet a working-set cap.
    # Nonpromoted deliveries can use the existing audit view later. Do not
    # replace a delivery whose consultation batch was not proved and retired.
    eligible_deliveries = {boundaries[identifier][0] for identifier in consulted}
    selected = []
    delivery_positions = []
    history_messages = []
    for position, message in enumerate(messages):
        history_messages.append({**deepcopy(message), "content": replacements[position]}
            if position in eligible_deliveries else deepcopy(message))
        if position in retired:
            if message.get("role") == "assistant" and message.get("content"):
                selected.append({"role": "assistant", "content": message["content"]})
        elif position in eligible_deliveries:
            delivery_positions.append(len(selected))
            selected.append({**message, "content": replacements[position]})
        else:
            selected.append(deepcopy(message))
    return {"messages": selected, "history_messages": history_messages, "delivery_positions": delivery_positions,
        "batches": batches, "candidate_check": None if execution or pending else {"call_id": check_id, "revision": revision},
        "execution_receipts": executions, "promoted_decisions": decisions,
        **({"source_observations": source_observations} if execution or pending else {}),
        **({"pending_candidate": pending_candidate} if pending else {}),
        "diagnostic": {"policy": PENDING_POLICY if pending else EXECUTION_POLICY if execution else POLICY, "check_call_id": check_id, "archived_batches": len(batches),
            **({"source_observation_count": len(source_observations), "source_bytes_limit": SOURCE_BYTES_LIMIT} if execution or pending else {}),
            **({"check_status": "not_run_for_current_revision", "candidate_file_hashes": {
                f["path"]: f["sha256"] for f in pending_candidate["files"]}} if pending else {}),
            "archived_messages": len(retired), "promoted_summary_refs": [d["artifact_ref"] for d in decisions],
            "full_decisions_preserved": True, "system_user_constraints_preserved": True,
            "checks_are_final_acceptance": False}}
