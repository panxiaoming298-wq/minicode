"""Crash-readable receipts for human messages accepted by the Web host."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import re

from minicode.config import runtime_sessions_directory
from minicode.session_contract import atomic_write_json


def ledger_path(session_id: str, run_id: str) -> Path:
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", key) for key in (session_id, run_id)):
        raise ValueError("invalid run input storage identity")
    return runtime_sessions_directory() / "run-inputs" / session_id / (run_id + ".json")


def persist_inputs(record, *, inputs=None, accepting_steering=None) -> None:
    """Caller owns input_lock. Persist before acknowledging acceptance."""
    atomic_write_json(ledger_path(record.session_id, record.run_id), {
        "schema": "minicode-web-run-inputs-v1", "runId": record.run_id,
        "sessionId": record.session_id,
        "workspace": record.workspace, "promptPreview": record.prompt_preview,
        "executionProfile": record.execution_profile, "permissionMode": record.permission_mode,
        "createdAt": record.created_at, "updatedAt": record.updated_at, "status": record.status,
        "acceptingSteering": record.accepting_steering if accepting_steering is None else accepting_steering,
        "queueProtocol": record.queue_protocol,
        "queueResumedRunId": record.queue_resumed_run_id,
        "prompt": record.prompt,
        "eventHistoryError": record.event_history_error,
        "eventSequence": record.sequence,
        "taskFile": record.task_file, "runBudget": record.run_budget,
        "contextSettings": record.context_settings, "executionLocation": record.execution_location,
        "requiresWorkspaceWrite": record.requires_workspace_write,
        "inputs": deepcopy(record.inputs if inputs is None else inputs),
    })


def restored_status(saved: dict, session) -> str:
    """Recover old nonterminal receipts only from the matching saved outcome."""
    status = saved["status"]
    if status in {"completed", "failed", "stopped"}:
        return status
    if session is None or int(session.metadata.updated_at * 1000) < saved["updatedAt"]:
        return "stopped"
    if session.metadata.status in {"failed", "stopped"}:
        return session.metadata.status
    if session.metadata.status != "completed" or any(
        item["status"] in {"accepted", "starting", "applied"} for item in saved["inputs"].values()
    ):
        return "stopped"
    for trace in reversed(session.runtime_runs):
        if not isinstance(trace, dict):
            continue
        delivery = trace.get("delivery", {})
        if not isinstance(delivery, dict):
            continue
        run_id = trace.get("web_run_id") or delivery.get("run_id")
        if run_id != saved["runId"]:
            continue
        scheduler = trace.get("scheduler", {})
        if isinstance(scheduler, dict) and not scheduler.get("canceled") and delivery.get("completed") is True:
            return "completed"
        return "stopped"
    return "stopped"


def find_inputs(run_id: str) -> dict | None:
    """Resolve an exact run after a host restart, without following transfers."""
    root = ledger_path("lookup", run_id).parent.parent
    matches = list(root.glob(f"*/{run_id}.json"))
    if len(matches) > 1:
        raise ValueError("ambiguous run input identity")
    return read_inputs(matches[0].parent.name, run_id) if matches else None


def read_inputs(session_id: str, run_id: str) -> dict | None:
    path = ledger_path(session_id, run_id)
    if not path.exists():
        return None
    return _read_inputs(path, session_id, follow_transfer=False)


def latest_inputs(session_id: str) -> dict | None:
    directory = ledger_path(session_id, "lookup").parent
    if not directory.is_dir():
        return None
    paths = [p for p in directory.glob("*.json") if p.is_file() and not p.is_symlink()]
    if not paths:
        return None
    path = max(paths, key=lambda p: p.stat().st_mtime_ns)
    return _read_inputs(path, session_id, follow_transfer=True)


def _read_inputs(path: Path, session_id: str, *, follow_transfer: bool) -> dict:
    from minicode.path_safety import is_unsafe_stored_path
    if is_unsafe_stored_path(path, boundary=runtime_sessions_directory(), reject_hardlinks=True):
        raise ValueError("unsafe run input receipt")
    if path.stat().st_size > 256_000_000:
        raise ValueError("run input receipt exceeds its bounded storage size")
    value = json.loads(path.read_text(encoding="utf-8"))
    visited = {path}
    while follow_transfer and isinstance(value, dict) and value.get("queueResumedRunId"):
        # A transfer link may be the last write after a crash. Follow ownership
        # to the new run instead of presenting its old held messages again.
        path = ledger_path(session_id, value["queueResumedRunId"])
        if (path in visited or len(visited) >= 128
            or is_unsafe_stored_path(path, boundary=runtime_sessions_directory(), reject_hardlinks=True)
            or not path.is_file() or path.stat().st_size > 256_000_000):
            raise ValueError("invalid transferred run input receipt")
        visited.add(path)
        value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("schema") != "minicode-web-run-inputs-v1"
        or value.get("sessionId") != session_id or not isinstance(value.get("inputs"), dict)
        or len(value["inputs"]) > 128 or ledger_path(session_id, value.get("runId", "")) != path
        or value.get("executionProfile") not in {"standard", "deep", "ultra"}
        or any(not isinstance(value.get(key), str) for key in ("workspace", "promptPreview"))
        or any(type(value.get(key)) is not int for key in ("createdAt", "updatedAt"))
        or value.get("permissionMode") not in {"default", "bypass"}
        or value.get("status") not in {"queued", "running", "stopping", "stopped", "completed", "failed"}):
        raise ValueError("invalid persisted run input receipt")
    if (not isinstance(value.get("prompt", ""), str) or len(value.get("prompt", "")) > 2_000_000
        or any(value.get(key) is not None and not isinstance(value[key], dict)
               for key in ("taskFile", "runBudget", "contextSettings"))
        or value.get("executionLocation") not in {None, "project", "isolated"}
        or type(value.get("requiresWorkspaceWrite", True)) is not bool
        or type(value.get("eventSequence", 0)) is not int or value.get("eventSequence", 0) < 0
        or value.get("eventHistoryError") is not None and not isinstance(value["eventHistoryError"], str)):
        raise ValueError("invalid persisted run settings")
    for key, item in value["inputs"].items():
        if (not isinstance(item, dict) or item.get("inputId") != key
            or item.get("kind") not in {"steer", "supplement", "next_task"}
            or not isinstance(item.get("text"), str) or len(item["text"]) > 200_000
            or item.get("status") not in {"accepted", "starting", "applied", "completed", "failed", "held", "canceled"}):
            raise ValueError("invalid persisted human input")
    return value
