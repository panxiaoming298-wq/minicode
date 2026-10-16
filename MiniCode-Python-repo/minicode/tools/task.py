"""Governed ``task`` tool.

This module is intentionally unable to import or start AgentLoop.  It emits a
DelegationRequest to the active Runtime; only TaskScheduler may create the
registered child Node/Attempt.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TypedDict

from minicode.runtime_protocol import (
    AgentResultStatus,
    BudgetEnvelope,
    DelegationRequest,
    ExecutionFailureKind,
    ReplayPolicy,
    ToolEffect,
    consultation_context_files,
)
from minicode.tooling import ToolDefinition, ToolInputRejected, ToolMetadata, ToolResult
from minicode.workspace_provider import snapshot_revision, workspace_snapshot


class AgentDef(TypedDict):
    name: str
    description: str
    allowed_effects: tuple[ToolEffect, ...]


AGENT_TYPES: dict[str, AgentDef] = {
    "explore": {
        "name": "Explore",
        "description": "Fast, read-only codebase exploration",
        "allowed_effects": (ToolEffect.READ_ONLY,),
    },
    "plan": {
        "name": "Plan",
        "description": "Thorough, read-only analysis",
        "allowed_effects": (ToolEffect.READ_ONLY,),
    },
    "review": {
        "name": "Review",
        "description": "Read-only review of a specified plan or result",
        "allowed_effects": (ToolEffect.READ_ONLY,),
    },
    "verify": {
        "name": "Verify",
        "description": "Read-only independent verification of a specified result",
        "allowed_effects": (ToolEffect.READ_ONLY,),
    },
    "general": {
        "name": "General",
        "description": "Serial compatibility Worker with workspace tools",
        "allowed_effects": (
            ToolEffect.READ_ONLY,
            ToolEffect.WORKSPACE_WRITE,
            ToolEffect.PRIVILEGED,
        ),
    },
}


def _validate(input_data: dict) -> dict:
    description = input_data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    agent_type = input_data.get("agent_type", "general")
    if agent_type not in AGENT_TYPES:
        valid = ", ".join(AGENT_TYPES)
        raise ValueError(f"agent_type must be one of: {valid}. Got: {agent_type}")
    prompt = input_data.get("prompt", description)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    return {
        "description": description.strip(),
        "agent_type": agent_type,
        "prompt": prompt.strip(),
    }


def _validate_agent_dispatch(value):
    from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp
    if not isinstance(value, dict) or set(value) - {"tasks", "mode", "auto_after_seconds"}:
        raise ValueError("unknown async dispatch fields")
    mode = value.get("mode", "background")
    if mode not in {"background", "foreground", "auto"}:
        raise ValueError("mode must be background, foreground or auto")
    threshold = value.get("auto_after_seconds", 120)
    if type(threshold) not in {int, float} or not 0 < threshold <= 3600:
        raise ValueError("auto_after_seconds must be 0..3600 seconds")
    if "auto_after_seconds" in value and mode != "auto":
        raise ValueError("auto_after_seconds only applies to auto mode")
    from dataclasses import asdict
    return {"tasks": [asdict(task) for task in createApp().validate_agent_dispatch({"tasks": value.get("tasks")})],
        "mode": mode, "auto_after_seconds": threshold}


def _agent_gateway(context):
    from minicode.delegation import SchedulerDelegationGateway
    runtime_context = getattr(context, "runtime_context", None)
    gateway = getattr(runtime_context, "delegation_gateway", runtime_context)
    if not isinstance(gateway, SchedulerDelegationGateway):
        raise ValueError("native async Agent Runtime is unavailable")
    return gateway


def _spawn_agents_run(value, context):
    from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp
    gateway = _agent_gateway(context)
    result = gateway.spawn_agents(parent_id=context.node_id,
        attempt_id=context.attempt_id, tasks=createApp().validate_agent_dispatch({"tasks": value["tasks"]}),
        cancellation_event=context.cancellation_event)
    result["mode"] = value["mode"]
    if result["refused"]:
        result["notice"] = ("Only registered task IDs exist; their receipts remain valid. "
            "Inspect the refused preparation errors before retrying. Reuse the same assignment keys to avoid duplicates; "
            "when a multi-task preparation exceeds its time limit, dispatch fewer tasks per call.")
    waiting = False
    if not result["refused"] and value["mode"] != "background":
        ids = [record["task_id"] for record in result["registered"]]
        identity = json.dumps([context.attempt_id, ids, value["mode"], value["auto_after_seconds"]])
        result["wait"] = gateway.wait_agents(parent_id=context.node_id, attempt_id=context.attempt_id,
            task_ids=ids, wait_id="agent-wait-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
            timeout_seconds=value["auto_after_seconds"] if value["mode"] == "auto" else None)
        waiting = result["wait"].get("decision", {}).get("status") == "waiting"
    return ToolResult(ok=not result["refused"], output=json.dumps(result, ensure_ascii=False),
        runtimeYield=waiting, runtimeControlSettled=True)


def is_settled_native_dispatch(tool, result) -> bool:
    """Only an exited native handler's receipt can settle its timed-out call."""
    producers = {"spawn_agents": _spawn_agents_run, "resume_agent": _resume_agent_run, "reassign_agent": _reassign_agent_run}
    return (tool is not None and tool.run is producers.get(tool.name)
        and result is not None and result.runtimeControlSettled
        and not result.timedOut and not result.ambiguousSideEffect)


