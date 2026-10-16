"""Route-specific conservative text admission; provider usage remains authoritative."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import math
import re
from pathlib import Path

from minicode.runtime_budget import official_deepseek_flash

POLICY = "official-deepseek-tokenized-json-v1"
TOKENIZER_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"
TOKENIZER_PATH = Path(__file__).resolve().parents[1] / "Package/AgentRuntime/Config/Tokenizers/deepseek-v4.json"
_BODY_KEYS = frozenset({"model", "messages", "tools", "tool_choice", "stream", "stream_options",
    "temperature", "top_p", "max_tokens", "frequency_penalty", "presence_penalty", "thinking",
    "reasoning_effort", "response_format", "parallel_tool_calls"})


def _read_tokenizer(path):
    from tokenizers import Tokenizer
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("official tokenizer identity changed")
    data = json.loads(raw)
    return Tokenizer.from_str(raw.decode("utf-8")), re.compile("|".join(
        re.escape(token["content"]) for token in data.get("added_tokens", ()) if token.get("special")))


@lru_cache(maxsize=1)
def _tokenizer():
    return _read_tokenizer(TOKENIZER_PATH)


class RequestInputEstimator:
    """A measured estimate with margin, never an exact server token-count claim.

    Count the WHOLE JSON request, including schema/history/reasoning/escapes,
    then add 25%, 1024 base tokens, 64/message and 128/tool for framing drift.
    The official tokenizer is a pinned data file, never downloaded or executed
    during a request. All unsupported cases keep the legacy byte estimate.
    """
    def __init__(self, *, provider: str, endpoint: str, disabled=lambda: False):
        self.provider, self.endpoint, self.disabled = provider, endpoint, disabled
        self.diagnostic = {}

    @lru_cache(maxsize=8)
    def _count(self, text):
        tokenizer, special = _tokenizer()
        if special.search(text):
            raise ValueError("special-token text needs conservative byte accounting")
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    def measure(self, body):
        from minicode.image_input import image_reservation
        try:
            body, image_tokens, image_count = image_reservation(body, self.endpoint)
        except ValueError:
            image_tokens, image_count = 0, 0  # Legacy unmeasured media; bounded dispatch rejects it.
        # After reserving pixels, collapse text blocks for the pinned text tokenizer.
        for message in body.get("messages", []) if image_count else []:
            content = message.get("content")
            if isinstance(content, list) and all(b.get("type") == "text" for b in content):
                message["content"] = "\n".join(b.get("text", "") for b in content)
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        legacy = len(text.encode("utf-8")) + 256 + image_tokens
        self.diagnostic = {"policy": "utf8-bytes-v1", "input_reservation": legacy,
                           "byte_estimate": legacy, "provider_usage_authoritative": True,
                           "image_count": image_count, "image_token_reservation": image_tokens}
        if (self.provider != "deepseek" or not official_deepseek_flash(body.get("model", ""), self.endpoint)
                or self.disabled() or set(body) - _BODY_KEYS or legacy > 2_000_000
                or not isinstance(body.get("tools", []), list)
                or not isinstance(body.get("messages"), list)
                or any(not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant", "tool"}
                    or not isinstance(m.get("content", ""), (str, type(None))) for m in body["messages"])):
            return legacy
        try:
            # Output cap is not input text. Normalize this generation control so
            # reducing its atomic grant cannot change the input token estimate.
            count = self._count(json.dumps({**body, **({"max_tokens": 0} if "max_tokens" in body else {})},
                ensure_ascii=False, separators=(",", ":")))
        except Exception as error:  # Missing/invalid optional data or runtime never reduces the guard.
            self.diagnostic["fallback_reason"] = type(error).__name__
            return legacy
        framing = 1024 + 64 * len(body["messages"]) + 128 * len(body.get("tools", []))
        estimate = min(legacy, math.ceil(count * 1.25) + framing + image_tokens)
        self.diagnostic = {**self.diagnostic, "policy": POLICY, "input_reservation": estimate,
            "tokenizer_sha256": TOKENIZER_SHA256, "tokenized_json": count, "framing_margin": framing,
            "multiplier": 1.25, "exact_provider_count": False}
        return estimate


def estimate_was_exceeded(call):
    usage = call.get("usage") or {}
    diagnostic = (usage.get("prepared_request") or {}).get("input_estimate") or {}
    return (diagnostic.get("policy") == POLICY and usage.get("tokens_known") is True
            and usage.get("input_tokens", 0) > call.get("input_reservation", float("inf")))
