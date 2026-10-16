"""Reducer-owned collaboration operations; all mutations use its durable lock."""
from copy import deepcopy
import json

from minicode.agent_collaboration import result_notice, validate_config
from minicode.runtime_protocol import NodeState, RuntimeEventType, ToolEffect


class AgentCollaborationState:
    def request_agent_interruption(self, *, parent_id, attempt_id, task_id, operation_id, reason):
        with self._lock:
            self.validate_agent_activation(parent_id, attempt_id)
            value = self._state.agent_collaboration
            assignment = self._state.agent_sessions.get(task_id)
            if value is None or parent_id != "primary" or not assignment or assignment["parent_node_id"] != parent_id:
                raise ValueError("interruption requires the current primary's owned collaboration agent")
            interruptions = value.setdefault("interruptions", {})
            existing = interruptions.get(task_id)
            if existing:
                if existing["operation_id"] != operation_id:
                    raise ValueError("this task already has an interruption request; use its original operation_id")
                return deepcopy(existing)
            if (assignment["definition"]["role"] == "verifier" or task_id not in self._state.agent_checkpoints
                or self._node(task_id).state not in {NodeState.RUNNING, NodeState.WAITING_INPUT, NodeState.WAITING_CHILDREN}
                or self.node_cancel_epoch(task_id)):
                raise ValueError("interrupt requires an active non-verifier with a recoverable checkpoint; cancellation is separate")
            record = {"task_id": task_id, "operation_id": operation_id, "requested_by": attempt_id,
                "cancel_operation_id": f"agent-interrupt:{task_id}:{operation_id}",
                "cancel_epoch": self.node_cancel_epoch(task_id) + 1, "reason": reason}
            interruptions[task_id] = record
            self._emit(RuntimeEventType.AGENT_INTERRUPTION_REQUESTED, deepcopy(record))
            self._persist()
            return deepcopy(record)

    def is_agent_interrupted(self, task_id):
        record = (self._state.agent_collaboration or {}).get("interruptions", {}).get(task_id)
        return bool(record and self._node(task_id).state is NodeState.CANCELED
            and self.node_cancel_epoch(task_id) == record["cancel_epoch"]
            and task_id in self._state.cancel_operations.get(record["cancel_operation_id"], {}).get("canceled_targets", ()))

    def agent_worker_settlement(self, task_id):
        with self._lock:
            attempts = self._node(task_id).attempts
            return deepcopy(self._state.worker_completions.get(attempts[-1].attempt_id)) if attempts else None

    @property
    def collaboration_config(self):
        value = self._state.agent_collaboration
        return deepcopy(value["config"]) if value is not None else None

    @property
    def agent_generation_limit(self):
        return (self.collaboration_config or {}).get("max_generations", 4)

    def validate_agent_budget(self, session_id):
        with self._lock:
            config = self.collaboration_config
            if config is None:
                if len(self._state.nodes) >= self.contract.budget.max_agents:
                    raise ValueError("max_agents budget exhausted")
                return
            if len(self._state.nodes) >= config["max_tasks"]:
                raise ValueError("cumulative agent task budget exhausted; waiting or retiring a member does not reset it")
            sessions = {r["session_id"] for r in self._state.agent_sessions.values()}
            if session_id not in sessions:
                fixed = len(self._state.nodes) - len(self._state.agent_sessions)
                if len(sessions) + fixed >= self.contract.budget.max_agents:
                    raise ValueError("max_agents distinct-session budget exhausted; reuse a related existing session")
                retired = set((self._state.agent_team or {}).get("retired_members", {}))
                if len(sessions - retired) >= config["max_sessions"]:
                    raise ValueError("active team session limit reached; retire an exited member or reuse a session")

    def latest_agent_task(self, handle):
        records = [r for r in self._state.agent_sessions.values() if r["session_id"] == handle]
        return max(records, key=lambda r: r["generation"])["task_id"] if records else handle

    def peer_route_allowed(self, sender, recipient):
        config = self.collaboration_config
        sessions = self._state.agent_sessions
        return bool(config and config["peer_messages"] and sender != recipient
            and sender in sessions and recipient in sessions
            and sessions[sender]["parent_node_id"] == sessions[recipient]["parent_node_id"]
            and all(self._node(key).definition.role != "verifier" for key in (sender, recipient)))

    def agent_communication_allowed(self, node_id):
        node = self._state.nodes.get(node_id)
        return bool(node and node.definition.role != "verifier")

    def agent_message_route_allowed(self, sender, recipient):
        if not all(self.agent_communication_allowed(key) for key in (sender, recipient)):
            return False
        parents = self._state.delegation_parents
        members = ({task for tasks in self._agent_member_tasks().values() for task in tasks}
                   if self._state.agent_team else set(self._state.agent_sessions))
        return bool((sender == "primary" and recipient in members and parents.get(recipient) == sender)
            or (recipient == "primary" and sender in members and parents.get(sender) == recipient)
            or self.peer_route_allowed(sender, recipient))

    def agent_message_recipient_available(self, recipient):
        node = self._node(recipient)
        return bool(not self.node_cancel_epoch(recipient)
            and node.state in {NodeState.PENDING, NodeState.READY, NodeState.CLAIMED, NodeState.RUNNING,
                NodeState.WAITING_INPUT, NodeState.WAITING_APPROVAL, NodeState.WAITING_CHILDREN}
            and (recipient not in self._state.agent_finishing
                or self._state.agent_finishing[recipient] != node.current_attempt_id))

    def agent_dismissal_blocker(self, task_id):
        """Cancel never waives work; only an explicitly optional, settled reader may be dismissed."""
        record = self._state.agent_sessions.get(task_id, {})
        node = self._state.nodes.get(task_id)
        if (record.get("optional") is not True or node is None
            or node.definition.role not in {"explorer", "planner", "reviewer"}
            or node.definition.allowed_effects != (ToolEffect.READ_ONLY,)):
            return "only a predeclared optional read-only assistant may be dismissed"
        if node.state not in {NodeState.CANCELED, NodeState.FAILED_TERMINAL}:
            return "cancel the optional task and wait for worker exit before dismissing it"
        if node.attempts and self.agent_worker_settlement(task_id) is None:
            return "the optional worker has not demonstrably exited"
        if (any(self._state.worker_completions.get(a.attempt_id, {}).get("ambiguous_side_effect") for a in node.attempts)
            or (node.result and node.result.failure and node.result.failure.ambiguous_side_effect)):
            return "an uncertain side effect cannot be dismissed"
        return None

    def dismiss_agent(self, *, parent_id, attempt_id, task_id, operation_id, reason):
        with self._lock:
            self.validate_agent_activation(parent_id, attempt_id)
            value = self._state.agent_collaboration
            assignment = self._state.agent_sessions.get(task_id)
            if value is None or parent_id != "primary" or not assignment or assignment["parent_node_id"] != parent_id:
                raise ValueError("dismissal requires the current primary's owned collaboration assistant")
            if any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in (operation_id, reason)):
                raise ValueError("dismissal requires a bounded operation_id and reason")
            existing = value.get("dismissals", {}).get(task_id)
            if existing:
                if existing["operation_id"] != operation_id or existing["reason"] != reason:
                    raise ValueError("this task already has a dismissal; reuse its original operation_id and reason")
                return deepcopy(existing)
            blocker = self.agent_dismissal_blocker(task_id)
            if blocker:
                raise ValueError(blocker)
            node = self._node(task_id)
            record = {"task_id": task_id, "operation_id": operation_id, "reason": reason,
                "requested_by": attempt_id, "contract_revision": self.current_plan.contract_revision,
                "worker_attempt_id": node.attempts[-1].attempt_id if node.attempts else None,
                "state": node.state.value, "counts_as_acceptance": False}
            value.setdefault("dismissals", {})[task_id] = record
            self._emit(RuntimeEventType.AGENT_TASK_DISMISSED, deepcopy(record))
            self._persist()
            return deepcopy(record)

    def dismissed_agent_ids(self):
        return frozenset((self._state.agent_collaboration or {}).get("dismissals", {}))

    def agent_directory(self, *, node_id, attempt_id, include_history=False, cursor=0):
        with self._lock:
            self.validate_agent_activation(node_id, attempt_id)
            if not self.agent_communication_allowed(node_id):
                raise ValueError("independent verifiers cannot inspect the team directory")
            records = list(self._state.agent_sessions.values())
            if not include_history:
                records = [r for r in records if self.latest_agent_task(r["session_id"]) == r["task_id"]]
            records.sort(key=lambda r: (r["session_id"], r["generation"]))
            items = []
            for record in records[cursor:cursor + 32]:
                node = self._node(record["task_id"])
                checkpoint = self._state.agent_checkpoints.get(record["task_id"], {})
                items.append({"session_id": record["session_id"], "task_id": record["task_id"],
                    "name": record.get("name", ""), "generation": record["generation"],
                    "role": node.definition.role, "state": node.state.value, "objective": node.definition.objective[:240],
                    "write_scope": list(node.definition.write_scope), "model": checkpoint.get("model_identity"),
                    "can_message": self.agent_message_route_allowed(node_id, record["task_id"])
                        and self.agent_message_recipient_available(record["task_id"]),
                    "optional": record.get("optional", False),
                    "dismissed": record["task_id"] in self.dismissed_agent_ids(),
                    "can_dismiss": node_id == "primary" and record["task_id"] not in self.dismissed_agent_ids()
                        and self.agent_dismissal_blocker(record["task_id"]) is None,
                    "result_processing": deepcopy((self._state.agent_collaboration or {}).get("results", {}).get(record["task_id"], {}))})
            return {"agents": items, "total": len(records),
                "next_cursor": cursor + 32 if cursor + 32 < len(records) else None,
                "policy": self.collaboration_config, "counts_as_acceptance": False}

    def agent_task_projection(self, record):
        task_id = record["task_id"]
        node = self._node(task_id)
        base = {"taskId": task_id, "sessionId": record["session_id"], "generation": record["generation"],
            "inputRevision": record["input_revision"], "workspaceMode": record["workspace"]["mode"],
            "state": node.state.value, "objective": node.definition.objective[:1000]}
        origin = record
        while origin.get("predecessor_task_id") or origin.get("replaces_task_id"):
            origin = self._state.agent_sessions[origin.get("predecessor_task_id") or origin["replaces_task_id"]]
        if origin.get("recovery_source"):
            base.update(sessionOriginRunId=origin["recovery_source"]["origin_run_id"],
                        recoveredFromRunId=origin["recovery_source"]["run_id"])
        if self.collaboration_config is None:
            return base
        team = self._state.agent_team or {}
        retired = record["session_id"] in team.get("retired_members", {})
        settlement = self.agent_worker_settlement(task_id)
        interrupted = self.is_agent_interrupted(task_id)
        continuing = bool(self.agent_resume_enabled and (node.state is NodeState.SUCCEEDED or interrupted)
            and task_id not in self.dismissed_agent_ids()
            and node.definition.role != "verifier" and (node.definition.role != "shared_writer" or self.collaboration_config)
            and self.latest_agent_task(record["session_id"]) == task_id
            and record["generation"] < self.agent_generation_limit and settlement and not settlement.get("ambiguous_side_effect")
            and not retired and not self._state.canceled and team.get("state", "active") == "active"
            and self._node("primary").state not in {NodeState.SUCCEEDED, NodeState.CANCELED, NodeState.FAILED_TERMINAL})
        model = self._state.agent_checkpoints.get(task_id, {}).get("model_identity") or {}
        checks = [r for r in (self._state.agent_collaboration or {}).get("checks", {}).values() if r["task_id"] == task_id]
        latest = {item["id"]: item for item in self.agent_check_summaries(task_id)}
        required = {item["id"] for item in record.get("checks", ())} | set(latest)
        check_status = ("failed" if any(item["state"] == "completed" and not item["passed"] for item in latest.values())
                        else "pending" if any(check_id not in latest or latest[check_id]["state"] != "completed" for check_id in required)
                        else "passed" if required else "not_requested")
        return {**base,
            "localCheckStatus": check_status,
            "name": record.get("name", ""), "continuationAvailable": continuing,
            "sessionState": "retired" if retired else "interrupted" if interrupted else "idle" if continuing else "closed" if node.state is NodeState.SUCCEEDED else node.state.value,
            "model": {key: model[key] for key in ("model", "provider") if key in model},
            "writeScope": list(node.definition.write_scope), "acceptanceCriteria": list(node.definition.acceptance_criteria),
            "resultHandling": (self._state.agent_collaboration or {}).get("results", {}).get(task_id, {}).get("state"),
            "optional": record.get("optional", False), "dismissed": task_id in self.dismissed_agent_ids(),
            "dismissalAvailable": task_id not in self.dismissed_agent_ids() and self.agent_dismissal_blocker(task_id) is None,
            "checks": [{"id": r["check"]["id"], "operationId": r["operation_id"], "state": r["state"],
                "passed": r["passed"], "exitCode": r.get("report", {}).get("exit_code")} for r in checks]}

    def pending_agent_notices(self, node_id):
        if node_id != "primary" or self._state.agent_collaboration is None:
            return []
        receipts = self._state.agent_collaboration["results"]
        notices = []
        for task_id, record in self._state.agent_sessions.items():
            node = self._node(task_id)
            if task_id in receipts or node.state not in {
                NodeState.SUCCEEDED, NodeState.CANCELED, NodeState.FAILED_TERMINAL}:
                continue
            settlement = self.agent_worker_settlement(task_id)
            if settlement is None:
                continue
            notice = result_notice(record, node.result, settlement)
            notice["node_state"] = node.state.value
            notice["session_interrupted"] = self.is_agent_interrupted(task_id)
            notice["local_checks"] = self.agent_check_summaries(task_id)
            notices.append({"message_id": "completion:" + task_id, "sender_id": task_id, "recipient_id": "primary",
                "kind": "completion", "runtime_event": "agent_completed", "task_id": task_id,
                "content": json.dumps(notice, ensure_ascii=False)})
            if len(notices) == 8:
                break
        return notices

    def agent_check_summaries(self, task_id):
        latest = {}
        # Serialized maps may be key-sorted; the native event sequence owns time.
        for event in self._state.events:
            if event.event_type is not RuntimeEventType.AGENT_CHECK_RECORDED:
                continue
            record = event.payload
            if record["task_id"] == task_id:
                report = record.get("report", {})
                latest[record["check"]["id"]] = {"id": record["check"]["id"], "operation_id": record["operation_id"],
                    "state": record["state"], "passed": record["passed"],
                    **{key: report[key] for key in ("exit_code", "input_revision", "source_still_matches", "report_artifact") if key in report}}
        return list(latest.values())

    def record_agent_notices(self, notices, checkpoint, attempt_id):
        for notice in notices:
            receipt = {"state": "delivered", "delivered_attempt_id": attempt_id,
                "checkpoint": deepcopy(checkpoint["artifact"])}
            self._state.agent_collaboration["results"][notice["task_id"]] = receipt
            self._emit(RuntimeEventType.AGENT_RESULT_DELIVERED, {"task_id": notice["task_id"], **deepcopy(receipt)})

    def acknowledge_agent_results(self, *, node_id, attempt_id, task_ids, note):
        with self._lock:
            self.validate_agent_activation(node_id, attempt_id)
            if node_id != "primary" or self._state.agent_collaboration is None:
                raise ValueError("only the live primary may acknowledge collaboration results")
            receipts = self._state.agent_collaboration["results"]
            if any(task not in receipts for task in task_ids):
                raise ValueError("results must first be delivered into the primary's checkpointed context")
            for task_id in task_ids:
                if receipts[task_id]["state"] == "handled":
                    continue
                receipts[task_id].update(state="handled", handled_attempt_id=attempt_id, note=note)
                self._emit(RuntimeEventType.AGENT_RESULT_HANDLED, {"task_id": task_id, **deepcopy(receipts[task_id])})
            self._persist()
            return {"handled": list(task_ids), "counts_as_acceptance": False}

    def validate_collaboration_state(self):
        value = self._state.agent_collaboration
        if value is None:
            return
        if (not self.agent_messages_enabled or not isinstance(value, dict)
            or set(value) - {"config", "results", "checks", "interruptions", "dismissals"} or not isinstance(value.get("results"), dict)
            or not isinstance(value["checks"], dict) or not isinstance(value.get("dismissals", {}), dict)):
            raise ValueError("invalid persisted collaboration state")
        validate_config(value["config"])
        delivered, checked, interrupted, dismissed = {}, {}, {}, {}
        for event in self._state.events:
            if event.event_type in {RuntimeEventType.AGENT_RESULT_DELIVERED, RuntimeEventType.AGENT_RESULT_HANDLED}:
                delivered[event.payload["task_id"]] = {key: item for key, item in event.payload.items() if key != "task_id"}
            elif event.event_type is RuntimeEventType.AGENT_CHECK_RECORDED:
                checked[f"{event.payload['task_id']}:{event.payload['operation_id']}"] = event.payload
            elif event.event_type is RuntimeEventType.AGENT_INTERRUPTION_REQUESTED:
                interrupted[event.payload["task_id"]] = event.payload
            elif event.event_type is RuntimeEventType.AGENT_TASK_DISMISSED:
                dismissed[event.payload["task_id"]] = event.payload
            elif event.event_type is RuntimeEventType.NODE_READY and event.payload.get("agent_session"):
                assignment = event.payload["agent_session"]
                previous = assignment.get("predecessor_task_id") or assignment.get("replaces_task_id")
                if previous in dismissed:
                    raise ValueError("a dismissed agent cannot receive a later continuation")
        if (delivered != value["results"] or checked != value["checks"] or interrupted != value.get("interruptions", {})
            or dismissed != value.get("dismissals", {})):
            raise ValueError("collaboration state must retain its exact native events")
        for task_id, record in dismissed.items():
            node = self._state.nodes.get(task_id)
            if (self.agent_dismissal_blocker(task_id) is not None
                or record.get("worker_attempt_id") != (node.attempts[-1].attempt_id if node.attempts else None)
                or record.get("state") != node.state.value or record.get("counts_as_acceptance") is not False
                or record.get("contract_revision") != self.current_plan.contract_revision
                or not any(a.attempt_id == record.get("requested_by") for a in self._node("primary").attempts)
                or any(not isinstance(record.get(key), str) or not record[key].strip() or len(record[key]) > 1000
                       for key in ("operation_id", "reason"))):
                raise ValueError("invalid optional agent dismissal or unsettled worker")
        for task_id, record in value.get("interruptions", {}).items():
            if (record.get("task_id") != task_id or task_id not in self._state.agent_sessions
                or not any(e.event_type is RuntimeEventType.AGENT_INTERRUPTION_REQUESTED and e.payload == record for e in self._state.events)):
                raise ValueError("agent interruption lacks its durable request")
        for key, record in value["checks"].items():
            if (record.get("task_id") not in self._state.agent_sessions
                or key != f"{record['task_id']}:{record.get('operation_id')}"
                or record.get("state") not in {"running", "completed"}
                or not any(e.event_type is RuntimeEventType.AGENT_CHECK_RECORDED and e.payload == record for e in self._state.events)):
                raise ValueError("invalid persisted local check receipt")
        for task_id, receipt in value["results"].items():
            if task_id not in self._state.agent_sessions or receipt.get("state") not in {"delivered", "handled"}:
                raise ValueError("invalid agent result receipt")
            event_type = RuntimeEventType.AGENT_RESULT_HANDLED if receipt["state"] == "handled" else RuntimeEventType.AGENT_RESULT_DELIVERED
            if not any(e.event_type is event_type and e.payload == {"task_id": task_id, **receipt} for e in self._state.events):
                raise ValueError("agent result receipt lacks its durable event")

    def agent_check_activation_live(self, node_id, attempt_id):
        with self._lock:
            try:
                self.validate_agent_activation(node_id, attempt_id)
                return True
            except (ValueError, RuntimeError, KeyError):
                return False

    def claim_agent_check(self, *, node_id, attempt_id, check_id, operation_id):
        with self._lock:
            self.validate_agent_activation(node_id, attempt_id)
            value = self._state.agent_collaboration
            assignment = self._state.agent_sessions.get(node_id, {})
            if value is None or self._node(node_id).definition.role != "shared_writer":
                raise ValueError("local checks require a collaboration-enabled scoped writer")
            check = next((item for item in assignment.get("checks", ()) if item["id"] == check_id), None)
            if check is None:
                raise ValueError("check_id was not declared by the primary for this task")
            key = f"{node_id}:{operation_id}"
            if key in value["checks"]:
                existing = value["checks"][key]
                if existing["check"] != check:
                    raise ValueError("local check operation_id already identifies another check")
                return {"reused": True, "record": deepcopy(existing)}
            if sum(r["task_id"] == node_id for r in value["checks"].values()) >= 32:
                raise ValueError("local check invocation budget exhausted for this task")
            record = {"task_id": node_id, "attempt_id": attempt_id, "operation_id": operation_id,
                "check": deepcopy(check), "state": "running", "passed": False,
                "notice": "Unsettled checks are never replayed or treated as passing after process loss."}
            value["checks"][key] = record
            self._emit(RuntimeEventType.AGENT_CHECK_RECORDED, deepcopy(record))
            self._persist()
            return {"reused": False, "record": deepcopy(record)}

    def finish_agent_check(self, *, node_id, operation_id, report):
        with self._lock:
            record = self._state.agent_collaboration["checks"][f"{node_id}:{operation_id}"]
            live = self.agent_check_activation_live(node_id, record["attempt_id"])
            record.update(state="completed", report=deepcopy(report), passed=bool(report["passed"] and live))
            if not live:
                record["notice"] = "The activation ended; this is historical evidence, not current success."
            self._emit(RuntimeEventType.AGENT_CHECK_RECORDED, deepcopy(record))
            self._persist()
            return deepcopy(record)
