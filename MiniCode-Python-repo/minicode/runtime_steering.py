"""Durable human directives for a live task, separate from Agent mailboxes.

The original admission contract, permissions and budget stay immutable. An
explicit human directive may project new task requirements; its own revision
and canceled scopes are retained with the Runtime. Only the public Worker can
consume or resolve these directives. Text from helpers never has this authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import PureWindowsPath
import re
from typing import Any

from minicode.runtime_protocol import NodeState, ReplayPolicy, RuntimeEventType, ToolEffect
from minicode.runtime_messages import runtime_context_message
from minicode.tooling import ToolContext, ToolDefinition, ToolMetadata, ToolResult

POLICY = "human-runtime-steering-v1"
RESOLVE_INPUT = "resolve_user_input"
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
_FIELDS = frozenset({"objective", "constraints", "acceptance_criteria", "expected_outputs"})


def initial_state(node_id: str) -> dict:
    return {"policy": POLICY, "node_id": node_id, "revision": 0, "inputs": {},
            "checkpoint": None, "amendments": [], "effective_fields": {},
            "superseded_nodes": [], "sealed": False}


def validate_state(value: Any, *, run_id: str, nodes: dict, events=(), parents=None) -> None:
    """Validate a versioned snapshot before allowing its projected authority."""
    from minicode.runtime_state import ReducerError
    if (not isinstance(value, dict) or value.get("policy") != POLICY
        or value.get("node_id") not in nodes or type(value.get("revision")) is not int
        or value["revision"] < 0 or not isinstance(value.get("inputs"), dict)
        or len(value["inputs"]) > 128 or type(value.get("sealed")) is not bool
        or not isinstance(value.get("amendments"), list)
        or not isinstance(value.get("effective_fields"), dict)
        or set(value["effective_fields"]) - _FIELDS
        or not isinstance(value.get("superseded_nodes"), list)):
        raise ReducerError("invalid human steering state")
    sequences = []
    from minicode.runtime_artifacts import RuntimeArtifactStore
    from minicode.runtime_protocol import ArtifactRef
    event_payloads = {}
    for event in events:
        event_payloads.setdefault(event.event_type, []).append(event.payload)
    attempts = {attempt.attempt_id for attempt in nodes[value["node_id"]].attempts}
    for key, record in value["inputs"].items():
        if (not isinstance(key, str) or not _IDENTITY.fullmatch(key) or not isinstance(record, dict)
            or record.get("inputId") != key or record.get("run_id") != run_id
            or record.get("status") not in {"accepted", "applied", "completed", "canceled", "held"}
            or not isinstance(record.get("text"), str) or not 1 <= len(record["text"]) <= 60000
            or record.get("content_hash") != hashlib.sha256(record["text"].encode()).hexdigest()
            or type(record.get("sequence")) is not int or record["sequence"] <= 0):
            raise ReducerError("invalid human steering input identity or content")
        sequences.append(record["sequence"])
        if not any(p.get("inputId") == key and p.get("content_hash") == record["content_hash"]
                   and p.get("sequence") == record["sequence"]
                   for p in event_payloads.get(RuntimeEventType.USER_INPUT_QUEUED, [])):
            raise ReducerError("human input lacks its durable acceptance event")
        if record["status"] in {"applied", "completed"}:
            raw_ref = record.get("checkpoint")
            if (not isinstance(raw_ref, dict) or record.get("consumed_attempt_id") not in attempts
                or not RuntimeArtifactStore.owns_reference(ArtifactRef(**raw_ref),
                    run_id=run_id, node_id=value["node_id"])
                or not any(p.get("inputId") == key and p.get("checkpoint") == raw_ref
                           and p.get("attempt_id") == record["consumed_attempt_id"]
                           for p in event_payloads.get(RuntimeEventType.USER_INPUT_CONSUMED, []))):
                raise ReducerError("consumed human input requires its owned durable checkpoint")
        if record["status"] == "completed":
            resolution = validate_resolution(record.get("resolution"))
            if (resolution != record["resolution"] or record.get("resolved_attempt_id") not in attempts
                or not any(p.get("inputId") == key and p.get("resolution") == resolution
                    and p.get("task_revision") == record.get("task_revision")
                    for p in event_payloads.get(RuntimeEventType.USER_INPUT_RESOLVED, []))):
                raise ReducerError("human input lacks its authoritative resolution")
    if len(set(sequences)) != len(sequences) or value["revision"] != len(value["amendments"]):
        raise ReducerError("invalid human steering revision or sequence")
    fields, superseded = {}, set()
    for revision, amendment in enumerate(value["amendments"], 1):
        record = value["inputs"].get(amendment.get("inputId"), {})
        if (amendment.get("revision") != revision or record.get("status") != "completed"
            or record.get("resolution", {}).get("action") != "amend"
            or amendment.get("changes") != record["resolution"].get("changes")):
            raise ReducerError("task amendment requires its resolved human instruction")
        if _validate_changes(amendment["changes"]) != amendment["changes"]:
            raise ReducerError("task requirements must use canonical bounded values")
        fields.update(amendment["changes"])
        for node_id in amendment.get("superseded_nodes", []):
            if node_id not in nodes or node_id == value["node_id"] or nodes[node_id].state not in {
                NodeState.CANCELED, NodeState.SUPERSEDED, NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL}:
                raise ReducerError("amended task retains an active obsolete scope")
            superseded.add(node_id)
            owner = (parents or {}).get(node_id)
            seen = set()
            while owner is not None and owner != value["node_id"] and owner not in seen:
                seen.add(owner)
                owner = (parents or {}).get(owner)
            if owner != value["node_id"]:
                raise ReducerError("human amendment supersedes an unrelated task")
    if value["effective_fields"] != fields or set(value["superseded_nodes"]) != superseded:
        raise ReducerError("effective task requirements differ from their human amendment log")
    if value["sealed"] and (any(r["status"] in {"accepted", "applied"} for r in value["inputs"].values())
        or not any(p.get("node_id") == value["node_id"] and p.get("task_revision") == value["revision"]
                   for p in event_payloads.get(RuntimeEventType.USER_INPUT_SEALED, []))):
        raise ReducerError("human input seal is incomplete")


def _validate_changes(changes: Any) -> dict:
    if not isinstance(changes, dict) or not changes or set(changes) - _FIELDS:
        raise ValueError("amend requires bounded task requirements; permissions and budgets cannot change")
    checked = {}
    for field, value in changes.items():
        if field == "objective":
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 12000:
                raise ValueError("objective must contain 1 to 12000 characters")
            checked[field] = value.strip()
        else:
            if (not isinstance(value, list) or len(value) > 32
                or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 2000 for item in value)):
                raise ValueError(field + " must be a bounded list of requirements")
            checked[field] = [item.strip() for item in value]
            if field == "expected_outputs":
                # These are literal files consumed by verify_expected_outputs,
                # not prose acceptance criteria. Reject unsafe/ambiguous values
                # before changing the task or canceling any owned work.
                for path in checked[field]:
                    parts = path.replace("\\", "/").split("/")
                    if (PureWindowsPath(path).drive or path.startswith(("/", "\\"))
                        or re.search(r'[\x00-\x1f<>:"|?*：]', path)
                        or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)):
                        raise ValueError("expected_outputs must be literal relative workspace file paths, e.g. "
                            "['revised.txt']; put file content requirements in constraints, not in the file path")
    return checked


def effective_contract(contract, state):
    if contract is None or not state:
        return contract
    fields = {key: tuple(value) for key, value in state["effective_fields"].items() if key != "objective"}
    if "objective" in state["effective_fields"]:
        fields["envelope"] = replace(contract.envelope, request=state["effective_fields"]["objective"])
    return replace(contract, **fields) if fields else contract


def effective_plan(plan, state):
    if not state or not state["amendments"]:
        return plan
    fields, obsolete = state["effective_fields"], set(state["superseded_nodes"])
    nodes = []
    for node in plan.nodes:
        if node.node_id in obsolete:
            continue
        changes = {"dependencies": tuple(key for key in node.dependencies if key not in obsolete)}
        if node.node_id == state["node_id"]:
            if "objective" in fields:
                changes["objective"] = fields["objective"]
            if "acceptance_criteria" in fields:
                changes["acceptance_criteria"] = tuple(fields["acceptance_criteria"])
        elif node.role == "verifier":
            if "acceptance_criteria" in fields:
                changes["acceptance_criteria"] = tuple(fields["acceptance_criteria"])
            if "objective" in fields:
                changes["objective"] = "Independently verify the current human-amended task: " + fields["objective"]
        nodes.append(replace(node, **changes))
    return replace(plan, nodes=tuple(nodes))


class HumanInputState:
    """Operations are serialized and persisted by the existing sole Reducer."""
    def __init__(self, reducer):
        self.reducer = reducer

    @property
    def state(self):
        value = self.reducer._state.user_steering
        if value is None:
            raise ValueError("human steering is not bound to this Runtime")
        return value

    def _live(self, attempt_id=None):
        from minicode.runtime_state import ReducerError
        state, reducer = self.state, self.reducer
        node = reducer._node(state["node_id"])
        if reducer._state.canceled or reducer._state.paused or state["sealed"] or reducer.terminal():
            raise ReducerError("this task has closed human input")
        if attempt_id is not None:
            attempt = reducer.current_attempt(node.definition.node_id)
            if (node.state is not NodeState.RUNNING or attempt is None or attempt.attempt_id != attempt_id
                or not attempt.lease.is_valid(reducer._clock.now())):
                raise ReducerError("human input requires the live public Worker activation")

    def queue(self, item: dict) -> dict:
        with self.reducer._lock:
            input_id, text = item.get("inputId"), item.get("text")
            if (not isinstance(input_id, str) or not _IDENTITY.fullmatch(input_id)
                or not isinstance(text, str) or not 1 <= len(text.strip()) <= 60000):
                raise ValueError("human input needs a bounded identity and 1 to 60000 characters")
            text = text.strip()
            existing = self.state["inputs"].get(input_id)
            if existing is not None:
                if existing["text"] != text:
                    raise ValueError("human input id conflicts with its original content")
                return deepcopy(existing)
            self._live()
            records = self.state["inputs"]
            if len(records) >= 128 or sum(r["status"] in {"accepted", "applied"} for r in records.values()) >= 8:
                raise ValueError("human input quota exhausted; wait for consumption")
            record = {"inputId": input_id, "text": text, "kind": "steer", "run_id": self.reducer.run_id,
                "executionProfile": item.get("executionProfile", "standard"), "status": "accepted",
                "sequence": len(records) + 1, "content_hash": hashlib.sha256(text.encode()).hexdigest()}
            records[input_id] = record
            self.reducer._emit(RuntimeEventType.USER_INPUT_QUEUED, deepcopy(record))
            self.reducer._persist()
            return deepcopy(record)

    def cancel(self, input_id: str) -> dict:
        with self.reducer._lock:
            record = self.state["inputs"].get(input_id)
            if record is None:
                raise ValueError("human input does not exist")
            if record["status"] not in {"accepted", "held", "canceled"}:
                raise ValueError("human input is already in context and cannot be withdrawn")
            record["status"] = "canceled"
            self.reducer._emit(RuntimeEventType.USER_INPUT_CANCELED, {"inputId": input_id})
            self.reducer._persist()
            return deepcopy(record)

    def pending(self) -> list[dict]:
        with self.reducer._lock:
            return deepcopy([r for r in self.state["inputs"].values() if r["status"] == "accepted"])

    def unresolved(self) -> list[dict]:
        with self.reducer._lock:
            return deepcopy([r for r in self.state["inputs"].values() if r["status"] == "applied"])

    def consume(self, attempt_id: str, make_checkpoint) -> list[dict]:
        from minicode.runtime_artifacts import RuntimeArtifactStore
        from minicode.runtime_protocol import ArtifactRef
        with self.reducer._lock:
            self._live(attempt_id)
            # Resolve one directive before advancing to the next. This keeps
            # context bounded and preserves reception order under compaction.
            pending = self.pending()[:1] if not self.unresolved() else []
            if not pending:
                return []
            ref = make_checkpoint(pending)
            if not isinstance(ref, ArtifactRef) or not RuntimeArtifactStore.owns_reference(ref,
                run_id=self.reducer.run_id, node_id=self.state["node_id"]):
                raise ValueError("human input consumption requires its owned context checkpoint")
            self.state["checkpoint"] = ref.to_dict()
            for item in pending:
                record = self.state["inputs"][item["inputId"]]
                record.update(status="applied", consumed_attempt_id=attempt_id, checkpoint=ref.to_dict())
                self.reducer._emit(RuntimeEventType.USER_INPUT_CONSUMED,
                    {"inputId": item["inputId"], "attempt_id": attempt_id, "checkpoint": ref.to_dict()})
            self.reducer._persist()
            return deepcopy([self.state["inputs"][r["inputId"]] for r in pending])

    def preflight(self, attempt_id: str, value: dict) -> dict:
        """Validate native authority before cancellation or other amendment effects."""
        value = validate_resolution(value)
        with self.reducer._lock:
            self._live(attempt_id)
            record = self.state["inputs"].get(value["input_id"])
            if record is None or record["status"] not in {"applied", "completed"}:
                raise ValueError("only a consumed human input can change task requirements")
            if record["status"] == "completed":
                # The reply wording is an acknowledgement, not a new decision.
                # An equivalent retry returns the original immutable receipt.
                original = record["resolution"]
                if ({k: v for k, v in original.items() if k != "response"}
                        != {k: v for k, v in value.items() if k != "response"}):
                    raise ValueError("human input resolution conflicts with its original decision")
                return deepcopy(original)
            if self.unresolved()[0]["inputId"] != record["inputId"]:
                raise ValueError("resolve human inputs in their acceptance order")
            if value["action"] == "amend":
                changes = _validate_changes(value.get("changes"))
                if ("acceptance_criteria" in changes
                    and tuple(changes["acceptance_criteria"]) != self.reducer.contract.acceptance_criteria
                    and not any(n.node_id == "final-verifier" and n.role == "verifier"
                                for n in self.reducer.current_plan.nodes)):
                    raise ValueError("This route has no independent final verifier for new free-text acceptance_criteria. "
                        "Express the changed task in objective, constraints and expected_outputs; preserve existing "
                        "registered criteria. No amendment has been applied.")
            return value

    def resolve(self, attempt_id: str, value: dict, *, superseded=()) -> dict:
        with self.reducer._lock:
            value = self.preflight(attempt_id, value)
            record = self.state["inputs"][value["input_id"]]
            if record["status"] == "completed":
                return deepcopy(record)
            if value["action"] == "amend":
                changes = value["changes"]
                for node_id in superseded:
                    if (node_id == self.state["node_id"] or self.reducer.node_state(node_id) not in {
                        NodeState.CANCELED, NodeState.SUPERSEDED, NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL}):
                        raise ValueError("obsolete child work must stop before the task amendment is applied")
                    if self.state["node_id"] not in self.reducer.creation_ancestors(node_id):
                        raise ValueError("human input cannot supersede another owner's task")
                self.state["revision"] += 1
                self.state["amendments"].append({"inputId": record["inputId"], "revision": self.state["revision"],
                    "changes": changes, "superseded_nodes": sorted(set(superseded)), "attempt_id": attempt_id})
                self.state["effective_fields"].update(changes)
                self.state["superseded_nodes"] = sorted(set(self.state["superseded_nodes"]) | set(superseded))
                # Prior verifier decisions do not certify amended requirements.
                self.reducer._state.final_verification_records.clear()
            record.update(status="completed", resolution=deepcopy(value), resolved_attempt_id=attempt_id,
                          task_revision=self.state["revision"])
            self.reducer._emit(RuntimeEventType.USER_INPUT_RESOLVED,
                {"inputId": record["inputId"], "resolution": deepcopy(value), "task_revision": self.state["revision"]})
            self.reducer._persist()
            return deepcopy(record)

    def seal(self, attempt_id: str) -> bool:
        with self.reducer._lock:
            self._live(attempt_id)
            if self.pending() or self.unresolved():
                return False
            self.state["sealed"] = True
            self.reducer._emit(RuntimeEventType.USER_INPUT_SEALED,
                {"node_id": self.state["node_id"], "attempt_id": attempt_id, "task_revision": self.state["revision"]})
            self.reducer._persist()
            return True


def validate_resolution(value: Any) -> dict:
    if (not isinstance(value, dict) or set(value) - {"input_id", "action", "response", "changes", "affected_task_ids"}
        or not isinstance(value.get("input_id"), str) or not _IDENTITY.fullmatch(value["input_id"])
        or value.get("action") not in {"continue", "amend"}
        or not isinstance(value.get("response"), str) or not 1 <= len(value["response"].strip()) <= 4000):
        raise ValueError("resolve_user_input needs input_id, continue/amend, and a bounded truthful response")
    result = {"input_id": value["input_id"], "action": value["action"], "response": value["response"].strip()}
    if value["action"] == "amend":
        result["changes"] = _validate_changes(value.get("changes"))
        affected = value.get("affected_task_ids")
        if affected is not None:
            if (not isinstance(affected, list) or len(affected) > 32
                or any(not isinstance(key, str) or not _IDENTITY.fullmatch(key) for key in affected)):
                raise ValueError("affected_task_ids must contain bounded current child identities")
            result["affected_task_ids"] = list(dict.fromkeys(affected))
    elif value.get("changes") or value.get("affected_task_ids"):
        raise ValueError("continue cannot amend task requirements or cancel child work")
    return result


class WorkerSteering:
    """One public activation consumes directives at complete execution boundaries."""
    def __init__(self, *, reducer, scheduler, worker, invocation, transport, callbacks):
        self.reducer, self.scheduler, self.worker = reducer, scheduler, worker
        self.invocation, self.transport, self.callbacks = invocation, transport, callbacks
        self.inbox = HumanInputState(reducer)
        self.messages = None
        self.context_manager = None
        self._resolution_in_batch = False

    def boundary(self, messages, *, context_manager=None, final=False, response=None, on_event=None, step=None):
        self.messages, self.context_manager = messages, context_manager
        # AgentLoop enters only after the preceding tool batch has joined.
        # Its next proposal may now use the newly resolved requirements.
        self._resolution_in_batch = False
        if self.inbox.state["sealed"]:
            return {"consumed": 0, "sealed": True}
        self.transport.poll_user_inputs(self.invocation, final=final and not self.inbox.unresolved())
        if response is not None and response.calls and self.inbox.pending() and not self.inbox.unresolved():
            from minicode.agent_loop import _refused_model_tool_messages
            messages.extend(_refused_model_tool_messages(response, on_event=on_event, step=step,
                reason="human_steered", detail="new human context arrived; reconsider these unstarted calls"))
        def checkpoint(pending):
            from minicode.working_memory import snapshot_working_state
            proposed = [*deepcopy(messages), *({"role": "user", "content": record["text"],
                "_userInputId": record["inputId"]} for record in pending)]
            # Instructions are replaceable state, not durable commands. Keeping
            # a completed input's command in history reactivates it on rotation.
            self._refresh_context(proposed, unresolved=pending)
            payload = {"schema": "human-context-v1", "run_id": self.invocation.run_id,
                "node_id": self.invocation.node_id, "attempt_id": self.invocation.attempt_id,
                "task_revision": self.inbox.state["revision"], "messages": proposed,
                "context_state": {"working_memory": snapshot_working_state(),
                    "compaction_history": deepcopy(getattr(context_manager, "compaction_history", []))}}
            ref = self.worker.artifact_store.put_text(json.dumps(payload, ensure_ascii=False, allow_nan=False),
                run_id=self.invocation.run_id, node_id=self.invocation.node_id,
                media_type="application/vnd.minicode.human-context+json")
            messages[:] = proposed
            return ref
        consumed = self.inbox.consume(self.invocation.attempt_id, checkpoint)
        for record in consumed:
            self.transport.publish_user_input(record)
        if final and not consumed and self.inbox.unresolved():
            self.refresh_context(messages)
            return {"consumed": 1, "sealed": False}
        if final and not consumed:
            return {"consumed": 0, "sealed": self.inbox.seal(self.invocation.attempt_id)}
        return {"consumed": len(consumed), "sealed": False}

    def refresh_context(self, messages):
        """Rebuild owned task state after compaction, before provider selection."""
        self._refresh_context(messages, unresolved=self.inbox.unresolved())

    def model_tools(self, registry):
        """Offer the control that can actually run while a directive is open.

        This is only a provider view. The complete executor registry and its
        live guard remain authoritative if a provider proposes a hidden tool.
        """
        if not self.inbox.unresolved():
            return registry
        return registry.filtered_without_names(frozenset(registry.list_all()) - {RESOLVE_INPUT},
                                               _reason="human_steering")

    def _refresh_context(self, messages, *, unresolved):
        from minicode.runtime_messages import runtime_context_kind
        had_snapshot = any(runtime_context_kind(m) == "human_steering" for m in messages)
        messages[:] = [m for m in messages if runtime_context_kind(m) != "human_steering"]
        if not self.inbox.state["inputs"] and not had_snapshot:
            return
        for record in unresolved:
            if not any(m.get("_userInputId") == record["inputId"] for m in messages):
                messages.append({"role": "user", "content": record["text"], "_userInputId": record["inputId"]})
        original = self.invocation.objective
        requirements = self.inbox.state["effective_fields"]
        goal = requirements.get("objective", original)
        messages.append(runtime_context_message(
            "Active task after human steering (intermediate replies do not end this task): "
            + json.dumps({"task_revision": self.inbox.state["revision"],
                "objective": goal[:12000], "objective_has_more_in_original_assignment": len(goal) > 12000,
                "updated_requirements": requirements,
                "unresolved_input_ids": [r["inputId"] for r in unresolved]}, ensure_ascii=False)
            + ". This latest native state is the sole source of actionable input IDs. Earlier steering notices "
              "and tool errors describe history, not additional pending work. "
            + ("Resolve the listed inputs in order with resolve_user_input. Use continue for progress replies or "
               "execution advice; use amend for changed objective/scope with complete updated affected fields. "
               "Preserve unaffected requirements and formal acceptance_criteria unless a native final-verifier exists. "
               "expected_outputs contains literal relative file paths; put content requirements in constraints. "
               if unresolved else
               "No user inputs need resolution. Do not call resolve_user_input for historical IDs; continue the active task. ")
            + "Intermediate replies do not end this task. "
              "Original permissions, required process and registered checks remain in force.", kind="human_steering"))

    def tool_guard(self, tool_name: str) -> str:
        if tool_name == RESOLVE_INPUT:
            return ""
        if not self.inbox.state["sealed"]:
            self.transport.poll_user_inputs(self.invocation, final=False)
        if self.inbox.pending():
            return "UserSteeringPending: a human update arrived; this unstarted call was not executed. Reconsider at the next boundary."
        if self.inbox.unresolved():
            return "UserSteeringUnresolved: address the consumed input with resolve_user_input before continuing tools."
        if self._resolution_in_batch:
            return ("UserSteeringResolved: this batch resolved a human input; its remaining ordinary calls "
                    "were proposed before that resolution and were not executed. Reconsider at the next boundary.")
        return ""

    def handoff(self, context=None):
        from minicode.workspace_provider import WorkspaceConflict
        if self.inbox.state["sealed"]:
            return
        self.transport.poll_user_inputs(self.invocation, final=True)
        if not self.inbox.seal(self.invocation.attempt_id):
            raise WorkspaceConflict("accepted human input must be processed before this task can finish")

    def tool(self):
        return ToolDefinition(name=RESOLVE_INPUT,
            description="Respond to a received human input while continuing the current task. continue answers progress questions or applies execution advice. amend projects updated objective/constraints/expected_outputs and stops affected owned helpers. acceptance_criteria are formal independently verified conditions; preserve them unless the plan has a native final-verifier. Does not reset budget, registered checks or permissions. Never invent input IDs.",
            input_schema={"type": "object", "properties": {
                "input_id": {"type": "string"}, "action": {"type": "string", "enum": ["continue", "amend"]},
                "response": {"type": "string", "maxLength": 4000},
                "changes": {"type": "object", "properties": {"objective": {"type": "string"},
                    **{key: {"type": "array", "items": {"type": "string"}, "maxItems": 32}
                       for key in ("constraints", "acceptance_criteria")},
                    "expected_outputs": {"type": "array", "items": {"type": "string"}, "maxItems": 32,
                        "description": "Only literal relative workspace file paths, e.g. ['revised.txt']. No content description or prose; place those requirements in constraints."}}, "additionalProperties": False},
                "affected_task_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 32}},
                "required": ["input_id", "action", "response"], "additionalProperties": False},
            validator=validate_resolution, run=self._resolve,
            metadata=ToolMetadata(name=RESOLVE_INPUT, description="Resolve a live human directive",
                                  effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.SAFE))

    def _resolve(self, value, context: ToolContext):
        from minicode.runtime_state import ReducerError
        from minicode.turn_events import TurnEvent
        try:
            if context.node_id != self.invocation.node_id or context.attempt_id != self.invocation.attempt_id:
                raise ValueError("human directive tool is bound to its public Worker activation")
            value = self.inbox.preflight(self.invocation.attempt_id, value)
            # Set before changing authoritative state: even a call scheduled
            # immediately after resolution must wait for a fresh proposal.
            self._resolution_in_batch = True
            if self.inbox.state["inputs"][value["input_id"]]["status"] == "completed":
                return ToolResult(ok=True, output=json.dumps({"status": "already_resolved",
                    "resolution": value, "task_revision": self.inbox.state["revision"],
                    "unresolved_input_ids": [r["inputId"] for r in self.inbox.unresolved()],
                    "continue_original_task": True}, ensure_ascii=False))
            superseded = []
            if value["action"] == "amend":
                records = self.inbox.state["inputs"]
                if records.get(value["input_id"], {}).get("status") != "applied":
                    if records.get(value["input_id"], {}).get("resolution") == value:
                        return ToolResult(ok=True, output=json.dumps({"status": "already_resolved"}))
                    raise ValueError("amendment requires this consumed human instruction")
                parents = self.reducer.snapshot().get("delegation_parents", {})
                owned = {node for node, parent in parents.items() if parent == self.invocation.node_id}
                targets = set(value.get("affected_task_ids", owned))
                if not targets <= owned:
                    raise ValueError("only the public Worker's owned child tasks can be superseded")
                for task_id in sorted(targets):
                    receipt = self.scheduler.cancel_task(task_id=task_id, subtree=True,
                        operation_id="user-amend:" + value["input_id"] + ":" + task_id,
                        reason="human changed current task requirements")
                    superseded.extend(receipt["canceled_node_ids"] or [task_id])
                with self.scheduler._dispatch_lock:
                    active_futures = [future for future, (_node, inv, _token) in
                        list((getattr(self.scheduler, "_active_futures", None) or {}).items())
                        if inv.node_id in superseded and not future.done()]
                if active_futures:
                    from concurrent.futures import wait
                    _, active = wait(active_futures, timeout=5)
                    if active:
                        return ToolResult(ok=False, output="UserAmendmentWaiting: cancellation was requested; owned work has not exited yet. Retry resolve_user_input with the same input and decision. No requirement change has been applied.")
            record = self.inbox.resolve(self.invocation.attempt_id, value, superseded=superseded)
            self.transport.publish_user_input(record)
            if self.callbacks is not None and hasattr(self.callbacks, "on_event"):
                self.callbacks.on_event(TurnEvent(kind="assistant", content=value["response"],
                    phase="commentary", message_id="user-response-" + record["inputId"],
                    node_id=self.invocation.node_id, attempt_id=self.invocation.attempt_id))
            return ToolResult(ok=True, output=json.dumps({"status": "resolved", "task_revision": record["task_revision"],
                "response": value["response"], "continue_original_task": True,
                "current_requirements": self.inbox.state["effective_fields"],
                "superseded_task_ids": superseded,
                "notice": "Completed file/command effects are not automatically undone; inspect and repair any obsolete changes."}, ensure_ascii=False))
        except (ValueError, ReducerError) as error:
            record = self.inbox.state["inputs"].get(value.get("input_id")) if isinstance(value, dict) else None
            state = {"input_status": record.get("status") if record else "unknown",
                     "task_revision": self.inbox.state["revision"],
                     "unresolved_input_ids": [r["inputId"] for r in self.inbox.unresolved()]}
            return ToolResult(ok=False, output="UserInputResolutionDenied: " + str(error)
                + "\nCurrent input state: " + json.dumps(state, ensure_ascii=False)
                + ". Completed decisions are unchanged. Resolve only currently unresolved IDs.")
