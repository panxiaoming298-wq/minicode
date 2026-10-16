from __future__ import annotations

import codecs
from email.message import Message
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from minicode.tooling import ToolDefinition, ToolContext, ToolResult
from minicode.tools._validation import validate_fields
from minicode.tools.web_fetch import _LimitedRedirectHandler, _build_safe_opener, _is_safe_url


MAX_HTTP_RESPONSE_BYTES = 1_000_000
MAX_HTTP_BODY_CHARS = 10_000
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _read_bounded(response, limit: int = MAX_HTTP_RESPONSE_BYTES) -> tuple[bytes, bool]:
    payload = response.read(limit + 1)
    declared = response.headers.get("Content-Length")
    if (declared and declared.isdecimal() and len(payload) < min(int(declared), limit + 1)
            and getattr(response, "_method", None) != "HEAD"
            and getattr(response, "status", None) not in {204, 304}):
        raise http.client.IncompleteRead(payload, int(declared) - len(payload))
    return payload[:limit], len(payload) > limit


def _validate_http_request(input_data: dict) -> dict:
    """Validate input for http_request tool."""
    validate_fields(input_data, required=("url",), url=str, method=str, headers=dict,
                    body=(str, dict), timeout=(int, float))
    url = input_data.get("url", "")
    method = input_data.get("method", "GET").upper()
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url is required and must be a non-empty string")
    if method not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
        raise ValueError(f"Invalid method: {method}")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must be an absolute http or https URL")
    parsed.port  # Reject malformed/out-of-range ports before any request.
    if any(ord(char) < 33 or ord(char) == 127 for char in url.strip()):
        raise ValueError("url must not contain unescaped spaces or control characters")
    headers = input_data.get("headers", {})
    for key, value in headers.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                or not isinstance(value, str) or any(c in value for c in "\r\n\0")):
            raise ValueError("headers must have valid HTTP names and string values without CR, LF or NUL")
        try:
            value.encode("latin-1")
        except UnicodeEncodeError as error:
            raise ValueError("header values must be encodable as Latin-1") from error
    if not 0 < input_data.get("timeout", 30) <= 120:
        raise ValueError("timeout must be greater than 0 and at most 120 seconds")
    body = input_data.get("body", "")
    if method in _SAFE_METHODS and body != "":
        raise ValueError(f"{method} does not accept a body in this tool")
    if isinstance(body, dict):
        json.dumps(body, allow_nan=False)
    return {
        "url": url.strip(),
        "method": method,
        "headers": input_data.get("headers", {}),
        "body": input_data.get("body", ""),
        "timeout": input_data.get("timeout", 30),
    }


def _response_result(status: int, headers, payload: bytes, byte_truncated: bool) -> ToolResult:
    """Describe transport success separately from the completeness of its body."""
    content_type = Message()
    content_type["Content-Type"] = headers.get("Content-Type", "text/plain")
    encoding = content_type.get_content_charset() or "utf-8"
    try:
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        content = decoder.decode(payload, final=not byte_truncated)
        if not isinstance(content, str):
            raise ValueError("response encoding is not a text encoding")
    except (LookupError, UnicodeError, ValueError) as error:
        return ToolResult(ok=False, output=f"HTTP {status}: response cannot be decoded as {encoding}: {error}",
                          failureKind="unsupported_content", outputTruncated=byte_truncated)
    # Never pretty-print a known partial document as if it were a complete JSON value.
    if not byte_truncated:
        try:
            content = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
    truncated = byte_truncated or len(content) > MAX_HTTP_BODY_CHARS
    lines = ["--- Response ---", f"Status: {status}", f"Headers: {json.dumps(dict(headers), ensure_ascii=True)}",
             f"BODY_TRUNCATED: {'yes' if truncated else 'no'}", f"BODY_CAPTURED_BYTES: {len(payload)}",
             f"BODY_DISPLAYED_CHARS: {min(len(content), MAX_HTTP_BODY_CHARS)}"]
    if truncated:
        lines.append("Body is incomplete; do not treat this excerpt as the full response. "
                     "Use a supported read/pagination operation to obtain missing content; do not repeat a write request.")
    lines.extend(["", "Body:", content[:MAX_HTTP_BODY_CHARS]])
    failure = None if status < 400 else (
        "transient_error" if status in {408, 429} or status >= 500 else "remote_rejected")
    return ToolResult(ok=status < 400, output="\n".join(lines), outputTruncated=truncated, failureKind=failure)


def _run_http_request(input_data: dict, context: ToolContext) -> ToolResult:
    """Make an HTTP request."""
    url = input_data["url"]
    method = input_data["method"]
    headers = input_data.get("headers", {})
    body = input_data.get("body", "")
    timeout = input_data.get("timeout", 30)
    safe, reason = _is_safe_url(url)
    if not safe:
        return ToolResult(ok=False, output=f"Security error: {reason}", failureKind="policy_denied")
    
    # Build request
    req = urllib.request.Request(url, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    
    if body != "" and method not in _SAFE_METHODS:
        if isinstance(body, dict):
            body = json.dumps(body, allow_nan=False)
            if not req.has_header("Content-type"):
                req.add_header("Content-Type", "application/json")
        req.data = body.encode("utf-8")
    
    try:
        opener = _build_safe_opener(_LimitedRedirectHandler())
        with opener.open(req, timeout=timeout) as response:
            payload, truncated = _read_bounded(response)
            result = _response_result(response.status, response.headers, payload, truncated)
            if not result.ok and method not in _SAFE_METHODS:
                result.ambiguousSideEffect = True
            return result
    
    except urllib.error.HTTPError as e:
        with e:
            try:
                payload, truncated = _read_bounded(e)
                result = _response_result(e.code, e.headers, payload, truncated)
            except (OSError, http.client.HTTPException) as error:
                result = ToolResult(ok=False, output=f"HTTP {e.code}: response body incomplete: {error}",
                                    failureKind="transient_error" if e.code in {408, 429} or e.code >= 500 else "remote_rejected",
                                    outputTruncated=True)
        result.ambiguousSideEffect = method not in _SAFE_METHODS and (e.code == 408 or e.code >= 500)
        return result
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return ToolResult(ok=False, output=f"Network error: {e}", failureKind="transient_error",
                          ambiguousSideEffect=method not in _SAFE_METHODS)
    except Exception as e:
        return ToolResult(ok=False, output=f"Error: {e}", ambiguousSideEffect=method not in _SAFE_METHODS)


http_request_tool = ToolDefinition(
    name="http_request",
    description="Make HTTP requests (GET, POST, PUT, DELETE, etc.). Supports custom headers and JSON body.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "url": {"type": "string", "description": "Request URL"},
            "method": {"type": "string", "description": "HTTP method: GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Request headers as key-value pairs"},
            "body": {"type": ["string", "object"], "description": "Text or JSON object body (for POST, PUT, PATCH, DELETE)"},
            "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 120, "description": "Request timeout in seconds (default: 30)"}
        },
        "required": ["url"]
    },
    validator=_validate_http_request,
    run=_run_http_request,
)
