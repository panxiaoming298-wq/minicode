"""Runtime-owned candidate changes, separate from a model's change claims."""
from dataclasses import dataclass
import re

from .ReviewPolicy import ReviewPolicy


@dataclass(frozen=True, slots=True)
class ReviewObservation:
    candidate_revision: str
    changed_resources: tuple[str, ...]
    integrated_nodes: tuple[str, ...] = ()

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.candidate_revision):
            raise ValueError("review observation requires the candidate content revision")
        for path in self.changed_resources:
            if (not isinstance(path, str) or not path or ":" in path or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))):
                raise ValueError("review changes must be literal workspace-relative paths")
        if any(not isinstance(node, str) or not node or node == "primary" for node in self.integrated_nodes):
            raise ValueError("integrated contributions require child identities")


@dataclass(frozen=True, slots=True)
class ReviewEscalationRequest:
    admitted: ReviewPolicy
    current: ReviewPolicy
    observation: ReviewObservation
    max_agents: int
    reasoning_effort: str | None = None
