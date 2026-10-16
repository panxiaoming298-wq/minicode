"""Authorize a new candidate, never a replay or a relaxation of acceptance."""

from ..Dto.RepairPolicy import RepairDecision, RepairPolicyRequest


class DecideCandidateRepair:
    @staticmethod
    def review_failure_is_recoverable(kind: str, *, ambiguous: bool = False) -> bool:
        # These are read-only failures. Authorization, cancellation, uncertain
        # side effects and unreconciled usage never acquire a retry capability.
        return not ambiguous and kind in {
            "VerificationFailed", "ReviewUnavailable", "ToolFailed", "ToolTimeout",
            "WorkerTimedOut", "ModelOutputTruncated", "BudgetExhausted", "ConsultationBudgetLimited",
        }

    def execute(self, request: RepairPolicyRequest) -> RepairDecision:
        if type(request.max_rounds) is not int or not 0 <= request.max_rounds <= 4:
            raise ValueError("repair rounds must be an integer between 0 and 4")
        if type(request.completed_rounds) is not int or request.completed_rounds < 0:
            raise ValueError("completed repair rounds must be a nonnegative integer")
        if request.canceled or request.paused:
            return RepairDecision(False, "canceled or paused runs cannot start candidate repair")
        if not request.max_rounds:
            return RepairDecision(False, "candidate repair is disabled")
        if request.completed_rounds >= request.max_rounds:
            return RepairDecision(False, "candidate repair round budget exhausted")
        if not request.all_required_work_resolved:
            return RepairDecision(False, "node failures or unfinished work require separate resolution")
        if len(request.candidate_revision) != 64 or any(c not in "0123456789abcdef" for c in request.candidate_revision):
            return RepairDecision(False, "candidate repair requires a verified SHA-256 workspace revision")
        review_retry = bool(request.failure_kinds) and all(
            kind == "review_unavailable" for kind in request.failure_kinds)
        if request.candidate_revision in request.previous_candidates and not review_retry:
            return RepairDecision(False, "candidate repair made no workspace progress or repeated a previous candidate")
        if not request.failure_kinds or any(kind not in {
            "candidate_check_failed", "candidate_review_failed", "review_unavailable",
        } for kind in request.failure_kinds):
            return RepairDecision(False, "only completed candidate checks or authenticated review failures are repairable")
        return RepairDecision(True, "return verification diagnostics to the primary for a new candidate")