def native_preparation_timeout(tool, value) -> float | None:
    """Budget bounded native preparations, never a same-name extension.

    The Scheduler still enforces each individual preparation's deadline. The
    outer batch clock also leaves time to persist receipts and drain I/O.
    Explicit operator caps are applied by the executor after this default.
    """
    from minicode.workspace_provider import ASYNC_PREPARATION_SECONDS
    if tool is None:
        return None
    if tool.name == "spawn_agents" and tool.run is _spawn_agents_run:
        try:
            count = len(_validate_agent_dispatch(value)["tasks"])
        except (ValueError, TypeError, KeyError):
            return None
        return ASYNC_PREPARATION_SECONDS * count + 20
    if tool.run is {"resume_agent": _resume_agent_run, "reassign_agent": _reassign_agent_run}.get(tool.name):
        return ASYNC_PREPARATION_SECONDS + 20
    return None


def _agent_poll_input(value):
    if not isinstance(value, dict) or set(value) - {"task_ids", "cursor"}:
        raise ValueError("expected task_ids and optional cursor")
    tasks = value.get("task_ids")
    if tasks is not None and (not isinstance(tasks, list) or len(tasks) > 128 or not all(isinstance(t, str) and t for t in tasks)):
        raise ValueError("task_ids must contain registered agent ids")
    cursor = value.get("cursor", 0)
    if type(cursor) is not int or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    return {"task_ids": tasks, "cursor": cursor}


def _poll_agents_run(value, context):
    from minicode.delegation import AgentTargetInputError
    try:
        result = _agent_gateway(context).poll_agents(parent_id=context.node_id,
            attempt_id=context.attempt_id, **value)
    except AgentTargetInputError as error:
        return ToolResult(ok=False, output=f"Input validation error in poll_agents: {error}",
            failureKind="invalid_input", inputValidationFailed=True, inputRejected=True)
    return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False))


def _agent_wait_input(value):
    if not isinstance(value, dict) or set(value) - {"task_ids", "mode", "wait_id", "timeout_seconds"}:
        raise ValueError("unknown wait fields")
    tasks = _agent_poll_input({"task_ids": value.get("task_ids")})["task_ids"]
    if not tasks or len(set(tasks)) != len(tasks):
        raise ValueError("wait requires distinct agent task_ids")
    if value.get("mode", "all") not in {"all", "any"}:
        raise ValueError("mode must be all or any")
    wait_id = value.get("wait_id")
    if not isinstance(wait_id, str) or not 1 <= len(wait_id) <= 128:
        raise ValueError("wait_id must be a stable 1..128 character operation id")
    timeout = value.get("timeout_seconds")
    if timeout is not None and (type(timeout) not in {float, int} or not 0 < timeout <= 3600):
        raise ValueError("timeout_seconds must be 0..3600 seconds")
    return {"task_ids": tasks, "mode": value.get("mode", "all"), "wait_id": wait_id, "timeout_seconds": timeout}


def _wait_agents_run(value, context):
    from minicode.delegation import AgentTargetInputError
    try:
        result = _agent_gateway(context).wait_agents(parent_id=context.node_id,
            attempt_id=context.attempt_id, **value)
    except AgentTargetInputError as error:
        return ToolResult(ok=False, output=f"Input validation error in wait_agents: {error}",
            failureKind="invalid_input", inputValidationFailed=True, inputRejected=True)
    return ToolResult(ok=bool(result.get("registered")), output=json.dumps(result, ensure_ascii=False),
        runtimeYield=result.get("decision", {}).get("status") == "waiting")


def _agent_cancel_input(value):
    if not isinstance(value, dict) or set(value) != {"task_id", "operation_id", "reason"}:
        raise ValueError("cancel requires task_id, operation_id and reason")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in value.values()):
        raise ValueError("cancel fields must be non-blank bounded strings")
    return value


def _cancel_agent_run(value, context):
    result = _agent_gateway(context).cancel_agent(parent_id=context.node_id,
        attempt_id=context.attempt_id, **value)
    return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False))


def _dismiss_agent_run(value, context):
    try:
        result = _agent_gateway(context).dismiss_agent(parent_id=context.node_id,
            attempt_id=context.attempt_id, **value)
    except ValueError as error:
        return ToolResult(ok=False, output=f"Input validation error in dismiss_agent: {error}",
            failureKind="invalid_input", inputValidationFailed=True, inputRejected=True)
    return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False))


def _agent_message_input(value):
    if (not isinstance(value, dict) or not {"recipient_id", "message_id", "kind", "content"} <= set(value)
        or set(value) - {"recipient_id", "message_id", "kind", "content", "reply_to"}):
        raise ValueError("message requires recipient_id, message_id, kind and content")
    if any(not isinstance(value[k], str) or not value[k].strip() for k in value):
        raise ValueError("message fields must be non-blank strings")
    if len(value["message_id"]) > 128 or len(value["content"]) > 60000:
        raise ValueError("message_id is limited to 128 characters and content to 60000; split larger work explicitly")
    if len(value.get("reply_to", "")) > 300:
        raise ValueError("reply_to must identify a bounded sender_id:message_id")
    if value["kind"] not in {"instruction", "progress", "question", "result"}:
        raise ValueError("unknown message kind")
    return value


def _send_agent_message_run(value, context):
    result = _agent_gateway(context).send_agent_message(sender_id=context.node_id,
        attempt_id=context.attempt_id, **value)
    return ToolResult(ok=result["state"] != "rejected", output=json.dumps(result, ensure_ascii=False))


