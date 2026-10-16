"""Immutable reserves inside an existing run budget, never extra spending."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClosureBudgetRequest:
    max_tokens: int | None
    max_cost: float | None
    final_verifier: bool


@dataclass(frozen=True, slots=True)
class ClosureBudget:
    primary_tokens: int | None
    verification_tokens: int | None
    primary_cost: float | None
    verification_cost: float | None
    version: str = "closure-budget-v1"


@dataclass(frozen=True, slots=True)
class ConsultationBudgetRequest:
    max_tokens: int | None
    max_cost: float | None
    reserve: ClosureBudget
    used_tokens: int
    used_cost: float
    pending_tokens: int
    pending_cost: float
    estimated_tokens: int
    usage_reconciled: bool


@dataclass(frozen=True, slots=True)
class ConsultationBudgetDecision:
    allowed: bool
    available_tokens: int | None
    available_cost: float | None
    reason: str
