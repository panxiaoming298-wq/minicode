"""Governed ``task`` tool.

This module is intentionally unable to import or start AgentLoop.  It emits a
DelegationRequest to the active Runtime; only TaskScheduler may create the
registered child Node/Attempt.
"""

from __future__ import annotations

import hashlib
import json
from typing import TypedDict

from minicode.runtime_protocol import (
    AgentResultStatus,
    BudgetEnvelope,
    DelegationRequest,
    ToolEffect,
)
from minicode.tooling import ToolDefinition, ToolResult


class AgentDef(TypedDict):
    name: str
    description: str
    allowed_effects: tuple[ToolEffect, ...]
    max_turns: int


AGENT_TYPES: dict[str, AgentDef] = {
    "explore": {
        "name": "Explore",
        "description": "Fast, read-only codebase exploration",
        "allowed_effects": (ToolEffect.READ_ONLY,),
        "max_turns": 5,
    },
    "plan": {
        "name": "Plan",
        "description": "Thorough, read-only analysis",
        "allowed_effects": (ToolEffect.READ_ONLY,),
        "max_turns": 8,
    },
    "general": {
        "name": "General",
        "description": "Serial compatibility Worker with workspace tools",
        "allowed_effects": (
            ToolEffect.READ_ONLY,
            ToolEffect.WORKSPACE_WRITE,
            ToolEffect.PRIVILEGED,
        ),
        "max_turns": 15,
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
    definition = AGENT_TYPES[agent_type]
    depth = 2 if parent_node_id.startswith("delegated-") else 1
    canonical = json.dumps(
        {
            "run_id": gateway.run_id,
            "parent_node_id": parent_node_id,
            "agent_type": agent_type,
            "objective": input_data["prompt"],
        },
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
            max_tokens=definition["max_turns"] * 2_000,
            retry_budget=0,
        ),
        delegation_depth=depth,
        idempotency_key=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
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
    return ToolResult(ok=result.status is AgentResultStatus.SUCCEEDED, output=body)


task_tool = ToolDefinition(
    name="task",
    description=(
        "Submit a governed child Worker through the active Runtime Scheduler. "
        "Use 'explore' or 'plan' for read-only work; 'general' is a serial "
        "Standard compatibility Worker. Ultra V1 disables dynamic delegation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short task label"},
            "prompt": {"type": "string", "description": "Full child objective"},
            "agent_type": {
                "type": "string",
                "enum": ["explore", "plan", "general"],
            },
        },
        "required": ["description"],
    },
    validator=_validate,
    run=_run,
)