def _agent_resume_input(value):
    if (not isinstance(value, dict) or not {"task_id", "idempotency_key", "objective"} <= set(value)
        or set(value) - {"task_id", "idempotency_key", "objective", "write_scope", "acceptance_criteria", "checks"}):
        raise ValueError("resume requires task_id, idempotency_key and objective")
    if any(not isinstance(value[k], str) or not value[k].strip() for k in ("task_id", "idempotency_key", "objective")):
        raise ValueError("resume fields must be non-blank strings")
    if len(value["idempotency_key"]) > 128 or len(value["task_id"]) > 128 or len(value["objective"]) > 12000:
        raise ValueError("resume fields exceed the bounded dispatch limits")
    return value


def _resume_agent_run(value, context):
    return _continue_agent_run(value, context, "resume_agent")


def _reassign_agent_run(value, context):
    if set(value) - {"task_id", "idempotency_key", "objective"}:
        raise ValueError("reassignment retains the original read-only obligation")
    return _continue_agent_run(value, context, "reassign_agent")


def _continue_agent_run(value, context, operation):
    from minicode.workspace_provider import WorkspacePreparationCanceled
    try:
        result = getattr(_agent_gateway(context), operation)(parent_id=context.node_id,
            attempt_id=context.attempt_id, cancellation_event=context.cancellation_event, **value)
    except WorkspacePreparationCanceled as error:
        return ToolResult(ok=False, output=json.dumps({"operation": operation, "registered": False,
            "error": str(error)}, ensure_ascii=False), runtimeControlSettled=True)
    return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False), runtimeControlSettled=True)


def _team_control_input(value):
    if not isinstance(value, dict) or set(value) - {"action", "task_id"}:
        raise ValueError("team control expects action and an optional task_id")
    if value.get("action") not in {"status", "drain", "retire"}:
        raise ValueError("team action must be status, drain or retire")
    if (value["action"] == "retire") != (isinstance(value.get("task_id"), str) and bool(value["task_id"])):
        raise ValueError("only retire requires the task_id of an idle member")
    return value


def _team_control_run(value, context):
    result = _agent_gateway(context).team_control(parent_id=context.node_id, attempt_id=context.attempt_id, **value)
    return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False))


def _agent_directory_input(value):
    if not isinstance(value, dict) or set(value) - {"include_history", "cursor"}:
        raise ValueError("list_agents accepts include_history and cursor")
    if type(value.get("include_history", False)) is not bool or type(value.get("cursor", 0)) is not int or value.get("cursor", 0) < 0:
        raise ValueError("invalid agent directory cursor or history flag")
    return value


def _agent_directory_run(value, context):
    result = _agent_gateway(context).list_agents(node_id=context.node_id, attempt_id=context.attempt_id, **value)
    return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False))


def _agent_ack_input(value):
    if not isinstance(value, dict) or set(value) != {"task_ids", "note"}:
        raise ValueError("ack_agent_results requires task_ids and a handling note")
    ids, note = value["task_ids"], value["note"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 32 or any(not isinstance(k, str) or not k for k in ids)
        or len(set(ids)) != len(ids) or not isinstance(note, str) or not note.strip() or len(note) > 1000):
        raise ValueError("acknowledgment requires 1..32 unique task IDs and a bounded nonempty note")
    return value


def _agent_ack_run(value, context):
    result = _agent_gateway(context).ack_agent_results(node_id=context.node_id, attempt_id=context.attempt_id, **value)
    return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False))


def _agent_check_input(value):
    import re
    if (not isinstance(value, dict) or set(value) != {"check_id", "operation_id"}
        or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v) for v in value.values())):
        raise ValueError("run_agent_check requires declared check_id and stable operation_id identifiers")
    return value


def _agent_check_run(value, context):
    result = _agent_gateway(context).run_agent_check(context, **value)
    report = result.get("report", {})
    invalid_declaration = report.get("command_executed") is False and bool(report.get("launch_issues"))
    return ToolResult(ok=result.get("state") == "completed" and result.get("passed") is True,
        output=json.dumps(result, ensure_ascii=False), runtimeControlSettled=True,
        failureKind="invalid_input" if invalid_declaration else None,
        inputRejected=invalid_declaration, inputValidationFailed=invalid_declaration,
        inputIssues=[{"path": ["check_id"], "expected": "a runnable parent-declared check; ask the primary to correct its command/args/cwd, not just operation_id",
                      "actual": value["check_id"]}] if invalid_declaration else [])


def _interrupt_agent_run(value, context):
    result = _agent_gateway(context).interrupt_agent(parent_id=context.node_id, attempt_id=context.attempt_id, **value)
    return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False))


AGENT_CHECK_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string"}, "command": {"type": "string", "description": "Executable for an authorized local test, without shell concatenation"},
    "args": {"type": "array", "items": {"type": "string"}},
    "cwd": {"type": "string", "description": "Relative directory within the configured check workspace (current workspace by default); default ."},
    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300}},
    "required": ["id", "command"], "additionalProperties": False}


