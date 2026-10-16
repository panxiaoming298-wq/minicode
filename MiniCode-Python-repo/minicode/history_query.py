"""Literal history matching and scope-bound continuation, shared by read tools."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re


SEARCH_PROPERTIES = {
    "query": {"type": "string", "maxLength": 512, "description": "Literal text; omit to browse."},
    "role": {"type": "string", "enum": ["", "user", "assistant", "assistant_progress", "assistant_tool_call", "tool_result"]},
    "tool_name": {"type": "string", "maxLength": 256},
    "case_sensitive": {"type": "boolean", "default": True},
    "recent_first": {"type": "boolean", "default": True},
    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
    "cursor": {"type": "string", "maxLength": 4096},
}

# Stay within native tool metadata and below generic output truncation, so the
# continuation JSON remains intact even when a user increases the token limit.
HISTORY_RESULT_CHAR_LIMIT = 10_000


def validate_search(value, extra_properties=None):
    from minicode.tools._validation import validate_contract
    properties = {**SEARCH_PROPERTIES, **(extra_properties or {})}
    validate_contract(value, {"type": "object", "additionalProperties": False, "properties": properties})
    return {"query": "", "role": "", "tool_name": "", "case_sensitive": True,
            "recent_first": True, "limit": 10, "cursor": "", **value}


def scope_key(*scope, options):
    query = {key: value for key, value in options.items() if key not in {"cursor", "limit"}}
    return hashlib.sha256(json.dumps([*scope, query], ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


def encode_cursor(scope, **position):
    return base64.urlsafe_b64encode(json.dumps({"version": 1, "scope": scope, **position},
        separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(value, scope):
    try:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError
        data = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("scope") != scope:
            raise ValueError
        return data
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("History cursor does not belong to this scope/query or is invalid; restart the search.") from exc


def cursor_integer(data, key, *, minimum=0):
    value = data.get(key)
    if type(value) is not int or value < minimum:
        raise ValueError("Invalid history cursor position; restart the search.")
    return value


def literal_excerpt(text, query, *, case_sensitive=True, context_chars=120):
    """Return original character coordinates, including for Unicode case matches."""
    if not query:
        start, end = 0, 0
    elif case_sensitive:
        start = text.find(query)
        if start < 0:
            return None
        end = start + len(query)
    else:
        match = re.search(re.escape(query), text, flags=re.IGNORECASE)
        if match is None:
            return None
        start, end = match.span()
    left, right = max(0, start - context_chars), min(len(text), end + context_chars)
    return {"excerpt": text[left:right], "excerpt_range": {"unit": "CHAR", "start": left, "end": right},
            "match_range": {"unit": "CHAR", "start": start, "end": end} if query else None,
            "total_chars": len(text)}


def fit_history_candidate(candidate, *, render_page, estimate_tokens, token_limit, has_results):
    """Budget the serialized envelope, including continuation and diagnostics.

    Return None without consuming a source when a subsequent result needs a new
    page. A large first excerpt may shrink to a source reference; the original
    match coordinates remain available for an explicit read.
    """
    def fits(value):
        encoded = json.dumps(render_page(value), ensure_ascii=False)
        return len(encoded) <= HISTORY_RESULT_CHAR_LIMIT - 16 and estimate_tokens(encoded) <= token_limit

    if fits(candidate):
        return candidate
    if has_results:
        return None
    if "excerpt" in candidate:
        match = candidate.get("match_range")
        excerpt = candidate["excerpt"]
        origin = candidate["excerpt_range"]["start"]
        left = match["start"] - origin if match else 0
        right = match["end"] - origin if match else min(32, len(excerpt))
        for start, end in ((left, right), (left, left)):
            compact = {**candidate, "excerpt": excerpt[start:end], "excerpt_truncated": True,
                       "excerpt_range": {"unit": "CHAR", "start": origin + start, "end": origin + end}}
            if fits(compact):
                return compact
    raise ValueError("History result token budget is too small for one source reference and its cursor.")
