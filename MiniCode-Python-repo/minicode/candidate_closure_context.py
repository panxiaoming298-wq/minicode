"""Compatibility entry for the candidate-only pilot; one shared phase engine."""
from minicode.primary_phase_context import CANDIDATE_POLICY as POLICY, PrimaryPhaseContext


class CandidateClosureContext(PrimaryPhaseContext):
    def __init__(self, current_revision, *, base_revision: str):
        super().__init__(current_revision, base_revision=base_revision, policy=POLICY)