def async_agent_tools() -> tuple[ToolDefinition, ...]:
    """Native controls: scoped separately from filesystem rights, never parallel tools."""
    definitions = (
        ("interrupt_agent", "Interrupt one active checkpointed assistant while retaining its logical session and partial edits. Inspect actual worker exit before resume_agent. Unlike cancellation this is a controlled pause; a later cancel revokes continuation. Independent verifiers cannot be interrupted for reuse.",
         {"task_id": {"type": "string"}, "operation_id": {"type": "string"}, "reason": {"type": "string"}},
         ["task_id", "operation_id", "reason"], _agent_cancel_input, _interrupt_agent_run),
        ("run_agent_check", "Execute one primary-declared local check in the writer's current shared workspace by default. Only explicit runtimeVerificationIsolation=true requests a verified copy. Temporary output and caches use output_directory; direct execution preserves actual file changes. Source changes invalidate the check without rollback. Normal command permissions, timeout and cancellation apply. Coordinate concurrent writes with the primary; file-tool write_scope is not a process sandbox. Inspect passed, exit_code, input_revision and report_artifact; passing is not final integration acceptance. Reuse operation_id only to read the same invocation; a new run needs a new key. An unsettled invocation is never automatically replayed.",
         {"check_id": {"type": "string"}, "operation_id": {"type": "string"}},
         ["check_id", "operation_id"], _agent_check_input, _agent_check_run),
        ("list_agents", "List this parent's team directory, including stable session IDs, names, current task IDs, rounds and message availability. After a provider balance stop, retained_agents lists the previous run's team, findings and continuation_available/blocked_reason. Inspect these before spawning replacements; resume_agent can continue a retained task_id. Assistants may inspect current peers. Use session_id to message the current round; a message never starts an idle agent. include_history pages earlier tasks.",
         {"include_history": {"type": "boolean"}, "cursor": {"type": "integer", "minimum": 0}},
         [], _agent_directory_input, _agent_directory_run),
        ("ack_agent_results", "Record how the primary handled checkpoint-delivered completion notices. This is NOT acceptance and cannot clear failed checks or cancellation. Full evidence remains available through poll_agents.",
         {"task_ids": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "string"}}, "note": {"type": "string", "maxLength": 1000}},
         ["task_ids", "note"], _agent_ack_input, _agent_ack_run),
        ("dismiss_agent", "Explicitly waive a predeclared optional read-only task after it was canceled or failed and its worker demonstrably exited. Supply the reason this assistance is no longer needed. Cancellation alone never waives delivery. Required tasks, writers, independent verifiers and uncertain side effects cannot be dismissed. The original outcome and budget remain recorded; this is not success or acceptance. Reuse the same operation_id and reason for retries.",
         {"task_id": {"type": "string"}, "operation_id": {"type": "string"}, "reason": {"type": "string"}},
         ["task_id", "operation_id", "reason"], _agent_cancel_input, _dismiss_agent_run),
        ("reassign_agent", "Reassign a settled failed read-only team task with fresh context and a new generation. Keep the exact failed objective. Only recoverable execution failures are eligible; canceled, budget, policy, ambiguous and verification failures remain blocking. Old results remain audited. Bounded to four generations and the same run budget.",
         {"task_id": {"type": "string"}, "idempotency_key": {"type": "string"}, "objective": {"type": "string"}},
         ["task_id", "idempotency_key", "objective"], _agent_resume_input, _reassign_agent_run),
        ("team_control", "Inspect the parent-child team's lifecycle and members; retire an exited idle member to free its place; or drain after all work is assigned. Draining permanently closes new assignments in this run, but lets existing work and messages finish. Completion is decided by the Runtime delivery gate.",
         {"action": {"type": "string", "enum": ["status", "retire", "drain"]}, "task_id": {"type": "string"}},
         ["action"], _team_control_input, _team_control_run),
        ("resume_agent", "Continue a successful idle or explicitly interrupted assistant on related work after its worker exits, keeping its session and conversation with a new task and fresh permissions. Also accepts a previous-run task_id returned by list_agents.retained_agents with continuation_available=true after a provider balance stop. Reuses its name and history; never replays prior tool calls. In collaboration-enabled runs writers require a freshly renewed write_scope, acceptance_criteria and checks. Reread current files. Use the latest task ID. Ordinary canceled/failed tasks and independent verifiers cannot resume; use spawn_agents for unrelated work.",
         {"task_id": {"type": "string"}, "idempotency_key": {"type": "string"}, "objective": {"type": "string"},
          "write_scope": {"type": "array", "items": {"type": "string"}},
          "acceptance_criteria": {"type": "array", "items": {"type": "string"}}, "checks": {"type": "array", "maxItems": 8, "items": AGENT_CHECK_SCHEMA}},
         ["task_id", "idempotency_key", "objective"], _agent_resume_input, _resume_agent_run),
        ("send_agent_message", "Send a bounded message to an owned assistant, its parent, or a peer returned by list_agents when peer communication is enabled. Independent verifiers remain isolated. Parent sends instruction; assistants send progress, question or result. reply_to is sender_id:message_id of a received message. queued is not consumed; consumed means checkpointed context, not completed work. Use resume_agent for finished assistants; messages do not start work or grant permissions.",
         {"recipient_id": {"type": "string", "description": "Owned assistant id, or parent from an assistant"},
           "message_id": {"type": "string"}, "kind": {"type": "string", "enum": ["instruction", "progress", "question", "result"]},
           "reply_to": {"type": "string", "description": "Full sender_id:message_id of the message being answered"},
          "content": {"type": "string", "maxLength": 60000,
              "description": "Up to 6000 characters inline; longer bodies are stored as owned artifacts and read by the recipient in bounded pages."}},
         ["recipient_id", "message_id", "kind", "content"], _agent_message_input, _send_agent_message_run),
        ("spawn_agents", "Dispatch 1..8 assistants with independent context. explore/plan/review are read-only and default to the parent's live shared directory. In enabled Ultra direct execution, worker edits that same directory and requires a nonempty write_scope of relative files/directories; overlapping assignments queue. The parent must not edit assigned paths until release. Workers use native file tools, read existing files before writing, and preserve partial edits on failure/cancel; never reapply their changes as patches. Wait for writers before commands and final checks. Read-only assistants may choose snapshot for fixed current files or worktree for committed Git HEAD. verify defaults to snapshot and cannot share. background returns after registration; foreground waits; auto waits up to its threshold. Poll/wait for results; registration is not completion. Reuse a key only for the same assignment, isolation and scope.",
         {"tasks": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
             "type": "object", "properties": {"idempotency_key": {"type": "string"},
                  "name": {"type": "string", "maxLength": 80, "description": "Stable module responsibility shown in the team directory and UI"},
                  "acceptance_criteria": {"type": "array", "maxItems": 16, "items": {"type": "string"}},
                  "checks": {"type": "array", "maxItems": 8, "items": AGENT_CHECK_SCHEMA,
                      "description": "Worker-only local checks authorized by the primary; use the current shared workspace unless runtime verification isolation is explicitly enabled"},
                  "optional": {"type": "boolean", "default": False,
                      "description": "Use true only for supplementary explore/plan/review assistance that may be abandoned. After cancel and actual worker exit, dismiss_agent can explicitly waive it. All tasks default required; workers and verifiers cannot be optional. Immutable across continuation."},
                  "objective": {"type": "string"}, "agent_type": {"type": "string", "enum": ["explore", "plan", "review", "verify", "worker"],
                     "description": "Default explore for ongoing investigation and future follow-ups. plan/review may also continue. verify is an independent one-shot verifier: it can NEVER resume or fork. Use explore when the plan requires resume_agent."},
                 "context_mode": {"type": "string", "enum": ["isolated", "fork"], "description": "Conversation context only, not filesystem isolation. Default isolated. fork requires an enabled compatible read-only parent; verifiers and writers remain independent."},
                 "write_scope": {"type": "array", "items": {"type": "string"}, "description": "Required for worker: nonempty relative files/directories, no wildcards or parent traversal. Other roles cannot have a write scope."},
                 "report_artifact_ids": {"type": "array", "maxItems": 8, "uniqueItems": True,
                     "items": {"type": "string", "pattern": "^artifact-[a-f0-9]{32}$"},
                     "description": "Explicitly attach current-run npm-build-and-browser reports/screenshots. Grants runtime_artifact_read for these IDs only; mentioning IDs in the objective does not grant access. Reports describe the checked revision, not necessarily the latest files."},
                 "isolation": {"type": "string", "enum": ["shared", "snapshot", "worktree"],
                     "description": "Default shared; verify defaults to snapshot. shared uses the live parent directory: read-only roles observe it, worker writes its assigned scope directly; neither replaces final acceptance. snapshot copies current files including dependencies. worktree checks out committed HEAD of the selected project's Git repository; requires Git and a commit, excludes uncommitted/ignored files, and never auto-falls back to another mode. Use snapshot when verifying current changes."}},
             "required": ["idempotency_key", "objective"], "additionalProperties": False}},
          "mode": {"type": "string", "enum": ["background", "foreground", "auto"], "default": "background"},
          "auto_after_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600, "default": 120}},
         ["tasks"], _validate_agent_dispatch, _spawn_agents_run),
        ("poll_agents", "Read owned assistants returned by spawn_agents, not spawn_builders (use poll_task_results for Builders). Shared results have input_is_current=null because live reads may span versions; reread relevant files before relying on them. Fixed old inputs are historical evidence, not current acceptance. Terminal tasks may have failed or been canceled; inspect the result.",
         {"task_ids": {"type": "array", "items": {"type": "string"}}, "cursor": {"type": "integer", "minimum": 0}},
         [], _agent_poll_input, _poll_agents_run),
        ("wait_agents", "Wait only for owned assistants returned by spawn_agents. For spawn_builders IDs use wait_dependencies with terminal_observed edges to inspect either outcome. Checkpoint and release the primary execution slot until selected assistants terminate, change generation or the bounded timeout returns control (default 60 seconds). any waits for the first, all waits for the set. Before primary commands use all for every unfinished shared writer. Inspect results and scope release; termination is not success. A timeout stops waiting, not the assistants. Use a new wait_id for another wait after timeout; do not repeatedly wait on terminal/unresolved tasks.",
         {"task_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
          "mode": {"type": "string", "enum": ["all", "any"]}, "wait_id": {"type": "string"},
          "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600, "default": 60}},
         ["task_ids", "wait_id"], _agent_wait_input, _wait_agents_run),
        ("cancel_agent", "Stop one owned assistant through the original run cancellation authority. Does not stop siblings or reset budgets. Cancellation does not waive the task's delivery obligation. For a predeclared optional read-only assistant, wait for worker exit and explicitly dismiss_agent if its help is no longer needed.",
         {"task_id": {"type": "string"}, "operation_id": {"type": "string"}, "reason": {"type": "string"}},
         ["task_id", "operation_id", "reason"], _agent_cancel_input, _cancel_agent_run),
    )
    return tuple(ToolDefinition(name=name, description=description,
        input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        validator=validate, run=run, metadata=ToolMetadata(name=name, description=description,
            effect=ToolEffect.PRIVILEGED, replay_policy=ReplayPolicy.IDEMPOTENT_WITH_KEY,
            runtime_control=True, candidate_control=True))
        for name, description, properties, required, validate, run in definitions)


