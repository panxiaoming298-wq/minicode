"""Bounded request for a Runtime-owned assistant or scoped shared writer."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentDispatch:
    idempotency_key: str
    objective: str
    agent_type: str = "explore"
    context_mode: str = "isolated"
    isolation: str = "shared"
    report_artifact_ids: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    name: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    checks: tuple[dict, ...] = ()
    optional: bool = False
