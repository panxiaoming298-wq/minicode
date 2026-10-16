"""Snapshot-only inputs for the candidate-to-workspace delivery boundary."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CandidateIntegrationRequest:
    accepted_revision: str
    candidate_revision: str
    baseline_revision: str
    source_revision: str


@dataclass(frozen=True, slots=True)
class CandidateIntegrationDecision:
    allowed: bool
    reason: str