def _run(input_data: dict, context) -> ToolResult:
    runtime_context = getattr(context, "runtime_context", None)
    gateway = getattr(runtime_context, "delegation_gateway", runtime_context)
    if gateway is None or not callable(getattr(gateway, "submit", None)):
        return ToolResult(
            ok=False,
            output=(
                "DelegationUnavailable: task requires an active unified Runtime "
                "and cannot start an unregistered AgentLoop"
            ),
        )

    parent_node_id = str(getattr(context, "node_id", "") or "")
    if not parent_node_id:
        return ToolResult(ok=False, output="DelegationUnavailable: parent node identity is missing")
    agent_type = input_data["agent_type"]
    if getattr(gateway, "primary_led", False) and agent_type == "general":
        return ToolResult(
            ok=False,
            output=("DelegationDenied: task only supports read-only consultation in primary-led execution. "
                    "Use explore, plan, review, or verify; use an exposed writer-delegation tool for implementation."),
        )
    definition = AGENT_TYPES[agent_type]
    depth = 2 if parent_node_id.startswith("delegated-") else 1
    identity = {
            "run_id": gateway.run_id,
            "parent_node_id": parent_node_id,
            "agent_type": agent_type,
            "objective": input_data["prompt"],
    }
    if getattr(gateway, "primary_led", False):
        # Repeating a question after editing the candidate is new work, not a
        # replay of the old answer. Keep legacy keys unchanged for recovery.
        identity["input_revision"] = snapshot_revision(workspace_snapshot(Path(context.cwd)))
        identity["contract_revision"] = gateway.contract_revision
    canonical = json.dumps(
        identity,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    request = DelegationRequest(
        run_id=gateway.run_id,
        parent_node_id=parent_node_id,
        requested_role=agent_type,
        objective=input_data["prompt"],
        context_requirements=("task-contract", "repository", "parent-result-context"),
        allowed_tool_capabilities=definition["allowed_effects"],
        proposed_budget=BudgetEnvelope(
            max_agents=1,
            max_active_workers=1,
            max_delegation_depth=max(0, gateway.max_delegation_depth - depth + 1),
            # No automatic per-child allocation. The atomic run ledger bounds
            # all helpers together; explicit direct request caps remain intact.
            max_tokens=None,
            retry_budget=0,
        ),
        delegation_depth=depth,
        idempotency_key=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        parent_attempt_id=getattr(context, "attempt_id", None),
    )
    try:
        result = gateway.submit(request)
    except Exception as error:
        return ToolResult(
            ok=False,
            output=f"DelegationFailed: {type(error).__name__}: {error}",
        )

    artifact_lines = [
        f"  Artifact: {ref.artifact_id} sha256={ref.content_hash} size={ref.size} key={ref.store_key}"
        for ref in result.artifact_refs
    ]
    header = [
        f"[Governed Worker {definition['name']} {result.status.value}]",
        f"  Run: {result.run_id}",
        f"  Node: {result.node_id}",
        f"  Attempt: {result.attempt_id}",
        f"  Plan version: {result.plan_version}",
        *artifact_lines,
    ]
    body = "\n".join(header) + "\n\n" + result.output_summary
    if result.failure is not None:
        body += f"\n\nFailure: {result.failure.kind.value}\n{result.failure.message}"
        if result.failure.kind is ExecutionFailureKind.CONSULTATION_BUDGET_LIMITED:
            body += (
                "\nRuntime declined a model request against the local consultation budget. "
                "Changing the task label or switching review to verify does not resolve that limit. "
                "Inspect the budget and narrow the assignment before retrying; this is not a passed review. "
                "This refusal does not satisfy required consultation or final verification."
            )
    return ToolResult(ok=result.status is AgentResultStatus.SUCCEEDED, output=body)


task_tool = ToolDefinition(
    name="task",
    description=(
        "Submit a governed child Worker through the active Runtime Scheduler. "
        "Use explore, plan, review, or verify for bounded read-only consultation. "
        "This call waits for the child result and does not run in the background. "
        "general is only available in the legacy Standard compatibility route; "
        "primary-led execution restricts this tool to read-only helpers; use an exposed writer-delegation tool for implementation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short task label"},
            "prompt": {"type": "string", "description": "Full child objective"},
            "agent_type": {
                "type": "string",
                "enum": ["explore", "plan", "review", "verify", "general"],
            },
        },
        "required": ["description"],
    },
    validator=_validate,
    run=_run,
)


