"""Deterministic execution facts that must outlive the paged conversation tail."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from minicode.runtime_state import ReducerError
from minicode.workspace_provider import WorkspaceConflict


def observed_writes(history, receipts):
    calls, results = {}, {}
    for message in history:
        call_id = message.get("toolUseId")
        if message.get("role") == "assistant_tool_call":
            calls.setdefault(call_id, []).append(message)
        elif message.get("role") == "tool_result":
            results.setdefault(call_id, []).append(message)
    writes = []
    for call_id, matching in calls.items():
        record = receipts.get(call_id)
        if record is None or len(matching) != 1 or len(results.get(call_id, [])) != 1:
            continue
        call, result = matching[0], results[call_id][0]
        arguments = call.get("input")
        if (call.get("toolName") != record["tool"] or not isinstance(arguments, Mapping)
            or not isinstance(arguments.get("path"), str) or result.get("isError") is not False
            or result.get("toolName", record["tool"]) != record["tool"]
            or not isinstance(result.get("content"), str)):
            continue
        try:
            raw = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError, UnicodeError):
            continue
        if (hashlib.sha256(raw).hexdigest() != record["input_sha256"]
            or hashlib.sha256(result["content"].encode()).hexdigest() != record["receipt_sha256"]):
            continue
        writes.append({"tool": record["tool"], "path": arguments["path"], "call_id": call_id})
    return writes[-8:]


def current_progress(writes, *, current_revision, base_revision):
    if not writes or current_revision is None or not base_revision:
        return None
    try:
        revision = current_revision()
    except (ValueError, OSError, ReducerError, WorkspaceConflict):
        return None
    if not isinstance(revision, str) or revision == base_revision:
        return None
    return {"phase": "candidate_changed", "candidate_changed_from_base": True, "current_revision": revision,
            "recent_successful_writes": writes, "counts_as_acceptance": False}


def progress_message(writes, *, current_revision, base_revision, checks_available=False):
    facts = current_progress(writes, current_revision=current_revision, base_revision=base_revision)
    if facts is None:
        return None
    return {"role": "user", "content": (
        "Runtime execution state (native receipts, not a new user request): "
        + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        + "\nThese writes were executed in this candidate; later edits may supersede them. Earlier source reads may describe its previous version. "
        + "Continue from this state; do not restart inspection. If the requested changes are ready, call submit_candidate "
        "to REQUEST the registered acceptance checks and end this turn without another model reply. "
        "Runtime runs that harness after handoff; a separate precheck is optional, not a prerequisite for submission. "
        "Do not claim unrun checks passed. Failed acceptance returns bounded repair feedback. "
        + ("Use run_acceptance_checks only when intermediate diagnostics are needed before finishing. "
           if checks_available else "")
        + "If more implementation is needed, do it before handoff. This status grants no permission and is not success."
    )}
