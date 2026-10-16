"""Bounded, credential-free diagnostics for errors received inside a model stream."""
from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime


def safe_identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value) else None


def retry_after_seconds(value):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(str(value)).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return max(0.0, delay) if math.isfinite(delay) else 0.0


class ProviderStreamError(RuntimeError):
    """A provider failure, never permission to replay an executed tool batch.

    Raw provider text can echo prompts or credentials. Retain controlled labels
    and opaque IDs here; the separately bounded raw-response capture owns bytes.
    """

    def __init__(self, event, *, headers=None, partial=False, transport=False, stage="stream"):
        self.stage = stage
        event = event if isinstance(event, Mapping) else {}
        error = event.get("error", event)
        error = error if isinstance(error, Mapping) else {"message": str(error)}
        self.error_type = safe_identifier(error.get("type"))
        self.code = safe_identifier(error.get("code"))
        status = error.get("status") or error.get("status_code")
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        labels = " ".join(str(error.get(k, "")).lower() for k in ("code", "type", "message"))
        self.classification = "unknown"
        # Specific quota/authentication evidence overrides a generic envelope
        # such as invalid_request_error or upstream_error. "Insufficient
        # capacity" is an availability error, not an account-balance verdict.
        if re.search(r"\b(?:insufficient[ _-]+(?:quota|balance|credits?)|quota[ _-]+(?:exceeded|exhausted)|"
                     r"credit balance|billing[ _-]+(?:hard[ _-]+limit|not[ _-]+active))\b", labels) or self.status == 402:
            self.classification = "quota"
        elif (any(s in labels for s in ("authentication", "unauthorized", "permission", "forbidden", "access to model denied"))
              or re.search(r"\binvalid[ _-]+api[ _-]+key\b", labels) or self.status in {401, 403}):
            self.classification = "authentication"
        elif any(s in labels for s in ("model_not_found", "invalid_request", "invalid_parameter", "context_length", "context length", "bad request")) or self.status in {400, 404, 405, 413, 422}:
            self.classification = "configuration"
        elif any(s in labels for s in ("rate_limit", "rate limit", "too many requests")) or self.status == 429:
            self.classification = "rate_limit"
        elif transport or any(s in labels for s in ("upstream_error", "upstream sent an error", "server_error", "internal_error", "api_error", "overloaded", "temporarily unavailable")) or self.status in {500, 502, 503, 504, 529}:
            self.classification = "upstream"
        self.retryable = self.classification in {"rate_limit", "upstream"}
        self.request_id = safe_identifier(event.get("request_id")) or safe_identifier(error.get("request_id"))
        headers = headers or {}
        for key in ("x-request-id", "request-id", "x-dashscope-request-id"):
            self.request_id = self.request_id or safe_identifier(headers.get(key))
        self.retry_after = retry_after_seconds(headers.get("Retry-After", error.get("retry_after")))
        self.partial = bool(partial)
        self.attempts = 1
        self.retries = 0
        self.model = None
        super().__init__("provider stream failed")

    def diagnostic(self):
        return {"stage": self.stage, "classification": self.classification,
                "type": self.error_type, "code": self.code, "status": self.status,
                "request_id": self.request_id, "model": self.model,
                "partial_output": self.partial, "retryable": self.retryable,
                "retry_after_seconds": self.retry_after,
                "http_attempts": self.attempts, "retries": self.retries}

    def __str__(self):
        return "Provider stream error: " + json.dumps(self.diagnostic(), ensure_ascii=False, separators=(",", ":"))