def _validate_read_only(input_data: dict) -> dict:
    validated = _validate({"agent_type": "explore", **input_data})
    if validated["agent_type"] == "general":
        raise ValueError("primary-led consultation only permits explore, plan, review, or verify")
    return validated


def _run_read_only(input_data: dict, context) -> ToolResult:
    runtime_context = getattr(context, "runtime_context", None)
    gateway = getattr(runtime_context, "delegation_gateway", runtime_context)
    if not getattr(gateway, "primary_led", False):
        return ToolResult(ok=False, output="DelegationDenied: scoped consultation requires the primary-led Runtime")
    return _run(input_data, context)


primary_task_tool = ToolDefinition(
    name="task",
    description=(
        "Consult one bounded read-only explore (default), plan, review or verify helper. "
        "Waits serially, never in background. This helper cannot edit; primary retains final decisions. "
        "An identical assignment on unchanged files reuses its successful result. "
        "Use an exposed writer-delegation tool for implementation assignments."
    ),
    input_schema={
        **task_tool.input_schema,
        "properties": {
            **task_tool.input_schema["properties"],
            "prompt": {"type": "string", "description": (
                "Focused question, workspace-relative sources, and evidence needed for the primary's next decision. "
                "Keep necessary constraints and edge cases, but do not copy specifications or repeat exhaustive test lists by default. "
                "Use relative paths, not a host workspace path. "
                "The helper receives the original user constraints and reads sources under its own permissions."
            )},
            "agent_type": {"type": "string", "enum": ["explore", "plan", "review", "verify"]},
        },
    },
    validator=_validate_read_only,
    run=_run_read_only,
    metadata=ToolMetadata(
        name="task",
        description="Primary-owned read-only consultation",
        effect=ToolEffect.READ_ONLY,
        replay_policy=ReplayPolicy.IDEMPOTENT_WITH_KEY,
        # Read-only effect does not imply concurrency safety. A synchronous
        # child borrows the parent's execution slot and candidate workspace.
        capabilities=set(),
    ),
)


