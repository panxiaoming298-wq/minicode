"""Native tools the primary uses to run and accept parallel Builder work.

These are the production entry points for the parallel-candidate mode. They are
registered for a primary only when the Runtime actually enabled the mode AND
bound a real ``BuilderGateway``; the preflight refuses the arm otherwise, so a
profile can never advertise parallel capacity that the model cannot reach.

Every tool re-reads live ownership and cancellation through the bound gateway.
None of them accepts authority from the model's own text, and ``spawn_builders``
returns as soon as the tasks are registered rather than pretending they ran.
"""

from __future__ import annotations

import json
from typing import Any

from minicode.builder_delegation import BUILDER_TOOL_NAMES, BuilderGateway
from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.tooling import ToolDefinition, ToolMetadata
from minicode.tools._validation import validate_contract


CANCEL_TASK_INPUT = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "task_id": {"type": "string", "minLength": 1},
        "reason": {"type": ["string", "null"],
                   "description": "Cancellation reason; omission, null or an empty string uses the default reason."},
        "subtree": {"type": "boolean", "default": True,
                    "description": "Cancel created children too. Use boolean false to cancel only this task; never quote it."},
        "dependency_successors": {"type": "boolean", "default": False,
                                  "description": "Also cancel dependent tasks only when explicitly true."},
        "operation_id": {"type": ["string", "null"], "minLength": 1,
                         "description": "Stable key for replaying the same operation; omit or use null to let the Runtime assign it."},
    },
    "required": ["task_id"],
}


def _gateway(context: Any) -> BuilderGateway | None:
    runtime_context = getattr(context, "runtime_context", None)
    gateway = getattr(runtime_context, "builder_gateway", None)
    return gateway if isinstance(gateway, BuilderGateway) else None


def _denied(name: str) -> tuple[None, Any]:
    return None, None


def _spawn_validate(input_data: dict) -> dict:
    tasks = input_data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("spawn_builders needs a non-empty tasks array")
    if len(tasks) > 2:
        raise ValueError("at most two Builders may run at once; split larger work")
    return {"tasks": tasks}


def _spawn_run(input_data: dict, context: Any):
    from minicode.tooling import ToolResult

    gateway = _gateway(context)
    if gateway is None:
        return ToolResult(ok=False, output=(
            "BuilderUnavailable: this Runtime did not enable parallel candidates. "
            "Continue in your own candidate and use read-only consultation instead."))
    try:
        outcome = gateway.spawn(input_data["tasks"])
    except Exception as error:  # noqa: BLE001 - a refusal is a reportable result
        return ToolResult(ok=False, output=f"BuilderRejected: {type(error).__name__}: {error}")
    registered = outcome.get("registered", [])
    lines = [f"registered={len(registered)} refused={len(outcome.get('refused', []))}",
             "Registration is not execution. Reuse source evidence already read; do useful independent work "
             "if any remains, otherwise wait_dependencies for terminal_observed to receive success OR failure. "
             "This only observes completion, never accepts a failed task or patch. Do not repeat reads merely to stay busy."]
    for item in registered:
        lines.append(f"- {item.get('task_id')} candidate={item.get('candidate_id')} "
                     f"depends_on={item.get('depends_on')}")
    for item in outcome.get("refused", []):
        lines.append(f"- refused: {item.get('error')}")
    return ToolResult(ok=bool(registered), output="\n".join(lines))


def _poll_validate(input_data: dict) -> dict:
    if "patch_id" in input_data:
        if set(input_data) - {"patch_id", "path", "offset", "max_chars"}:
            raise ValueError("patch review cannot be mixed with task_ids/cursor")
        if not isinstance(input_data["patch_id"], str) or not input_data["patch_id"]:
            raise ValueError("patch_id must be a nonblank string")
        path, offset, limit = input_data.get("path"), input_data.get("offset", 0), input_data.get("max_chars", 12000)
        if path is not None and (not isinstance(path, str) or not path):
            raise ValueError("path must name a changed file")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
            raise ValueError("offset must be non-negative; max_chars must be 1..16000")
        return {"patch_id": input_data["patch_id"], "path": path, "offset": offset, "max_chars": limit}
    task_ids = input_data.get("task_ids")
    if task_ids is not None and (not isinstance(task_ids, list)
                                 or any(not isinstance(item, str) for item in task_ids)):
        raise ValueError("task_ids must be an array of strings")
    cursor = input_data.get("cursor", 0)
    if type(cursor) is not int or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    return {"task_ids": task_ids, "cursor": cursor}


