"""Isolated evaluation surface. Not wired into production verification or acceptance."""
from dataclasses import dataclass, field

from ..Application.Usecase.EvaluateClaimReview import EvaluateClaimReview


@dataclass(frozen=True, slots=True)
class Env:
    pass


@dataclass(frozen=True, slots=True)
class RunnableApp:
    _review: EvaluateClaimReview = field(default_factory=EvaluateClaimReview, repr=False)

    def prepare_decision(self, candidate, sources, **limits):
        return self._review.prepare_decision(candidate, sources, **limits)

    def select_confirmation(self, plan, batch_result):
        return self._review.select_confirmation(plan, batch_result)

    def parse_confirmation(self, plan, request, output):
        return self._review.parse_confirmation(plan, request, output)

    def decide(self, plan, batches, confirmation=None):
        return self._review.decide(plan, batches, confirmation)

    def score_decision(self, plan, decision, gold):
        return self._review.score_decision(plan, decision, gold)

    def prepare(self, candidate, sources, strategy, **limits):
        return self._review.prepare(candidate, sources, strategy, **limits)

    def parse(self, plan, batch, output):
        return self._review.parse(plan, batch, output)

    def expand(self, plan, batch):
        return self._review.expand(plan, batch)

    def split(self, plan, batch):
        return self._review.split(plan, batch)

    def combine(self, batch, results):
        return self._review.combine(batch, results)

    def aggregate(self, plan, results):
        return self._review.aggregate(plan, results)

    def score(self, plan, result, gold):
        return self._review.score(plan, result, gold)


def createApp(env=None):
    if env is not None and not isinstance(env, Env):
        raise TypeError("ClaimReviewEvaluation requires its own Env")
    return RunnableApp()