def _validate_batch(input_data: dict) -> tuple[dict, ...]:
    assignments = input_data.get("tasks")
    if not isinstance(assignments, list) or not 1 <= len(assignments) <= 8:
        raise ValueError("tasks must contain between 1 and 8 bounded consultations")
    validated = []
    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise ValueError("each consultation must be an object")
        item = _validate_read_only(assignment)
        if len(item["prompt"]) > 12000:
            raise ValueError("consultation prompt exceeds the 12000 character limit")
        criticality = assignment.get("criticality", "required")
        if criticality not in {"required", "advisory"}:
            raise ValueError("criticality must be required or advisory")
        if criticality == "advisory" and item["agent_type"] not in {"explore", "plan"}:
            raise ValueError("review and verify consultations are always required")
        files = consultation_context_files(assignment.get("context_files", ()))
        validated.append({"prompt": item["prompt"], "agent_type": item["agent_type"],
                          **({"context_files": files} if files else {}),
                          **({"criticality": criticality} if criticality != "required" else {})})
    if len({(item["prompt"], item["agent_type"]) for item in validated}) != len(validated):
        raise ValueError("duplicate consultation assignments are not allowed")
    return tuple(validated)


def _run_batch(assignments: tuple[dict, ...], context) -> ToolResult:
    runtime_context = getattr(context, "runtime_context", None)
    gateway = getattr(runtime_context, "delegation_gateway", runtime_context)
    if not callable(getattr(gateway, "prepare_consultation", None)):
        return ToolResult(ok=False, output="Durable consultation requires the unified Runtime")
    receipt = gateway.prepare_consultation(
        parent_id=context.node_id, attempt_id=context.attempt_id,
        revision=snapshot_revision(workspace_snapshot(Path(context.cwd))), assignments=assignments,
    )
    return ToolResult(ok=True, output=json.dumps(receipt, ensure_ascii=False), runtimeYield=receipt["status"] == "queued")


durable_consultation_tool = ToolDefinition(
    name="consult_agents",
    description=(
        "Checkpoint primary and wait for read-only helpers within the worker limit, then resume with results. "
        "Use one, or batch independent questions; primary waits, helpers never write. "
        "Ask for decision-ready findings, not a restatement of specifications. "
        "Default required. Only optional explore/plan advice may be advisory BEFORE dispatch. "
        "Eligible failed advice still blocks until explicit takeover. Never waive required acceptance; review/verify are always required."
    ),
    input_schema={"type": "object", "properties": {
        "tasks": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
            **primary_task_tool.input_schema, "properties": {
                **primary_task_tool.input_schema["properties"],
                "criticality": {"type": "string", "enum": ["required", "advisory"], "default": "required"},
                "context_files": {"type": "array", "maxItems": 8, "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 256},
                    "description": "Optional explicit workspace-relative files, using /. Runtime reads the first 1000 characters of each under the helper's own permissions before its first model request. Original read results count toward its existing input budget; truncation/errors stay visible. Select only needed sources, not conclusions; omit for open-ended discovery."},
            },
        }},
    }, "required": ["tasks"]},
    validator=_validate_batch,
    run=_run_batch,
    metadata=ToolMetadata(name="consult_agents", description="Durable read-only consultation batch",
                          effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.IDEMPOTENT_WITH_KEY),
)


def _validate_consultation_retry(value):
    if value != {}:
        raise ValueError("retry_consultation takes no arguments; the original requirements are immutable")
    return value


def _run_consultation_retry(_value, context):
    runtime_context = getattr(context, "runtime_context", None)
    gateway = getattr(runtime_context, "delegation_gateway", runtime_context)
    if not callable(getattr(gateway, "retry_consultation", None)):
        return ToolResult(ok=False, output="Consultation recovery requires the unified Runtime")
    receipt = gateway.retry_consultation(parent_id=context.node_id, attempt_id=context.attempt_id,
        revision=snapshot_revision(workspace_snapshot(Path(context.cwd))))
    return ToolResult(ok=True, output=json.dumps(receipt, ensure_ascii=False), runtimeYield=receipt["status"] == "queued")


retry_consultation_tool = ToolDefinition(
    name="retry_consultation",
    description=("After reading the delivered review and fixing its findings, recheck the CURRENT candidate with "
        "the exact original consultation batch. No arguments; Runtime preserves all requirements and verdict history. "
        "Checkpoint and yield, then resume with fresh results. Required failures cannot be waived; retries are bounded."),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    validator=_validate_consultation_retry, run=_run_consultation_retry,
    metadata=ToolMetadata(name="retry_consultation", description="Recheck the original consultation after repair",
                          effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.IDEMPOTENT_WITH_KEY),
)


