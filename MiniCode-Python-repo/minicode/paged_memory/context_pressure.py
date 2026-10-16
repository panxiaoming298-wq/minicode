"""Untrimmed request pressure and durable epoch counters for mode selection."""
from __future__ import annotations

from dataclasses import replace

from .rotation import history_for_anchor, project_history
from .context_utilization import growth_reserve


def measure_pressure(*, metrics, messages, rotation, state, required_items, config,
                     token_budget, model_context_limit, tool_tokens, request_budget=None):
    active = rotation.get("status") == "active"
    projected = None
    if messages is not None:
        system = [m for m in messages if m.get("role") == "system"]
        history = [m for m in messages if m.get("role") != "system"]
        projected = [*system, *project_history(history, rotation)]
    history_tokens = (sum(token_budget.estimate_message(m) for m in projected)
                      if projected is not None else metrics.estimated_input_tokens)
    required_tokens = sum(token_budget.estimate_text(item.text) for item in required_items)
    reserved_tools = max(config.tool_token_reserve, tool_tokens)
    input_limit = (request_budget.input_limit if request_budget is not None else
                   max(0, model_context_limit - config.output_token_reserve - config.safety_token_reserve))
    pressure_window = (request_budget.capacity["budget_policy"]["pressure_window"]
                       if request_budget is not None else input_limit)
    admission_input = history_tokens + required_tokens + reserved_tools
    if request_budget is not None and projected is not None:
        # Include native task/note sources before optional allocation. The adapter
        # adds real schemas, reasoning controls and the live async task snapshot.
        admission_input = request_budget.measure([
            {"role": "system", "content": "\n\n".join(item.text for item in required_items)}, *projected])
    effective = replace(metrics, context_usage_ratio=min(2.0,
        admission_input / max(1, pressure_window)))
    growth = growth_reserve(config, metrics, input_limit)
    effective.context_forecast_ratio = min(2.0,
        (admission_input + growth) / max(1, pressure_window))
    baseline = rotation.get("counter_baseline", {})
    counter_source = "lifetime"
    if (active and isinstance(baseline, dict) and state is not None
            and all(type(baseline.get(key)) is int and baseline[key] >= 0
                    for key in ("completed_steps", "tool_result_tokens", "large_artifact_count"))
            and isinstance(baseline.get("run_id"), str)):
        effective.completed_steps = max(0, len(state.completed_steps) - baseline["completed_steps"])
        if baseline["run_id"] == metrics.counter_run_id:
            effective.cumulative_tool_result_tokens = max(0,
                metrics.cumulative_tool_result_tokens - baseline["tool_result_tokens"])
            effective.large_artifact_count = max(0, metrics.large_artifact_count - baseline["large_artifact_count"])
            counter_source = "sealed_epoch_baseline"
        else:
            # Fresh runs have fresh local counters. The durable post-boundary
            # conversation is also counted; another run's baseline is not
            # subtracted unless its checkpoint restored that counter origin.
            anchor = rotation["anchor"]
            suffix = history_for_anchor(history, anchor)[anchor["prefix_count"]:] if messages is not None else []
            visible_tokens = sum(token_budget.estimate_message(m) for m in suffix if m.get("role") == "tool_result")
            effective.cumulative_tool_result_tokens = max(metrics.cumulative_tool_result_tokens, visible_tokens)
            counter_source = "epoch_steps_and_new_run_visible_tools"
    info = {
        "policy": "effective-context-v1" if config.context_mode_policy_enabled else "lifetime",
        "canonical_message_tokens_estimated": metrics.estimated_input_tokens,
        "projected_message_tokens_estimated": history_tokens,
        "required_packet_tokens_estimated": required_tokens,
        "reserved_tool_tokens": reserved_tools,
        "input_limit": input_limit,
        "pressure_window": pressure_window,
        "admission_input_tokens": admission_input,
        "accounting": "request_context_measurement" if request_budget is not None else "memory_estimate",
        "effective_pressure_ratio": effective.context_usage_ratio,
        "forecast_pressure_ratio": effective.context_forecast_ratio,
        "growth_reserve_tokens": growth,
        "lifetime_completed_steps": metrics.completed_steps,
        "lifetime_tool_result_tokens": metrics.cumulative_tool_result_tokens,
        "effective_completed_steps": effective.completed_steps,
        "effective_tool_result_tokens": effective.cumulative_tool_result_tokens,
        "effective_large_artifact_count": effective.large_artifact_count,
        "counter_source": counter_source,
        "active_epoch": rotation.get("epoch", 0) if active else 0,
    }
    return effective, info