def _poll_run(input_data: dict, context: Any):
    from minicode.tooling import ToolResult

    gateway = _gateway(context)
    if gateway is None:
        return ToolResult(ok=False, output="BuilderUnavailable: parallel candidates are disabled")
    if "patch_id" in input_data:
        try:
            review = gateway.review_patch(**input_data)
        except (ValueError, RuntimeError, KeyError, OSError) as error:
            return ToolResult(ok=False, output=f"PatchReviewRefused: {type(error).__name__}: {error}")
        return ToolResult(ok=True, output=json.dumps(review, ensure_ascii=False))
    outcome = gateway.poll(task_ids=input_data.get("task_ids"), cursor=input_data.get("cursor", 0))
    lines = [f"cursor={outcome['cursor']} results={len(outcome['results'])}"]
    for item in outcome["results"]:
        lines.append(f"- {item['task_id']} terminal={item.get('terminal')} "
                     f"artifact={str(item.get('artifact_sha256'))[:12]} "
                     f"applied={item.get('patch_applied')} checked={item.get('checked')} "
                     f"revision={item.get('revision')}")
    for task_id, milestones in outcome.get("milestones", {}).items():
        lines.append(f"  {task_id} state={milestones.get('state')} "
                     f"artifact_validated={milestones.get('artifact_validated')} "
                     f"patch_applied_and_checked={milestones.get('patch_applied_and_checked')} "
                     f"terminal_observed={milestones.get('terminal_observed')}")
    return ToolResult(ok=True, output="\n".join(lines))


def _wait_validate(input_data: dict) -> dict:
    edges = input_data.get("edges")
    if not isinstance(edges, list) or not edges:
        raise ValueError("wait_dependencies needs a non-empty edges array")
    for edge in edges:
        if not isinstance(edge, dict) or not isinstance(edge.get("task_id"), str) \
                or not isinstance(edge.get("predicate"), str):
            raise ValueError("each edge needs a task_id and a predicate")
    mode = input_data.get("mode", "all")
    if mode not in {"all", "any"}:
        raise ValueError("mode must be all or any")
    failure_policy = input_data.get("failure_policy", "block")
    if failure_policy not in {"block", "continue"}:
        raise ValueError("failure_policy must be block or continue")
    timeout = input_data.get("timeout_seconds")
    if timeout is not None and (type(timeout) not in (int, float) or timeout <= 0):
        raise ValueError("timeout_seconds must be a positive number when supplied")
    return {"edges": edges, "mode": mode, "failure_policy": failure_policy,
            "timeout_seconds": timeout, "wait_id": input_data.get("wait_id")}


def _wait_run(input_data: dict, context: Any):
    from minicode.tooling import ToolResult

    gateway = _gateway(context)
    if gateway is None:
        return ToolResult(ok=False, output="BuilderUnavailable: parallel candidates are disabled")
    outcome = gateway.wait(edges=input_data["edges"], mode=input_data.get("mode", "all"),
                           failure_policy=input_data.get("failure_policy", "block"),
                           timeout_seconds=input_data.get("timeout_seconds"),
                           wait_id=input_data.get("wait_id"))
    if not outcome.get("registered"):
        return ToolResult(ok=False, output=f"WaitRejected: {outcome.get('error')}")
    decision = outcome["decision"]
    blocking = decision.get("blocking_task_ids") or []
    lines = [f"wait_id={outcome['wait']['wait_id']} status={decision.get('status')} "
             f"reason={decision.get('reason')} resume_allowed={decision.get('resume_allowed')} "
             f"durable_resumes={outcome.get('resumed')}"]
    if blocking:
        lines.append("blocking tasks: " + ", ".join(str(item) for item in blocking))
    pending = bool(decision.get("pending_task_ids") or decision.get("pending"))
    waiting = decision.get("status") == "waiting" or (pending and decision.get("resume_allowed") is not True)
    # A wait that is not yet satisfied is a real YIELD, not a normal return. The
    # owner stops here so the Runtime can release its execution slot, deliver the
    # declared results at a safe boundary, and resume exactly once; returning the
    # waiting status as an ordinary tool result left the owner believing it had
    # already finished the dependency step.
    return ToolResult(ok=not blocking, output="\n".join(lines), runtimeYield=waiting,
                      waitKind="dependency_wait" if waiting else None,
                      waitReason=(decision.get("reason") if waiting else None))