def _run_durable_primary_task(value, context):
    if value["agent_type"] in {"review", "verify"}:
        return _run_batch(_validate_batch({"tasks": [value]}), context)
    return _run_read_only(value, context)


def recoverable_primary_task_tool() -> ToolDefinition:
    """Product primary uses the same durable recovery for single reviews."""
    return replace(primary_task_tool, run=_run_durable_primary_task,
        description=("Consult one bounded read-only helper. explore/plan wait serially. "
            "review/verify checkpoint and release the primary slot, then resume with the original report. "
            "Failed required reviews return for repair and retry_consultation; success is never assumed. "
            "Helpers cannot edit; use writer-delegation tools for implementation."))


def scoped_consultation_tool(requirement=None) -> ToolDefinition:
    """Publish a required contract and allow one no-progress input correction.

    Fresh per primary activation; no shared mutable schema or counters. This is
    only input ergonomics: Scheduler still validates the live contract/lease,
    and no validation success authorizes dispatch or waives the budget gate.
    """
    minimum = requirement.min_helpers if requirement and requirement.single_batch else 1
    schema = deepcopy(durable_consultation_tool.input_schema)
    schema["properties"]["tasks"]["minItems"] = minimum
    description = durable_consultation_tool.description
    if requirement is None:
        # Optional consultation must not terminalize an otherwise viable task.
        # Keep its existing fallback/error semantics, including after fulfillment.
        return replace(durable_consultation_tool, input_schema=schema)
    if requirement:
        description += (f" This task requires at least {minimum} distinct REQUIRED helpers in this tasks array. "
                        "Submit the complete array in ONE call; a sequence of smaller batches is not equivalent."
                        if requirement.single_batch else
                        " This task requires successful REQUIRED helpers; submit at least one required task per batch.")
    best_required = -1
    repeated_errors: dict[str, int] = {}
    terminal_output = None

    def reject(code: str, message: str, *, received: int | None = None):
        nonlocal terminal_output
        repeated_errors[code] = repeated_errors.get(code, 0) + 1
        exhausted = repeated_errors[code] >= 2
        payload = {"code": code, "message": message[:320], "minimum_required": minimum if requirement else 0,
                   "received_required": received, "remaining_corrections": 0 if exhausted else 1,
                   "action": "stop_incomplete" if exhausted else "correct_batch_once"}
        output = json.dumps(payload, ensure_ascii=False)
        if exhausted:
            terminal_output = output
        raise ToolInputRejected(output, exhausted=exhausted)

    def validate(value):
        nonlocal best_required
        if terminal_output is not None:
            raise ToolInputRejected(terminal_output, exhausted=True)
        try:
            if not isinstance(value, dict):
                raise ValueError("consult_agents input must be an object with a tasks array")
            assignments = _validate_batch(value)
        except (ValueError, TypeError, KeyError) as error:
            # Do not echo arbitrary arguments into the next model request.
            reject("consultation_input_invalid", str(error).split(". Got:", 1)[0])
        required = sum(item.get("criticality", "required") == "required" for item in assignments)
        if requirement and required < minimum:
            if required > best_required:
                best_required = required
                repeated_errors.pop("consultation_required_count", None)
            reject("consultation_required_count",
                   f"Submit at least {minimum} distinct required read-only tasks together in ONE tasks array; "
                   "do not resubmit a smaller batch or invent placeholder helpers.", received=required)
        repeated_errors.clear()
        best_required = -1
        return assignments

    return replace(durable_consultation_tool, description=description, input_schema=schema, validator=validate)


def _validate_advisory_acknowledgment(input_data: dict) -> dict[str, str]:
    child_id, reason = input_data.get("child_id"), input_data.get("reason")
    if not isinstance(child_id, str) or not child_id.startswith("consult-") or len(child_id) > 128:
        raise ValueError("child_id must identify a delivered consultation")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
        raise ValueError("reason must explain the replacement work in 1..2000 characters")
    return {"child_id": child_id, "reason": reason.strip()}


def _run_advisory_acknowledgment(input_data: dict[str, str], context) -> ToolResult:
    runtime_context = getattr(context, "runtime_context", None)
    gateway = getattr(runtime_context, "delegation_gateway", runtime_context)
    if not getattr(gateway, "advisory_enabled", False):
        return ToolResult(ok=False, output="Advisory takeover requires a new v6 durable primary run")
    receipt = gateway.acknowledge_advisory_failure(parent_id=context.node_id, attempt_id=context.attempt_id, **input_data)
    return ToolResult(ok=True, output=json.dumps(receipt, ensure_ascii=False))


advisory_acknowledgment_tool = ToolDefinition(
    name="acknowledge_advisory_failure",
    description=("Take over failed advisory explore/plan AFTER diagnosis delivery, only when fallback_allowed. "
                 "Give child_id and a replacement plan. Failure stays failure; required checks remain binding. "
                 "No waiver for global budget, policy, cancellation or uncertain effects."),
    input_schema={"type": "object", "properties": {"child_id": {"type": "string"}, "reason": {"type": "string"}},
                  "required": ["child_id", "reason"]},
    validator=_validate_advisory_acknowledgment,
    run=_run_advisory_acknowledgment,
    metadata=ToolMetadata(name="acknowledge_advisory_failure", description="Audited primary takeover of optional advice",
                          effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.IDEMPOTENT_WITH_KEY),
)
