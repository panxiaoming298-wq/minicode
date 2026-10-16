"""Serializable model-call accounting; mutations are serialized by RuntimeReducer."""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from minicode.runtime_protocol import BudgetEnvelope, ExecutionFailureKind

# Evidence fields a settled call may still gain. None of them is a charge:
# every numeric counter and outcome stays exactly as first settled.
_REFINABLE_FIELDS = frozenset({"raw_response", "provider_failure"})


def budget_stop_failure_kind(reason: str) -> ExecutionFailureKind:
    """Which kind a stopped run really stopped for, from the reason that recorded it.

    Every bounded stop ends work the same way, so they are easy to flatten into one
    "Canceled" verdict. That loses the fact the caller has to act on: an exhausted
    token/cost budget means raise the budget (or shrink the request), a passed
    deadline means allow more time, and only an unrecognized reason stays a
    cancellation. This is the single definition both the Scheduler and the Worker
    use, so the two can never disagree about the same stop.
    """

    lowered = (reason or "").lower()
    if "usage" in lowered and any(word in lowered for word in ("reconcil", "incomplete", "unknown", "unsettled")):
        return ExecutionFailureKind.MODEL_USAGE_UNRECONCILED
    if "deadline" in lowered:
        return ExecutionFailureKind.WORKER_TIMED_OUT
    if "token" in lowered or "cost" in lowered or "budget" in lowered:
        return ExecutionFailureKind.BUDGET_EXHAUSTED
    return ExecutionFailureKind.CANCELED


