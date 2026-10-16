"""Only integrate the exact verified tree onto its unchanged source baseline."""

from ..Dto.CandidateIntegration import CandidateIntegrationDecision, CandidateIntegrationRequest


class DecideCandidateIntegration:
    def execute(self, request: CandidateIntegrationRequest) -> CandidateIntegrationDecision:
        revisions = (request.accepted_revision, request.candidate_revision,
                     request.baseline_revision, request.source_revision)
        if any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value) for value in revisions):
            return CandidateIntegrationDecision(False, "candidate integration requires SHA-256 snapshot identities")
        if request.accepted_revision != request.candidate_revision:
            return CandidateIntegrationDecision(False, "candidate changed after acceptance; verification is stale")
        if request.baseline_revision != request.source_revision:
            return CandidateIntegrationDecision(False, "source workspace changed since isolation; candidate must be rebased and reverified")
        return CandidateIntegrationDecision(True, "verified candidate and source baseline are unchanged")
