"""Publish authoritative, bounded execution progress independently of UI sinks."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from minicode.runtime_protocol import ExecutionProfile, TopologyDecision
from minicode.runtime_state import RuntimeReducer

logger = logging.getLogger(__name__)


def collaboration_view(profile: ExecutionProfile | Mapping[str, Any]) -> dict[str, Any] | None:
    """Describe the admitted collaboration policy, never current preferences.

    Historical profiles omit disabled capabilities for hash compatibility. If
    none of the capability fields was recorded, leave the policy unknown. This
    view describes available collaboration; actual delegation is in the nodes.
    """
    fields = {
        "async_agents": "asyncAgents", "agent_teams": "agentTeams",
        "coordinator": "coordinator", "agent_team_writes": "agentTeamWrites",
        "agent_fork": "agentFork",
    }
    if isinstance(profile, ExecutionProfile):
        values = {key: getattr(profile, key) for key in fields}
    else:
        if not any(key in profile or alias in profile for key, alias in fields.items()):
            return None
        values = {key: profile.get(key, profile.get(alias, False)) for key, alias in fields.items()}
        if any(type(value) is not bool for value in values.values()):
            return None
    return {
        "mode": "teams" if values["agent_teams"] else "async" if values["async_agents"] else "off",
        "primaryRole": "coordinator" if values["coordinator"] else "worker",
        "teamWrites": values["agent_team_writes"],
        "fork": values["agent_fork"],
    }


class ActivityPublisher:
    def __init__(self, reducer: RuntimeReducer, profile: ExecutionProfile,
                 topology: TopologyDecision, callback: Any) -> None:
        self.reducer = reducer
        self.profile = profile
        self.topology = topology
        self.callback = callback
        self.previous: dict[str, Any] | None = None

    def publish(self, phase: str = "", *, detail: str = "") -> None:
        if not callable(self.callback):
            return
        snapshot = self.reducer.activity_snapshot()
        nodes = snapshot["nodes"]
        active = [n for n in nodes if n["state"] in {"CLAIMED", "RUNNING", "VERIFYING"}]
        if not phase:
            if any(n["state"] == "WAITING_INPUT" for n in nodes):
                phase = "waiting_input"
            elif any(n["state"] == "WAITING_APPROVAL" for n in nodes):
                phase = "waiting_approval"
            elif any(n["state"] == "FAILED_RETRYABLE" or
                     (n["attemptCount"] - n.get("resumeCredits", 0) > 1 and n in active) for n in nodes):
                phase = "retrying"
            elif any(n["state"] == "WAITING_CHILDREN" for n in nodes):
                phase = "consulting"
            elif any(n["role"] == "verifier" or n["state"] == "VERIFYING" for n in active):
                phase = "verifying"
            elif any(n["role"] in {"explorer", "planner", "reviewer"} for n in active):
                phase = "consulting" if self.profile.primary_led else "exploring"
            elif any(n["role"] == "synthesizer" for n in active):
                phase = "synthesizing"
            elif any(n["role"] == "primary" and self.reducer.consultation_recovery_status(
                n["nodeId"], self.reducer.current_attempt(n["nodeId"]).attempt_id)["allowed"] for n in active):
                phase = "repairing"
                detail = "复核意见已交回主 Agent，正在处理并准备再次复核"
            elif any(n["role"] == "primary" and n.get("repairCount", 0) for n in active):
                phase = "repairing"
            elif active:
                phase = "executing"
            elif any(n["role"] == "primary" and n["state"] == "READY"
                     and self.reducer.continuation_for(n["nodeId"]) is not None for n in nodes):
                phase = "consulting"
                detail = "正在将复核结果交回主 Agent 处理"
            elif any(n["state"] == "FAILED_TERMINAL" and n["nodeId"] in self.reducer.completion_blockers() for n in nodes):
                phase = "failed"
            elif nodes and not self.reducer.completion_blockers():
                phase = "verifying"
            else:
                phase = "preparing"
        snapshot.update({
            "profile": self.profile.requested_mode.value,
            "route": self.profile.selected_route.value,
            "collaboration": collaboration_view(self.profile),
            "phase": phase,
            "routeReasons": list(self.topology.reasons),
        })
        if detail:
            snapshot["detail"] = detail
        previous = self.previous or {}
        snapshot["phaseStartedAt"] = (previous["phaseStartedAt"]
            if previous.get("phase") == phase and previous.get("detail", "") == detail
            else round(time.time() * 1000))
        if self.reducer.continuations_enabled:
            snapshot["consultationMode"] = "durable-batch"
            snapshot["maxActiveWorkers"] = self.profile.max_active_workers
        if self.reducer.repair_limit:
            snapshot["repairCount"] = self.reducer.repair_count
            snapshot["repairLimit"] = self.reducer.repair_limit
        if self.reducer.candidate_first:
            snapshot["workspaceDelivery"] = "candidate-first"
            snapshot["integrationStatus"] = self.reducer.integration_status
        if snapshot == self.previous:
            return
        self.previous = snapshot
        try:
            self.callback(snapshot)
        except Exception:  # UI observers cannot change execution or verification.
            logger.debug("Execution progress observer failed", exc_info=True)