class BudgetUnavailable(RuntimeError):
    """A model request cannot be admitted under the current budget."""

    def __init__(self, message: str, *, diagnostics: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = deepcopy(dict(diagnostics or {}))


class ConsultationBudgetLimited(BudgetUnavailable):
    """This helper cannot spend closure reserves; the global budget is not waived."""


class ModelUsageUnreconciled(BudgetUnavailable):
    """A bounded bill is unknown; increasing the ceiling cannot reconcile it."""


def nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return float(value)


def add_amounts(*amounts: float) -> float:
    return float(sum(Decimal(str(amount)) for amount in amounts))


@dataclass(frozen=True, slots=True)
class TokenPricing:
    """Explicit rates in currency units per million tokens, never a fallback price."""

    input: float
    output: float
    cache_read: float
    cache_write: float

    def __post_init__(self) -> None:
        for name in ("input", "output", "cache_read", "cache_write"):
            nonnegative(getattr(self, name), name)

    @property
    def input_ceiling(self) -> float:
        return max(self.input, self.cache_read, self.cache_write)

    def cost(
        self, input_tokens: int, output_tokens: int, cached: int = 0, created: int = 0
    ) -> float:
        # input_tokens includes cached and cache-creation input in the normalized protocol.
        return float(
            sum(
                (
                    Decimal(max(0, input_tokens - cached - created))
                    * Decimal(str(self.input)),
                    Decimal(output_tokens) * Decimal(str(self.output)),
                    Decimal(cached) * Decimal(str(self.cache_read)),
                    Decimal(created) * Decimal(str(self.cache_write)),
                )
            )
            / Decimal(1_000_000)
        )


def _metadata_refinement(
    previous: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Return the refined usage when only attached evidence was added.

    A settled call may gain non-numeric evidence that only became available
    after its final usage observation. Nothing may be removed, changed, or
    added outside that evidence set, so this can never restate a charge.
    """
    if not _REFINABLE_FIELDS.intersection(previous.keys() | candidate.keys()):
        return None
    if "provider_failure" in previous and previous["provider_failure"] != candidate.get("provider_failure"):
        return None  # A recorded provider error may be added once, never rewritten.
    core = {key: value for key, value in candidate.items() if key not in _REFINABLE_FIELDS}
    if core != {key: value for key, value in previous.items() if key not in _REFINABLE_FIELDS}:
        return None
    if any(key not in candidate for key in previous if key not in _REFINABLE_FIELDS):
        return None
    return {**previous, **{key: candidate[key] for key in _REFINABLE_FIELDS if key in candidate}}


def cache_usage_summary(calls: Mapping[str, Mapping[str, Any]], *, legacy_calls: int = 0) -> dict[str, Any]:
    """Token-weighted provider observations, with explicit reporting coverage.

    Old positive cache counters are evidence of a hit. An old zero without a
    reporting flag is ambiguous and must not become a fabricated cache miss.
    Pending or incomplete calls never contribute to the displayed hit rate.
    """
    def totals(values, unknown=0):
        reported = []
        for call in values:
            usage = call.get("usage") or {}
            cached, inputs = usage.get("cache_read_tokens"), usage.get("input_tokens")
            known = usage.get("cache_tokens_known")
            if (call.get("status") == "settled" and usage.get("tokens_known") is True
                and type(cached) is int and type(inputs) is int and 0 <= cached <= inputs
                and (known is True or (known is None and cached > 0))):
                reported.append((inputs, cached))
        inputs = sum(item[0] for item in reported)
        cached = sum(item[1] for item in reported)
        count = len(values) + unknown
        return {"input_tokens": inputs, "cache_read_tokens": cached,
                "hit_rate": cached / inputs if inputs else None,
                "reported_calls": len(reported), "model_calls": count,
                "complete": count > 0 and len(reported) == count}

    groups = {key: [] for key in ("primary", "agents", "memory_rerank", "other")}
    for call in calls.values():
        purpose = (call.get("usage") or {}).get("prepared_request", {}).get("call_identity", {}).get("purpose", "agent")
        key = ("memory_rerank" if purpose == "memory_rerank" else "other" if purpose != "agent"
               else "primary" if call.get("node_id") in {"primary", "root", "deep-worker", "exclusive-writer", "read-synthesizer"}
               else "agents")
        groups[key].append(call)
    return {**totals(list(calls.values()), legacy_calls),
            "groups": [{"key": key, **totals(values)} for key, values in groups.items() if values]}


@dataclass(slots=True)
class UsageLedger:
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    result_attempts: set[str] = field(default_factory=set)
    result_usage: dict[str, dict[str, Any]] = field(default_factory=dict)

    def pending(self, attempt_id: str | None = None) -> tuple[int, float]:
        calls = [
            c
            for c in self.calls.values()
            if attempt_id is None or c["attempt_id"] == attempt_id
        ]
        return (
            sum(c["reserved_tokens"] for c in calls),
            add_amounts(*(c["reserved_cost"] for c in calls)),
        )

    def has_calls(self, attempt_id: str) -> bool:
        return any(c["attempt_id"] == attempt_id for c in self.calls.values())

    def budget_reconciled(self, budget: BudgetEnvelope, attempt_id: str | None = None) -> bool:
        """Pending calls hold reservations; uncertain/unknown bounded bills do not."""
        if budget.max_tokens is None and budget.max_cost is None:
            return True
        calls = [c for c in self.calls.values() if attempt_id is None or c["attempt_id"] == attempt_id]
        legacy = [value for key, value in self.result_usage.items() if attempt_id is None or key == attempt_id]
        if any(call["status"] == "uncertain" for call in calls):
            return False
        if budget.max_tokens is not None and any(item.get("tokens_known", True) is not True for item in legacy):
            return False
        if budget.max_cost is not None and (
            any(call["status"] == "settled" and call.get("cost") is None for call in calls)
            or any(not item.get("cost_known", True) or ("cost" in item and item["cost"] is None) for item in legacy)
        ):
            return False
        return True

    def settlement_ready(self, budget: BudgetEnvelope, attempt_id: str | None = None) -> bool:
        """Wait for live calls; unknown finished bills only block usage-capped work.

        This is admission policy, not reconciliation: uncertain records and
        their reservations remain intact for truthful display and later audit.
        """
        if any(c["status"] == "pending" for c in self.calls.values()
               if attempt_id is None or c["attempt_id"] == attempt_id):
            return False
        return self.budget_reconciled(budget, attempt_id)

    def summary(self, attempt_id: str | None = None) -> dict[str, Any]:
        calls = [
            c
            for c in self.calls.values()
            if attempt_id is None or c["attempt_id"] == attempt_id
        ]
        legacy = [
            value
            for key, value in self.result_usage.items()
            if attempt_id is None or key == attempt_id
        ]
        cost_known = all(
            c["status"] == "settled" and c.get("cost") is not None for c in calls
        ) and all(
            item.get("cost_known", True)
            and ("cost" not in item or item["cost"] is not None)
            for item in legacy
        )
        tokens_known = all(c["status"] == "settled" for c in calls) and all(
            item.get("tokens_known", True) for item in legacy
        )
        tokens = sum(c["charged_tokens"] for c in calls) + sum(
            int(item.get("tokens", 0) or 0) for item in legacy
        )
        cost = add_amounts(
            *(c["charged_cost"] for c in calls),
            *(float(item.get("cost", 0) or 0) for item in legacy),
        )
        reserved_tokens, reserved_cost = self.pending(attempt_id)
        return {
            "tokens": tokens,
            "cache": cache_usage_summary({call["call_id"]: call for call in calls},
                legacy_calls=sum(int(item.get("model_calls", 0) or 0) for item in legacy)),
            "cost": cost if cost_known else None,
            "known_cost": cost,
            "model_calls": len(calls)
            + sum(int(item.get("model_calls", 0) or 0) for item in legacy),
            "tokens_known": tokens_known,
            "cost_known": cost_known,
            "reserved_tokens": reserved_tokens,
            "reserved_cost": reserved_cost,
            "unsettled_calls": sum(c["status"] != "settled" for c in calls),
        }

    def reconciliation_diagnostics(self) -> dict[str, Any]:
        """Identify unsettled bills without exposing prompts or credentials."""
        return {
            "reason_code": "model_usage_unreconciled",
            "calls": [{key: call.get(key) for key in (
                "call_id", "node_id", "attempt_id", "model", "provider", "status", "outcome",
                "charged_tokens", "reserved_tokens", "charged_cost", "reserved_cost")}
                | {"purpose": call.get("usage", {}).get("prepared_request", {}).get("call_identity", {}).get("purpose"),
                   "transport_error": call.get("usage", {}).get("response_shape", {}).get("transport_error"),
                   "http_error": call.get("usage", {}).get("response_shape", {}).get("http_error")}
                for call in self.calls.values() if call["status"] == "uncertain" or
                    (call["status"] == "settled" and call.get("cost") is None)],
            "legacy_attempts": sorted(key for key, usage in self.result_usage.items()
                if usage.get("tokens_known", True) is not True or usage.get("cost_known", True) is not True
                or ("cost" in usage and usage["cost"] is None)),
        }

    def reserve(self, **request: Any) -> dict[str, Any]:
        record = self.quote(**request)
        self.calls[record["call_id"]] = record
        return dict(record)

    def quote(
        self,
        *,
        call_id: str,
        node_id: str,
        attempt_id: str,
        model: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        pricing: TokenPricing | None,
        budget: BudgetEnvelope,
        attempt_budget: BudgetEnvelope,
        tokens_used: int,
        cost_used: float,
        now: float,
        minimum_output_tokens: int = 1,
    ) -> dict[str, Any]:
        """Validate admission without mutation; reserve commits this exact quote."""
        if call_id in self.calls:
            raise ValueError("model call ID already reserved")
        if any(
            cap.max_tokens is not None or cap.max_cost is not None
            for cap in (budget, attempt_budget)
        ) and any(c["status"] == "uncertain" for c in self.calls.values()):
            raise ModelUsageUnreconciled(
                "unsettled model usage requires reconciliation before another request",
                diagnostics=self.reconciliation_diagnostics(),
            )
        nonnegative(input_tokens, "input reservation")
        nonnegative(output_tokens, "output reservation")
        if type(minimum_output_tokens) is not int or minimum_output_tokens < 1:
            raise ValueError("minimum output reservation must be a positive integer")
        if output_tokens < 1:
            raise BudgetUnavailable("model output reservation must be positive")
        reserved_tokens, reserved_cost = self.pending()
        local = self.summary(attempt_id)
        available_output = output_tokens
        scopes: list[dict[str, Any]] = []

        def decline(message: str, code: str) -> BudgetUnavailable:
            # Only ledger quantities, never request content or provider secrets.
            return BudgetUnavailable(message, diagnostics={
                "reason_code": code, "input_reservation": input_tokens,
                "requested_output_reservation": output_tokens,
                "minimum_output_reservation": minimum_output_tokens,
                "available_output_reservation": available_output, "scopes": scopes,
            })

        for name, cap, used, held in (
            ("run", budget, tokens_used, reserved_tokens),
            ("attempt", attempt_budget, local["tokens"], local["reserved_tokens"]),
        ):
            after_input = None if cap.max_tokens is None else cap.max_tokens - used - held - input_tokens
            scopes.append({"scope": name, "token_limit": cap.max_tokens,
                           "tokens_used": used, "tokens_held": held,
                           "output_tokens_after_input": after_input})
            if cap.deadline is not None and now >= cap.deadline:
                raise decline("deadline exhausted before model request", "deadline")
            if cap.max_tokens is not None:
                available_output = min(available_output, after_input)
        for scope, (cap, used, held) in zip(scopes, (
            (budget, cost_used, reserved_cost),
            (attempt_budget, local["known_cost"], local["reserved_cost"]),
        )):
            scope.update(cost_limit=cap.max_cost, known_cost_used=used, cost_held=held)
            if cap.max_cost is None:
                continue
            if pricing is None:
                raise decline("max_cost requires explicit pricing for this provider/model", "pricing_unknown")
            remainder = (
                Decimal(str(cap.max_cost))
                - Decimal(str(used))
                - Decimal(str(held))
                - Decimal(input_tokens)
                * Decimal(str(pricing.input_ceiling))
                / Decimal(1_000_000)
            )
            scope["cost_after_input"] = float(remainder)
            if remainder < 0:
                raise decline("max_cost budget cannot cover model input", "input_cost")
            if pricing.output > 0:
                scope["output_tokens_after_input_cost"] = int(
                    (remainder * Decimal(1_000_000) / Decimal(str(pricing.output)))
                    .to_integral_value(rounding=ROUND_FLOOR)
                )
                available_output = min(
                    available_output, scope["output_tokens_after_input_cost"],
                )
        if available_output < minimum_output_tokens:
            raise decline("model request cannot fit remaining token/cost budget", "minimum_output_unfunded")
        maximum_cost = (
            float(
                (
                    Decimal(input_tokens) * Decimal(str(pricing.input_ceiling))
                    + Decimal(available_output) * Decimal(str(pricing.output))
                )
                / Decimal(1_000_000)
            )
            if pricing is not None
            else 0.0
        )
        record = {
            "call_id": call_id,
            "node_id": node_id,
            "attempt_id": attempt_id,
            "model": model,
            "provider": provider,
            "status": "pending",
            "outcome": "pending",
            "input_reservation": input_tokens,
            "output_reservation": available_output,
            "reserved_tokens": input_tokens + available_output,
            "reserved_cost": maximum_cost,
            "charged_tokens": 0,
            "charged_cost": 0.0,
            "cost": None,
            "usage": {},
            "created_at": now,
        }
        return dict(record)

    def settle(
        self, call_id: str, usage: Mapping[str, Any], *, complete: bool, outcome: str
    ) -> tuple[int, float]:
        record = self.calls[call_id]
        clean = dict(usage)
        tokens = int(nonnegative(clean.get("tokens", 0), "tokens"))
        raw_cost = clean.get("cost")
        cost = nonnegative(raw_cost, "cost") if raw_cost is not None else None
        status = (
            "settled"
            if complete
            else "pending"
            if outcome == "pending"
            else "uncertain"
        )
        if record["status"] == "settled":
            refinement = _metadata_refinement(record["usage"], clean)
            if refinement is not None and status == "settled":
                # Metadata-only refinement: the charge is identical and no
                # numeric counter moved. A settled call keeps its tokens/cost,
                # but attached evidence that only became available after the
                # final usage observation (for example the frozen raw-response
                # receipt) can still be recorded instead of being dropped.
                record["usage"] = refinement
                record["outcome"] = outcome
                return 0, 0.0
            if record["usage"] != clean or status != "settled":
                raise ValueError(
                    "a settled model call cannot be charged again with different usage"
                )
            record["outcome"] = outcome
            return 0, 0.0
        # Partial stream observations are cumulative, not additive.
        tokens = max(tokens, record["charged_tokens"])
        charged_cost = max(cost or 0.0, record["charged_cost"])
        delta_tokens = tokens - record["charged_tokens"]
        delta_cost = add_amounts(charged_cost, -record["charged_cost"])
        record["reserved_tokens"] = (
            0 if complete else max(0, record["reserved_tokens"] - delta_tokens)
        )
        record["reserved_cost"] = (
            0.0
            if complete and cost is not None
            else max(0.0, add_amounts(record["reserved_cost"], -delta_cost))
        )
        record.update(
            {
                "status": status,
                "outcome": outcome,
                "usage": clean,
                "charged_tokens": tokens,
                "charged_cost": charged_cost,
                "cost": cost,
            }
        )
        return delta_tokens, delta_cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": deepcopy(self.calls),
            "result_attempts": sorted(self.result_attempts),
            "result_usage": {
                key: dict(value) for key, value in self.result_usage.items()
            },
        }

    @classmethod
    def restore(cls, raw: Mapping[str, Any]) -> UsageLedger:
        if not isinstance(raw, Mapping):
            raise TypeError("persisted usage ledger must be a mapping")
        ledger = cls(result_attempts=set(raw.get("result_attempts", ())))
        ledger.result_usage = {
            str(key): dict(value) for key, value in raw.get("result_usage", {}).items()
        }
        for call_id, value in raw.get("calls", {}).items():
            record = dict(value)
            if record["call_id"] != call_id or record["status"] not in {
                "pending",
                "uncertain",
                "settled",
            }:
                raise ValueError("invalid persisted model call")
            for key in (
                "reserved_tokens",
                "reserved_cost",
                "charged_tokens",
                "charged_cost",
            ):
                nonnegative(record[key], key)
            ledger.calls[call_id] = record
        return ledger
