"""Provider-independent inputs and decisions for bounded candidate repair."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RepairPolicyRequest:
    max_rounds: int
    completed_rounds: int
    candidate_revision: str
    previous_candidates: tuple[str, ...]
    all_required_work_resolved: bool
    canceled: bool
    paused: bool
    failure_kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepairDecision:
    allowed: bool
    reason: str
