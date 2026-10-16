"""Prospective token planning from admitted primary requests, not a new ledger.

This deliberately uses the existing conservative UTF-8 input reservations. It
does not divide bytes by an assumed tokenizer ratio or claim to bound arbitrary
future tool results. The allowances describe one conservative planning scenario,
not a necessary minimum or a spending authorization. Old fixed helper ceilings
are admission constraints. The shared-pool caller explicitly relabels the same
helper amounts as advisory startup targets, not disjoint lifetime allocations.
Every actual request still goes through the atomic admission controller.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from fractions import Fraction
from math import ceil, isfinite
from typing import Any


CONSULTATION_TOKEN_CAP = 16_000
FORECAST_VERSION = "consultation-token-forecast-v2"


def input_reservation_observations(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Diagnose observed sizing; NEVER authorize a request or recalibrate a cap.

    The resume probe gives a byte-ratio shortcut the actual future body size.
    Even this hindsight advantage may undercount after a handoff. It is not a
    prospective tokenizer, price estimate, model-quality score, or rollout gate.
    Unknown/incomplete records are counted as unavailable, never as zero usage.
    """
    usable = []
    for call in calls:
        usage = call.get("usage")
        sizing = usage.get("prepared_request") if isinstance(usage, Mapping) else None
        if not isinstance(sizing, Mapping) or usage.get("tokens_known") is not True:
            continue
        timestamp = call.get("created_at")
        if (call.get("status") != "settled" or type(timestamp) not in (int, float) or not isfinite(timestamp)
            or any(not isinstance(call.get(key), str) or not call[key]
                   for key in ("provider", "model", "node_id", "attempt_id", "call_id"))
            or type(call.get("input_reservation")) is not int or call["input_reservation"] < 1
            or type(usage.get("input_tokens")) is not int or usage["input_tokens"] < 1
            or any(type(sizing.get(key)) is not int or sizing[key] < 0
                   for key in ("messages_utf8_bytes", "tools_utf8_bytes"))):
            continue
        body_bytes = sizing["messages_utf8_bytes"] + sizing["tools_utf8_bytes"]
        if body_bytes:
            usable.append({"call": call, "bytes": body_bytes, "input": usage["input_tokens"],
                           "density": Fraction(usage["input_tokens"], body_bytes)})
    usable.sort(key=lambda item: (item["call"]["created_at"], item["call"]["call_id"]))
    probes, seen, history = [], set(), []
    for item in usable:
        call = item["call"]
        if call["node_id"] != "primary":
            continue
        identity = (call["provider"], call["model"])
        activation = (*identity, call["attempt_id"])
        if activation not in seen:
            previous = [entry for entry in history
                        if (entry["call"]["provider"], entry["call"]["model"]) == identity
                        and entry["call"]["attempt_id"] != call["attempt_id"]
                        and entry["call"]["created_at"] < call["created_at"]]
            if previous:
                density = max(entry["density"] for entry in previous)
                estimate = ceil(item["bytes"] * density)
                probes.append({"call_id": call["call_id"], "attempt_id": call["attempt_id"],
                    "prior_primary_calls": len(previous), "observed_future_body_bytes": item["bytes"],
                    "prior_max_density": {"numerator": density.numerator, "denominator": density.denominator},
                    "ratio_estimated_input_tokens": estimate, "actual_input_tokens": item["input"],
                    "underestimate_tokens": max(0, item["input"] - estimate)})
            seen.add(activation)
        history.append(item)
    actual = sum(item["input"] for item in usable)
    reserved = sum(item["call"]["input_reservation"] for item in usable)
    return {"version": "input-reservation-observations-v1", "is_admission_policy": False,
            "observed_calls": len(usable), "unavailable_calls": len(calls) - len(usable),
            "actual_input_tokens": actual if usable else None,
            "input_reservation_sum": reserved if usable else None,
            "reservation_to_actual_ratio": reserved / actual if usable else None,
            "observed_input_exceeded_reservation": sum(item["input"] > item["call"]["input_reservation"] for item in usable),
            "primary_resume_probes": probes,
            "resume_underestimates": sum(probe["underestimate_tokens"] > 0 for probe in probes),
            "limitation": "Observed sizes, not spending totals. Resume probes use the observed future body, "
                          "not a prospective request estimate. No tokenizer ratio or hard budget is changed."}


def consultation_forecast(
    calls: Sequence[Mapping[str, Any]], *, attempt_id: str,
    objectives: tuple[str, ...], helper_token_limits: tuple[int | None, ...],
    writes_workspace: bool,
) -> dict[str, Any] | None:
    """Return an auditable planning estimate without reading content or spending.

    Model-free Workers have no primary calls and retain the existing admission
    policy. A model-backed primary must have a settled envelope for THIS
    activation; old activations and helper models cannot supply its estimate.
    Scheduler supplies old lifetime ceilings or shared first-attempt targets;
    the shared caller must label the returned helper estimate as advisory.
    """
    if len(helper_token_limits) != len(objectives) or any(
        limit is not None and (type(limit) is not int or limit < 1)
        for limit in helper_token_limits
    ):
        raise ValueError("helper token limits must match the consultation batch")
    primary = [call for call in calls if call.get("node_id") == "primary"]
    if not primary:
        return None
    current = [call for call in primary if call.get("attempt_id") == attempt_id]
    result: dict[str, Any] = {
        "version": FORECAST_VERSION, "known": False,
        "source": "largest_current_primary_admitted_envelope", "is_reservation": False,
        "primary_estimate_authority": "advisory_scenario",
    }
    if not current or any(call.get("status") != "settled" for call in current):
        return {**result, "reason": "current primary request envelope is absent or unsettled"}
    if any(limit is None for limit in helper_token_limits):
        return {**result, "reason": "helper token ceiling is not bounded"}
    if any(type(call.get(name)) is not int or call[name] < 1
           for call in current for name in ("input_reservation", "output_reservation")):
        return {**result, "reason": "current primary request sizing is unavailable"}
    # JSON stores may sort call IDs. Never infer recency from map order, or use
    # a temporarily smaller projected request to underfund future continuation.
    largest = max(current, key=lambda call: (call["input_reservation"], call["call_id"]))
    base = largest["input_reservation"]
    output = max(call["output_reservation"] for call in current)
    # Include the proposing tool call, returned evidence/diagnosis envelopes,
    # and activation instructions. These are allowances, not provider tokens or
    # an assertion that every possible result has this maximum size.
    proposal = sum(len(objective.encode("utf-8")) for objective in objectives)
    delivery = 6144 * len(objectives)
    activation = 1024
    growth = 4096
    stages = ("resume_read", "edit", "verify_deliver") if writes_workspace else ("resume_read", "synthesize_verify")
    inputs = tuple(base + proposal + delivery + activation + index * growth for index in range(len(stages)))
    primary_tokens = sum(value + output for value in inputs)
    return {
        **result, "known": True, "source_call_id": largest["call_id"],
        "base_input_reservation": base, "output_reservation_per_stage": output,
        "proposal_utf8_bytes": proposal, "delivery_allowance": delivery,
        "activation_allowance": activation, "tool_growth_per_stage": growth,
        "stages": list(stages), "stage_input_estimates": list(inputs),
        "primary_tokens": primary_tokens, "helper_tokens": sum(helper_token_limits),
        "reason": "conservative closure-path estimate; future reads, retries and prices are not guaranteed",
    }
