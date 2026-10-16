"""Zero-adapter composition root for provider-independent Runtime policy."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..Application.Dto.ExecutionPolicy import ExecutionPolicy, PolicyRequest
from ..Application.Usecase.ResolveExecutionPolicy import ResolveExecutionPolicy
from ..Application.Dto.ConsultationPlan import ConsultationPlan
from ..Application.Usecase.ValidateConsultation import ValidateConsultation
from ..Application.Dto.RepairPolicy import RepairDecision, RepairPolicyRequest
from ..Application.Usecase.DecideCandidateRepair import DecideCandidateRepair
from ..Application.Dto.CandidateIntegration import CandidateIntegrationDecision, CandidateIntegrationRequest
from ..Application.Usecase.DecideCandidateIntegration import DecideCandidateIntegration
from ..Application.Dto.FailureDiagnosis import FailureDiagnosis, FailureDiagnosisRequest
from ..Application.Usecase.ClassifyNodeFailure import ClassifyNodeFailure
from ..Application.Dto.ClosureBudget import ClosureBudget, ClosureBudgetRequest, ConsultationBudgetDecision, ConsultationBudgetRequest
from ..Application.Usecase.ResolveClosureBudget import ResolveClosureBudget
from ..Application.Dto.AgentDispatch import AgentDispatch
from ..Application.Usecase.ValidateAgentDispatch import ValidateAgentDispatch
from ..Application.Usecase.PreparePromptCache import PreparePromptCache
from ..Application.Usecase.ResolveCollaborationMode import ResolveCollaborationMode
from ..Application.Dto.ReviewPolicy import ReviewPolicy, ReviewPolicyRequest
from ..Application.Usecase.ResolveReviewPolicy import ResolveReviewPolicy
from ..Application.Dto.ReviewObservation import ReviewEscalationRequest
from ..Application.Usecase.EscalateReviewPolicy import EscalateReviewPolicy
from ..Application.Dto.ReviewDirective import ReviewDirectiveRequest
from ..Application.Usecase.ResolveReviewDirective import ResolveReviewDirective


@dataclass(frozen=True, slots=True)
class Env:
    """No external infrastructure is needed for policy resolution."""


@dataclass(frozen=True, slots=True)
class RunnableApp:
    _resolver: ResolveExecutionPolicy = field(repr=False)
    _consultations: ValidateConsultation = field(repr=False)
    _repairs: DecideCandidateRepair = field(repr=False)
    _integration: DecideCandidateIntegration = field(repr=False)
    _failures: ClassifyNodeFailure = field(repr=False)
    _closure: ResolveClosureBudget = field(repr=False)
    _agents: ValidateAgentDispatch = field(repr=False)
    _cache: PreparePromptCache = field(repr=False)
    _collaboration: ResolveCollaborationMode = field(repr=False)
    _review: ResolveReviewPolicy = field(repr=False)
    _review_escalation: EscalateReviewPolicy = field(repr=False)
    _review_directive: ResolveReviewDirective = field(repr=False)

    def resolve_review_directive(self, request: ReviewDirectiveRequest) -> bool | None:
        return self._review_directive.execute(request)

    def escalate_review_policy(self, request: ReviewEscalationRequest) -> ReviewPolicy:
        return self._review_escalation.execute(request)

    def resolve_review_policy(self, request: ReviewPolicyRequest) -> ReviewPolicy:
        return self._review.execute(request)

    def review_unavailable_is_optional(self, policy: ReviewPolicy, **outcome) -> bool:
        return self._review.unavailable_is_optional(policy, **outcome)

    def resolve_collaboration_mode(self, settings, **capabilities):
        return self._collaboration.execute(settings, **capabilities)

    def prepare_prompt_cache(self, body, **scope):
        return self._cache.execute(body, **scope)

    def validate_agent_dispatch(self, value) -> tuple[AgentDispatch, ...]:
        return self._agents.execute(value)

    def resolve_policy(self, request: PolicyRequest) -> ExecutionPolicy:
        return self._resolver.execute(request)

    def validate_consultation(self, plan: ConsultationPlan) -> tuple[str, ...]:
        return self._consultations.execute(plan)

    def decide_repair(self, request: RepairPolicyRequest) -> RepairDecision:
        return self._repairs.execute(request)

    def review_failure_is_recoverable(self, kind: str, *, ambiguous: bool = False) -> bool:
        return self._repairs.review_failure_is_recoverable(kind, ambiguous=ambiguous)

    def decide_integration(self, request: CandidateIntegrationRequest) -> CandidateIntegrationDecision:
        return self._integration.execute(request)

    def classify_failure(self, request: FailureDiagnosisRequest) -> FailureDiagnosis:
        return self._failures.execute(request)

    def resolve_closure_budget(self, request: ClosureBudgetRequest) -> ClosureBudget:
        return self._closure.execute(request)

    def admit_consultation_budget(self, request: ConsultationBudgetRequest) -> ConsultationBudgetDecision:
        return self._closure.admit(request)


def createApp(env: Env | None = None) -> RunnableApp:
    if env is not None and not isinstance(env, Env):
        raise TypeError("AgentRuntime requires its own composition Env")
    return RunnableApp(ResolveExecutionPolicy(), ValidateConsultation(), DecideCandidateRepair(), DecideCandidateIntegration(), ClassifyNodeFailure(), ResolveClosureBudget(), ValidateAgentDispatch(), PreparePromptCache(), ResolveCollaborationMode(), ResolveReviewPolicy(), EscalateReviewPolicy(), ResolveReviewDirective())