def _accept_validate(input_data: dict) -> dict:
    patch_id = input_data.get("patch_id")
    if not isinstance(patch_id, str) or not patch_id:
        raise ValueError("accept_builder_patch needs a patch_id")
    operation_id = input_data.get("operation_id")
    if operation_id is not None and (not isinstance(operation_id, str) or not operation_id):
        raise ValueError("operation_id must be a non-blank string when supplied")
    recover = input_data.get("recover", False)
    if type(recover) is not bool:
        raise ValueError("recover must be a boolean")
    return {"patch_id": patch_id, "operation_id": operation_id, "recover": recover}


def _accept_run(input_data: dict, context: Any):
    from minicode.tooling import ToolResult

    gateway = _gateway(context)
    if gateway is None:
        return ToolResult(ok=False, output="BuilderUnavailable: parallel candidates are disabled")
    if input_data.get("recover"):
        try:
            recovery = gateway.recover(patch_id=input_data["patch_id"])
        except (ValueError, RuntimeError, KeyError, OSError) as error:
            return ToolResult(ok=False, output=f"BuilderRecoveryRefused: {error}")
        return ToolResult(ok=True, output="builder_recovery=" + json.dumps(recovery, ensure_ascii=False) +
            "\nOld patch and usage are retained. The replacement must read current files and produce new evidence. "
            "Wait for its artifact_validated result, review and accept its NEW patch_id. Nothing was merged by this call.")
    operation_id = input_data.get("operation_id") or f"apply-{input_data['patch_id']}"
    accepted = gateway.accept(patch_id=input_data["patch_id"], operation_id=operation_id)
    if not accepted.get("accepted"):
        decision = accepted.get("decision") or {}
        return ToolResult(ok=False, output=(
            f"PatchRejected: {decision.get('reason')} "
            f"(requires_rebase={decision.get('requires_rebase')}). "
            "Nothing was applied. Reading the exported output or repeating this call cannot repair "
            "the producer's missing observations or changed dependencies. Preserve this patch; "
            "recovery requires fresh verified work against the current base and new native evidence. "
            "Use accept_builder_patch with the same patch_id and recover=true to register a bounded fresh Builder generation. "
            "If no supported recovery fits the remaining budget, report the blocker and stop. "
            "Do not clear omissions, rewrite revisions, or claim this patch was merged."))
    applied = gateway.apply(patch_id=input_data["patch_id"], operation_id=operation_id)
    if not applied.get("receipt") or not applied["receipt"].get("applied"):
        refused = applied.get("refused") or {}
        return ToolResult(ok=False, output=(
            f"PatchNotApplied: {refused.get('reason') or 'the write did not complete'}"))
    receipt = applied["receipt"]
    checks = applied.get("checks") or {}
    failed = [name for name, value in checks.items()
              if isinstance(value, dict) and value.get("passed") is False
              and value.get("status") != "not_configured"]
    lines = [f"applied={receipt['applied']} revision={receipt.get('applied_revision')} "
             f"path={receipt.get('promotion_path')} invalidates_prior_checks=True",
             "This is not verification and not integration."]
    if failed:
        lines.append("combination checks failed: " + ", ".join(sorted(failed)))
    elif any(isinstance(value, dict) and value.get("status") == "not_configured" for value in checks.values()):
        lines.append("Combination commands are not configured: no check ran and no check is claimed to have passed. "
                     "Review all merged files, then submit_candidate for mandatory independent final verification.")
    check_summary = {name: {key: value[key] for key in
        ("passed", "status", "scope", "checked_revision", "candidate_unchanged", "error", "reason") if key in value}
        for name, value in checks.items() if isinstance(value, dict)}
    lines.append("check_receipt=" + json.dumps(check_summary, ensure_ascii=False, separators=(",", ":")))
    lines.append("The patch IS applied; the separate check result does not undo that change. "
                 "Do not rewrite or reapply this patch just because the combined candidate is not yet complete.")
    progress = gateway.merge_progress()
    if progress is not None:
        from minicode.builder_request_context import merge_guidance
        lines.append("current_merge=" + json.dumps(progress, ensure_ascii=False, separators=(",", ":")))
        lines.append(merge_guidance(progress))
    return ToolResult(ok=not failed, output="\n".join(lines))


