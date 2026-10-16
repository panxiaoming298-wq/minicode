"""Bounded, deterministic lending between the optional context sections."""
from __future__ import annotations

from .rotation import project_history

# Supplemental retrieval is the lowest-priority source admitted for growth.
# Lower-priority optional diagnostics may use a base share, but do not expand it.
_MIN_GROWTH_PRIORITY = 45


def section_bases(config):
    return {"recent_conversation": config.recent_event_token_budget,
            "working_set": config.working_set_token_budget,
            "supplemental_memories": config.supplemental_token_budget,
            "recent_messages": config.recent_memory_token_budget}


def optional_capacity(config, token_budget, *, fixed_items, messages, available_budget, pressure=None):
    """One ceiling for candidate preparation and final optional allocation."""
    fixed_tokens = sum(token_budget.annotate(item).estimated_tokens for item in fixed_items)
    if not config.context_utilization_enabled:
        return fixed_tokens, max(0, available_budget - fixed_tokens)
    system_tokens = sum(token_budget.estimate_message(m) for m in (messages or ()) if m.get("role") == "system")
    packet_system_tokens = sum(item.estimated_tokens for item in fixed_items if item.item_type == "SYSTEM")
    fixed_tokens += max(0, system_tokens - packet_system_tokens)
    growth = (pressure or {}).get("growth_reserve_tokens", config.context_growth_reserve_tokens)
    basis = (pressure or {}).get("pressure_window", available_budget)
    target = min(available_budget, int(basis * config.context_rotation_target_ratio))
    return fixed_tokens, max(0, target - fixed_tokens - growth - 2048)


