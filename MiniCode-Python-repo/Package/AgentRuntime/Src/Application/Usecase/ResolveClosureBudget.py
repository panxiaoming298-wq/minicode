"""Reserve primary closure and planned verification without prescribing topology."""

import math
from decimal import Decimal

from ..Dto.ClosureBudget import ClosureBudget, ClosureBudgetRequest, ConsultationBudgetDecision, ConsultationBudgetRequest


class ResolveClosureBudget:
    def execute(self, request: ClosureBudgetRequest) -> ClosureBudget:
        tokens, cost = request.max_tokens, request.max_cost
        if tokens is not None and (type(tokens) is not int or tokens < 1):
            raise ValueError("closure max_tokens must be a positive integer or None")
        if cost is not None and (type(cost) not in {int, float} or not math.isfinite(cost) or cost <= 0):
            raise ValueError("closure max_cost must be finite and positive or None")
        if type(request.final_verifier) is not bool:
            raise ValueError("final_verifier must be boolean")
        verification_tokens = None if tokens is None else (tokens + 9) // 10 if request.final_verifier else 0
        primary_tokens = None if tokens is None else min(tokens - verification_tokens, (tokens + 4) // 5)
        verification_cost = None if cost is None else float(Decimal(str(cost)) / 10) if request.final_verifier else 0.0
        return ClosureBudget(primary_tokens, verification_tokens,
            None if cost is None else float(Decimal(str(cost)) / 5), verification_cost)

    def admit(self, request: ConsultationBudgetRequest) -> ConsultationBudgetDecision:
        self.execute(ClosureBudgetRequest(request.max_tokens, request.max_cost, False))
        if type(request.usage_reconciled) is not bool or request.reserve.version != "closure-budget-v1":
            raise ValueError("invalid consultation reconciliation flag or closure policy version")
        for cap, amounts, integer in (
            (request.max_tokens, (request.reserve.primary_tokens, request.reserve.verification_tokens), True),
            (request.max_cost, (request.reserve.primary_cost, request.reserve.verification_cost), False),
        ):
            if cap is None:
                if any(value is not None for value in amounts):
                    raise ValueError("an unbounded dimension cannot have a closure reserve")
            elif (any(type(value) not in ({int} if integer else {int, float})
                      or not math.isfinite(value) or value < 0 for value in amounts)
                  or sum(Decimal(str(value)) for value in amounts) > Decimal(str(cap))):
                raise ValueError("closure reserves must fit inside the configured cap")
        for value in (request.used_tokens, request.pending_tokens, request.estimated_tokens):
            if type(value) is not int or value < 0:
                raise ValueError("consultation token amounts must be nonnegative integers")
        for value in (request.used_cost, request.pending_cost):
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise ValueError("consultation costs must be finite and nonnegative")
        reserve = request.reserve
        tokens = None if request.max_tokens is None else max(0, request.max_tokens - request.used_tokens
            - request.pending_tokens - (reserve.primary_tokens or 0) - (reserve.verification_tokens or 0))
        cost = None if request.max_cost is None else max(0.0, float(Decimal(str(request.max_cost))
            - Decimal(str(request.used_cost)) - Decimal(str(request.pending_cost))
            - Decimal(str(reserve.primary_cost or 0)) - Decimal(str(reserve.verification_cost or 0))))
        if not request.usage_reconciled:
            return ConsultationBudgetDecision(False, tokens, cost, "bounded usage is unknown or requires reconciliation")
        if tokens is not None and tokens < max(1, request.estimated_tokens):
            return ConsultationBudgetDecision(False, tokens, cost, "consultation would consume the primary/verification token reserve")
        if cost is not None and cost <= 0:
            return ConsultationBudgetDecision(False, tokens, cost, "consultation would consume the primary/verification cost reserve")
        return ConsultationBudgetDecision(True, tokens, cost, "estimate fits outside closure reserves; each model request is checked again")