def _cancel_validate(input_data: dict) -> dict:
    validate_contract(input_data, CANCEL_TASK_INPUT)
    task_id = input_data["task_id"]
    if not task_id.strip():
        raise ValueError("$.task_id: must identify a task")
    operation_id = input_data.get("operation_id")
    if operation_id is not None and not operation_id.strip():
        raise ValueError("$.operation_id: must be nonblank when supplied")
    return {"task_id": task_id, "reason": input_data.get("reason") or "canceled by the primary",
            "subtree": input_data.get("subtree", True),
            "operation_id": operation_id,
            "dependency_successors": input_data.get("dependency_successors", False)}


def _cancel_run(input_data: dict, context: Any):
    from minicode.tooling import ToolResult

    gateway = _gateway(context)
    if gateway is None:
        return ToolResult(ok=False, output="BuilderUnavailable: parallel candidates are disabled")
    outcome = gateway.cancel(task_id=input_data["task_id"], reason=input_data["reason"],
                             subtree=input_data.get("subtree", True),
                             operation_id=input_data.get("operation_id"),
                             dependency_successors=input_data.get("dependency_successors", False))
    return ToolResult(ok=True, output=(
        f"canceled={len(outcome['canceled_node_ids'])} epoch={outcome['cancel_epoch']}\n"
        + "\n".join(f"- {item}" for item in outcome["canceled_node_ids"])
        + "\n" + outcome["notice"]))


def _metadata(name: str, description: str) -> ToolMetadata:
    # A Builder registration or an accepted patch changes Runtime state, but it
    # never writes the user's target directory: the Runtime applies into the
    # primary candidate under its own short mutation boundary.
    return ToolMetadata(name=name, description=description,
                        effect=ToolEffect.READ_ONLY,
                        replay_policy=ReplayPolicy.IDEMPOTENT_WITH_KEY,
                        capabilities=set())


def _select_validate(input_data: dict) -> dict:
    if any(not isinstance(input_data.get(key), str) or not input_data[key].strip()
           for key in ("task_id", "reason", "operation_id")):
        raise ValueError("selection requires task_id, reason and a stable operation_id")
    return {key: input_data[key] for key in ("task_id", "reason", "operation_id")}


def _select_run(input_data: dict, context: Any):
    import json
    from minicode.tooling import ToolResult
    gateway = _gateway(context)
    if gateway is None:
        return ToolResult(ok=False, output="BuilderUnavailable: parallel candidates are disabled")
    try:
        selection = gateway.select(**input_data)
        return ToolResult(ok=True, output=json.dumps(selection, ensure_ascii=False))
    except (ValueError, RuntimeError, KeyError) as error:
        return ToolResult(ok=False, output=f"SelectionRejected: {error}")


SELECT_BUILDER_CANDIDATE = ToolDefinition(name="select_builder_candidate",
    description=("Choose one Builder from an explicitly declared alternative_group before accepting its patch. "
        "Other group members are discarded from delivery, but active work must still terminate and uncertain effects remain blocking. "
        "A selection can change only before a former winner's application has started. This cannot waive required task outputs."),
    input_schema={"type": "object", "properties": {"task_id": {"type": "string"},
        "reason": {"type": "string"}, "operation_id": {"type": "string"}},
        "required": ["task_id", "reason", "operation_id"]},
    validator=_select_validate, run=_select_run,
    metadata=_metadata("select_builder_candidate", "Choose a registered alternative without bypassing task acceptance"))