def _lend_by_demand(allocations, limits, bases, remaining):
    """Share spare capacity by base weights, returning unused shares each round."""
    active = [name for name in bases if allocations[name] < limits[name]]
    while remaining and active:
        total_weight = sum(max(1, bases[name]) for name in active)
        round_budget = remaining
        for name in active:
            share = max(1, round_budget * max(1, bases[name]) // total_weight)
            extra = min(limits[name] - allocations[name], share, remaining)
            allocations[name] += extra
            remaining -= extra
        active = [name for name in active if allocations[name] < limits[name]]


def allocate_sections(config, token_budget, *, sections, fixed_items, messages,
                      rotation_info, available_budget, pressure=None, optional_token_limit=None):
    """Reserve base shares, then grow useful live content within a bounded window.

    Required items may exceed a soft share, exactly as in the standard packer.
    The final provider guard remains responsible for the actual serialized wire.
    """
    bases = section_bases(config)
    seen = {item.dedup_key() for item in fixed_items}
    candidates, omitted = {}, []
    for name in ("working_set", "supplemental_memories", "recent_messages"):
        candidates[name] = []
        for item in sections[name]:
            token_budget.annotate(item)
            if item.dedup_key() in seen:
                omitted.append({"item_id": item.item_id, "reason": "duplicate"})
                continue
            seen.add(item.dedup_key())
            candidates[name].append(item)
    history = project_history([m for m in (messages or ()) if m.get("role") != "system"], rotation_info)
    demands = {name: sum(item.estimated_tokens for item in items) for name, items in candidates.items()}
    demands["recent_conversation"] = (sum(token_budget.estimate_message(m) for m in history)
                                      if messages is not None else bases["recent_conversation"])
    fixed_tokens, ceiling = optional_capacity(config, token_budget,
        fixed_items=fixed_items, messages=messages, available_budget=available_budget, pressure=pressure)
    if optional_token_limit is not None:
        ceiling = min(ceiling, max(0, optional_token_limit))
    pool = min(sum(bases.values()), ceiling)
    growth_limits = dict(demands)
    protection = False
    if config.context_utilization_enabled:
        from .context_utilization import pressure_status

        protection = pressure_status(config, pressure)["protection"] if pressure else False
        useful_tokens = {name: sum(item.estimated_tokens for item in items
                                  if item.required or item.priority >= _MIN_GROWTH_PRIORITY)
                         for name, items in candidates.items()}
        growth_limits = {name: useful_tokens[name] + min(bases[name], demands[name] - useful_tokens[name])
                         for name in candidates}
        growth_limits["recent_conversation"] = demands["recent_conversation"]
        needed = sum(min(demands[name], growth_limits[name]) for name in bases)
        pool = min(ceiling, max(sum(bases.values()), needed))
        # Protection blocks new business operations, not the evidence needed to
        # author the handoff. Keep the same bounded pool until rotation verifies
        # a fresh note and projects its sealed history prefix.
    remaining, allocations = pool, {}
    # Reserve each section's base before lending; a busy recent tail must not
    # starve a useful historical section of its normal share.
    for name, base in bases.items():
        allocations[name] = min(base, demands[name], remaining)
        remaining -= allocations[name]
    if config.context_utilization_enabled:
        _lend_by_demand(allocations, growth_limits, bases, remaining)
    else:
        for name in bases:
            extra = min(max(0, demands[name] - allocations[name]), remaining)
            allocations[name] += extra
            remaining -= extra
    selected, actual = {}, {}
    for name, items in candidates.items():
        if config.context_utilization_enabled:
            # A large useful card may not fit its share. Its demand must not
            # authorize unrelated small diagnostics to fill the expanded share.
            low_priority = [item for item in items if not item.required and item.priority < _MIN_GROWTH_PRIORITY]
            bounded_low, missing, _ = token_budget.pack(low_priority, token_budget=bases[name])
            omitted.extend({**item, "section": name} for item in missing)
            items = [item for item in items if item.required or item.priority >= _MIN_GROWTH_PRIORITY
                     or item in bounded_low]
        selected[name], missing, meta = token_budget.pack(items, token_budget=allocations[name])
        omitted.extend({**item, "section": name} for item in missing)
        actual[name] = meta["selected_tokens"]
    if config.context_utilization_enabled:
        # Whole cards can miss a fractional share even when other sections have
        # spare capacity. Admit complete useful cards before returning gaps to
        # the conversation; never split source evidence just to fit a share.
        spare = max(0, pool - allocations["recent_conversation"] - sum(actual.values()))
        pending = [(name, item) for name, items in candidates.items() for item in items
                   if item not in selected[name] and item.priority >= _MIN_GROWTH_PRIORITY]
        for name, item in sorted(pending, key=lambda entry: (-entry[1].priority,
                entry[1].estimated_tokens, entry[0], entry[1].item_id)):
            tokens = item.estimated_tokens
            if tokens <= spare and actual[name] + tokens <= growth_limits[name]:
                selected[name].append(item)
                actual[name] += tokens
                spare -= tokens
        selected_keys = {(name, item.item_id) for name, items in selected.items() for item in items}
        omitted = [item for item in omitted if item.get("reason") != "token_budget"
                   or (item.get("section"), item["item_id"]) not in selected_keys]
        allocations.update(actual)
    # Whole cards can leave unusable gaps; the live conversation can use them.
    allocations["recent_conversation"] = max(allocations["recent_conversation"],
        min(demands["recent_conversation"], max(0, pool - sum(actual.values()))))
    return selected, omitted, {
        "policy": "demand-context-sections-v3" if config.context_utilization_enabled else "elastic-context-sections-v1", "base_tokens": bases,
        "protection_projection": protection,
        "recovery_context_preserved": protection and config.context_utilization_enabled,
        "growth_limits": growth_limits,
        "pool_tokens": pool, "fixed_tokens": fixed_tokens,
        "demand_tokens": demands, "allocated_tokens": allocations,
        "selected_memory_tokens": actual,
        "borrowed_tokens": {name: max(0, allocations[name] - base) for name, base in bases.items()},
        "recent_conversation_budget": allocations["recent_conversation"],
    }
