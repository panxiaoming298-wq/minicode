"""Provider-neutral proposal for a bounded, read-only consultation batch."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConsultationChild:
    node_id: str
    role: str
    effects: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    criticality: str = "required"


@dataclass(frozen=True, slots=True)
class ConsultationPlan:
    parent_id: str
    parent_role: str
    parent_effects: tuple[str, ...]
    registered_ids: tuple[str, ...]
    children: tuple[ConsultationChild, ...]
    max_agents: int
    completed_waits: int
    max_waits: int