SPAWN_BUILDERS = ToolDefinition(
    name="spawn_builders",
    description=(
        "Register up to two bounded Builder tasks, each in its OWN isolated candidate, and "
        "return immediately. Delegate only when independent scoped work justifies coordination cost; "
        "small or tightly coupled changes usually fit the primary. Give concise objectives, source/spec "
        "paths and acceptance criteria, not copied source or a prewritten implementation. "
        "Builders have file tools, not shell/process execution; arrange installation, builds and browser "
        "checks through the primary's available tools or registered Runtime checks. Include all intended "
        "new paths in write_scope, including root configuration/dotfiles; out-of-scope writes are refused. "
        "A sibling's newly created files are invisible until its patch is accepted into the primary candidate. "
        "Create and accept shared interface files before dispatching consumers; do not tell consumers to read "
        "a file another concurrent Builder has not delivered. "
        "Reuse evidence already read; continue useful independent work or wait_dependencies when none remains. "
        "A Builder never writes the user's target directory; its change becomes a patch that YOU "
        "must accept with accept_builder_patch."),
    input_schema={"type": "object", "properties": {
        "tasks": {"type": "array", "minItems": 1, "maxItems": 2, "items": {
            "type": "object",
            "properties": {
                "idempotency_key": {"type": "string",
                                    "description": "Stable key; a repeat call reuses the task."},
                "objective": {"type": "string", "description": "One bounded, self-contained change."},
                "depends_on": {"type": "array", "items": {"type": "string"},
                               "description": "Task ids that must be satisfied first."},
                "predicate": {"type": "string",
                              "enum": ["artifact_validated", "patch_applied_and_checked",
                                       "terminal_observed"],
                              "description": "What each declared dependency must mean. Code "
                                             "dependencies default to patch_applied_and_checked."},
                "required": {"type": "boolean"},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "write_scope": {"type": "array", "items": {"type": "string"},
                                "description": "Allowed candidate-relative files or directories. Empty means the entire isolated candidate."},
                "alternative_group": {"type": "string", "description": "Optional bounded name for competing solutions to the SAME requirement. Choose one with select_builder_candidate before accepting a patch. Omit for independently required work."},
            },
            "required": ["idempotency_key", "objective"]}}}},
    validator=_spawn_validate, run=_spawn_run, metadata=_metadata(
        "spawn_builders", "Register asynchronous writable Builder candidates"))

POLL_TASK_RESULTS = ToolDefinition(
    name="poll_task_results",
    description=("Read what Builder tasks have actually reported since a cursor. Read-only and "
                 "cheap: it reserves no model budget. The three dependency facts are reported "
                 "separately -- artifact_validated, patch_applied_and_checked, terminal_observed. "
                 "To REVIEW a Builder's actual code before acceptance, pass patch_id (and optional path, "
                 "offset/max_chars) instead of task_ids/cursor. This reads hash-checked exported content "
                 "without applying it. Candidate/task IDs are NOT filesystem paths or generic artifact handles. "
                 "Review changed_paths one file at a time and page when truncated=true."),
    input_schema={"type": "object", "properties": {
        "task_ids": {"type": "array", "items": {"type": "string"}},
        "cursor": {"type": "integer", "minimum": 0},
        "patch_id": {"type": "string", "description": "Native patch id to inspect; mutually exclusive with task_ids/cursor"},
        "path": {"type": "string", "description": "Exact changed path from the patch; defaults to its first changed file"},
        "offset": {"type": "integer", "minimum": 0},
        "max_chars": {"type": "integer", "minimum": 1, "maximum": 16000}}},
    validator=_poll_validate, run=_poll_run, metadata=_metadata(
        "poll_task_results", "Read new Builder results since a cursor"))

