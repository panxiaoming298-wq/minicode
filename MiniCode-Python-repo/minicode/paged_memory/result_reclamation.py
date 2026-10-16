"""Bounded request-local preview reuse; authoritative sources are never cached here."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import json
import math

from .models import content_hash_text, handoff_contract_hash, stable_json


def projection_key(item):
    return tuple(item[field] for field in ("tool_use_id", "tool_name", "expected_content_hash", "source_event_id"))


def completed_results(messages):
    """Only complete, unambiguous tool batches can surrender optional bodies."""
    pending, batch, completed, seen_calls, seen_results = {}, set(), set(), set(), set()
    for message in messages:
        role, identity = message.get("role"), message.get("toolUseId")
        if role == "assistant_tool_call":
            if not identity or identity in seen_calls:
                return set()
            seen_calls.add(identity)
            pending[identity] = message.get("toolName")
        elif role == "tool_result":
            if (not identity or identity in seen_results or identity not in pending
                    or pending[identity] != message.get("toolName")):
                return set()
            seen_results.add(identity)
            pending.pop(identity)
            batch.add(identity)
            if not pending:
                completed.update(batch)
                batch.clear()
        elif role in {"user", "assistant"} and pending:
            return set()
    return completed


class ResultReclamation:
    """One Runtime owns this optimization; restart simply verifies and rebuilds."""
    MAX_ENTRIES = 128
    MAX_BYTES = 1024 * 1024

    def __init__(self, runtime):
        self.runtime = runtime
        self.scope = None
        self.entries = OrderedDict()
        self.bytes = 0

    def clear(self):
        self.entries.clear()
        self.bytes = 0
        self.scope = None

    def begin(self, packet, user_input):
        state = self.runtime.authority.get_state(tenant_id=self.runtime.tenant_id, task_id=self.runtime.task_id)
        rotation = packet.debug_metadata.get("context_rotation", {})
        criteria = [json.loads(item.text).get("acceptance_criteria", {}) for item in packet.current_step]
        scope = (self.runtime.tenant_id, self.runtime.task_id, self.runtime.run_id, self.runtime.step_namespace,
                 self.runtime.model_name, self.runtime.token_budget.model_name, rotation.get("epoch", 0),
                 rotation.get("status", "disabled"), content_hash_text(user_input),
                 handoff_contract_hash(state) if state is not None else "missing-state",
                 content_hash_text(stable_json(criteria)))
        reset = scope != self.scope
        if reset:
            self.clear()
            self.scope = scope
        return {"policy": "bounded-stable-reclamation-v1", "scope_reset": reset,
                "cache_cost_estimated": False}

    def previous(self, item):
        stored = self.entries.get(projection_key(item))
        return deepcopy(stored[0]) if stored else None

    def invalidate_current(self, results, verified):
        identities = {m.get("toolUseId") for m in results}
        for key in list(self.entries):
            if key[0] in identities and key not in verified:
                _, size = self.entries.pop(key)
                self.bytes -= size

    def select(self, candidates, previous, *, original_tokens, cost, packet, diagnostic):
        """Compare against prior previews so old savings cannot pay for churn."""
        baseline = original_tokens - sum(item["original_tokens"] - cost(item["content"]) for item in previous.values())
        proposed = original_tokens - sum(item["original_tokens"] - cost(item["content"]) for item in candidates)
        changed = [item for item in candidates if projection_key(item) not in previous
                   or item["content"] != previous[projection_key(item)]["content"]]
        changed_before = sum(cost(previous[projection_key(item)]["content"]) if projection_key(item) in previous
                             else item["original_tokens"] for item in changed)
        config = self.runtime.config
        threshold = max(config.tool_result_reclaim_min_tokens,
                        math.ceil(changed_before * config.tool_result_reclaim_min_ratio))
        saving = max(0, baseline - proposed)
        urgent = (packet.debug_metadata.get("context_utilization", {}).get("protection", False)
                  or packet.debug_metadata.get("context_pressure", {}).get("effective_pressure_ratio", 0) >= 1)
        reason = "unchanged" if not changed else "admitted"
        if changed and saving < threshold:
            if urgent and saving > 0:
                reason = "pressure_override"
            else:
                reason = "below_minimum_benefit"
                candidates = list(previous.values())
        used = original_tokens - sum(item["original_tokens"] - cost(item["content"]) for item in candidates)
        reused = sum(projection_key(item) in previous and item["content"] == previous[projection_key(item)]["content"]
                     for item in candidates)
        for item in candidates:
            key = projection_key(item)
            snapshot = {name: deepcopy(item[name]) for name in (
                "tool_use_id", "tool_name", "expected_content_hash", "source_event_id", "content",
                "original_tokens", "projected_tokens", "source_ranges") if name in item}
            size = len(stable_json(snapshot).encode("utf-8"))
            old = self.entries.pop(key, None)
            self.bytes -= old[1] if old else 0
            if size <= self.MAX_BYTES:
                self.entries[key] = (snapshot, size)
                self.bytes += size
            while len(self.entries) > self.MAX_ENTRIES or self.bytes > self.MAX_BYTES:
                _, (_, removed_size) = self.entries.popitem(last=False)
                self.bytes -= removed_size
        diagnostic.update(decision=reason, minimum_savings_tokens=threshold,
            proposed_additional_savings_tokens=saving, additional_savings_tokens=max(0, baseline - used),
            reused_results=reused, changed_results=len(candidates) - reused,
            retained_entries=len(self.entries), retained_bytes=self.bytes)
        return candidates, used
