"""Request input measurement, independent of cumulative spending reservations.

Provider observations may anchor an unchanged request prefix. Only hashes and
numeric measurements are retained; a different route, header or prefix starts
with a fresh local measurement. Generation limits are never input tokens.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict

POLICY = "request-context-input-v2"
_GENERATION = frozenset({"max_tokens", "max_completion_tokens", "temperature", "top_p",
    "stream", "stream_options", "frequency_penalty", "presence_penalty", "stop", "seed"})
_FIELDS = _GENERATION | {"model", "messages", "system", "tools", "tool_choice", "thinking",
    "reasoning_effort", "response_format", "parallel_tool_calls", "enable_thinking", "n"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def usage_anchor(diagnostic, usage):
    """Accept a complete provider observation of this measured request only."""
    if (not isinstance(diagnostic, dict) or diagnostic.get("policy") != POLICY
            or not diagnostic.get("countable") or usage.get("tokens_known") is not True
            or type(usage.get("input_tokens")) is not int or usage["input_tokens"] <= 0
            or usage.get("cost_source") == "request_rejected"):
        return None
    return {**diagnostic, "provider_input_tokens": usage["input_tokens"]}


class RequestContextMeter:
    def __init__(self, *, provider, endpoint, protocol="openai", previous=None):
        self.provider, self.endpoint, self.protocol = provider, endpoint, protocol
        self.previous = previous
        self.diagnostic = {}
        self._cache = OrderedDict()

    def _measure(self, serialized):
        from minicode.context_usage import estimate_request
        from minicode.image_input import image_reservation

        body = json.loads(serialized)
        local = estimate_request(body, provider=self.provider, endpoint=self.endpoint)
        base = local["estimated_tokens"]
        messages = body.get("messages", [])
        header = _hash({"provider": self.provider, "endpoint": self.endpoint.rstrip("/"),
            "protocol": self.protocol, "header": {k: v for k, v in body.items() if k != "messages"}})
        countable = (not local["has_unmeasured_media"] and not set(body) - _FIELDS
                     and isinstance(messages, list) and all(isinstance(m, dict) for m in messages))
        local_guard = base
        if not countable or local["method"] == "text_heuristic":
            # A characters-per-token heuristic is not a capacity bound: opaque
            # IDs, Base64 and emoji can exceed it several times over. Keep the
            # byte bound for input without a supported tokenizer.
            try:
                priced, images, _ = image_reservation(body, self.endpoint)
            except ValueError:
                priced, images = body, 0
            local_guard = max(base, len(_json(priced).encode("utf-8")) + 256 + images)
        estimate, method = base, local["method"]
        guarded_input = local_guard
        previous = self.previous or {}
        count = previous.get("message_count")
        if (countable and previous.get("policy") == POLICY and previous.get("countable")
                and previous.get("header_fingerprint") == header
                and type(count) is int and 0 <= count <= len(messages)
                and previous.get("messages_fingerprint") == _hash(messages[:count])
                and type(previous.get("local_input_tokens")) is int
                and previous.get("local_method") == local["method"]
                and type(previous.get("local_guard_tokens")) is int
                and local_guard >= previous["local_guard_tokens"]
                and type(previous.get("provider_input_tokens")) is int
                and previous["provider_input_tokens"] >= 0
                and base >= previous["local_input_tokens"]):
            estimate = previous["provider_input_tokens"] + base - previous["local_input_tokens"]
            guarded_input = previous["provider_input_tokens"] + local_guard - previous["local_guard_tokens"]
            method = "provider_prefix_delta"
        # Budget uncertainty is explicit, separate from the displayed estimate.
        margin = max(1024, math.ceil(estimate * .10))
        guard = max(estimate, guarded_input) + margin
        margin = guard - estimate
        return {"policy": POLICY, "method": method, "estimated_input_tokens": estimate,
            "guard_input_tokens": guard, "margin_tokens": margin,
            "local_input_tokens": base, "countable": countable,
            "local_guard_tokens": local_guard, "local_method": local["method"],
            "header_fingerprint": header, "messages_fingerprint": _hash(messages),
            "message_count": len(messages), "exact_provider_count": False}

    def measure(self, body):
        serialized = _json({k: v for k, v in body.items() if k not in _GENERATION})
        if serialized not in self._cache:
            self._cache[serialized] = self._measure(serialized)
            if len(self._cache) > 4:
                self._cache.popitem(last=False)
        self._cache.move_to_end(serialized)
        self.diagnostic = dict(self._cache[serialized])
        return self.diagnostic["guard_input_tokens"]