WAIT_DEPENDENCIES = ToolDefinition(
    name="wait_dependencies",
    description=("Register a versioned wait on declared Builder edges and evaluate it at once. "
                 "mode=all waits only the declared set; mode=any releases once on the first "
                 "satisfying edge. To observe Builder completion and inspect either success or failure, "
                 "use terminal_observed; this does not grant patch acceptance or successful delivery. "
                 "Timeout returns control with pending tasks still running; it does not cancel them or prove success. "
                 "The default observation window is 120 seconds; poll progress or register a new wait after it expires. "
                 "Use artifact_validated/patch_applied_and_checked only for a strict success dependency: "
                 "a required producer failure TERMINATES the waiting owner, not just this tool call. "
                 "Failed required work still blocks final delivery and cannot be waived by changing this predicate."),
    input_schema={"type": "object", "properties": {
        "edges": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "predicate": {"type": "string",
                          "enum": ["artifact_validated", "patch_applied_and_checked",
                                   "terminal_observed"],
                          "description": "Use terminal_observed to receive the task's outcome; success predicates fail the owner when a required producer fails."},
            "required": {"type": "boolean"}}, "required": ["task_id", "predicate"]}},
        "mode": {"type": "string", "enum": ["all", "any"]},
        "failure_policy": {"type": "string", "enum": ["block", "continue"]},
        "timeout_seconds": {"type": "number"},
        "wait_id": {"type": "string"}}},
    validator=_wait_validate, run=_wait_run, metadata=_metadata(
        "wait_dependencies", "Register and evaluate a versioned dependency wait"))

ACCEPT_BUILDER_PATCH = ToolDefinition(
    name="accept_builder_patch",
    description=("Accept one Builder patch you have reviewed, then let the Runtime apply it into "
                 "YOUR candidate under a short exclusive boundary and run the combination check. "
                 "Refused when the owner attempt, plan, contract, dependency vector or cancellation "
                 "epoch no longer match, or when the target moved in a conflicting way. Applying is "
                 "not verification and not integration. A failed combination check may follow a successful "
                 "apply; inspect the applied flag and check revision. After all reviewed patches are applied, "
                 "submit_candidate requests final registered checks; do not search for or recreate their harness. "
                 "If rejected for incomplete reads or changed dependencies, recover=true registers an idempotent fresh "
                 "Builder generation with the same assignment, current base and shared budget; it does not apply the old patch. "
                 "Use only a patch_id returned by native results. Candidate preparation failures have no patch; "
                 "do not construct a patch_id from a task_id or use this tool to retry workspace creation."),
    input_schema={"type": "object", "properties": {
        "patch_id": {"type": "string"},
        "recover": {"type": "boolean", "description": "Rebuild a rejected, unmerged patch with new native evidence. Default false."},
        "operation_id": {"type": "string",
                         "description": "Stable id; a repeat returns the recorded result."}},
        "required": ["patch_id"]},
    validator=_accept_validate, run=_accept_run, metadata=_metadata(
        "accept_builder_patch", "Accept and apply one Builder patch into the primary candidate"))

CANCEL_TASK = ToolDefinition(
    name="cancel_task",
    description=("Cancel one Builder task, its created subtree, and optionally its dependency "
                 "successors. The same operation_id is idempotent. Already-running external work "
                 "may still finish and is reported, never claimed to have stopped instantly."),
    input_schema=CANCEL_TASK_INPUT,
    validator=_cancel_validate, run=_cancel_run, metadata=_metadata(
        "cancel_task", "Cancel one Builder task or subtree"))

BUILDER_TOOLS: tuple[ToolDefinition, ...] = (
    SPAWN_BUILDERS, POLL_TASK_RESULTS, WAIT_DEPENDENCIES, ACCEPT_BUILDER_PATCH, CANCEL_TASK,
    SELECT_BUILDER_CANDIDATE)


def builder_tools() -> tuple[ToolDefinition, ...]:
    """The native parallel-candidate tool set, for registration by the Boot layer."""
    return BUILDER_TOOLS


def builder_tool_names() -> tuple[str, ...]:
    return BUILDER_TOOL_NAMES
