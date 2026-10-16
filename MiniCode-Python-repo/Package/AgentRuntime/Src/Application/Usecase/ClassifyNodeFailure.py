"""Classify authoritative failures without turning failed work into success."""

from ..Dto.FailureDiagnosis import FailureDiagnosis, FailureDiagnosisRequest


class ClassifyNodeFailure:
    def execute(self, request: FailureDiagnosisRequest) -> FailureDiagnosis:
        if request.ambiguous_side_effect or request.kind == "UncertainSideEffect":
            category = "uncertain_side_effect"
        elif request.kind in {"PolicyDenied", "ApprovalExpired"}:
            category = "authorization"
        elif request.kind == "BudgetExhausted":
            category = "budget"
        elif request.kind == "ConsultationBudgetLimited" and not request.failed_verification:
            category = "consultation_limit"
        elif request.kind == "Canceled" or request.state == "CANCELED":
            category = "canceled"
        elif request.failed_verification or request.kind == "VerificationFailed":
            category = "verification"
        elif request.kind == "ProtocolError":
            category = "protocol"
        elif request.kind == "WorkspaceBusy":
            category = "workspace"
        elif request.state == "SUCCEEDED" and not request.kind:
            category = "none"
        elif request.kind in {"ToolFailed", "ToolTimeout", "WorkerTimedOut"}:
            category = "execution_unavailable"
        else:
            category = "unknown"
        allowed = (
            category in {"execution_unavailable", "consultation_limit"} and request.state == "FAILED_TERMINAL"
            and request.criticality == "advisory" and request.consultation
            and request.role in {"explorer", "planner"} and request.effects == ("read_only",)
        )
        return FailureDiagnosis(category, allowed)
