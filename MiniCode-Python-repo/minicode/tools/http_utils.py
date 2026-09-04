from __future__ import annotations

import json
import urllib.error
import urllib.request

from minicode.tooling import ToolDefinition, ToolContext, ToolResult
from minicode.tools.web_fetch import _LimitedRedirectHandler, _build_safe_opener, _is_safe_url


MAX_HTTP_RESPONSE_BYTES = 1_000_000


def _read_bounded(response, limit: int = MAX_HTTP_RESPONSE_BYTES) -> tuple[bytes, bool]:
    payload = response.read(limit + 1)
    return payload[:limit], len(payload) > limit


def _validate_http_request(input_data: dict) -> dict:
    """Validate input for http_request tool."""
    url = input_data.get("url", "")
    method = input_data.get("method", "GET").upper()
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url is required and must be a non-empty string")
    if method not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
        raise ValueError(f"Invalid method: {method}")
    return {
        "url": url.strip(),
        "method": method,
        "headers": input_data.get("headers", {}),
        "body": input_data.get("body", ""),
        "timeout": input_data.get("timeout", 30),
    }


def _run_http_request(input_data: dict, context: ToolContext) -> ToolResult:
    """Make an HTTP request."""
    url = input_data["url"]
    method = input_data["method"]
    headers = input_data.get("headers", {})
    body = input_data.get("body", "")
    timeout = input_data.get("timeout", 30)
    safe, reason = _is_safe_url(url)
    if not safe:
        return ToolResult(ok=False, output=f"Security error: {reason}")
    
    # Build request
    req = urllib.request.Request(url, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    
    if body and method in {"POST", "PUT", "PATCH"}:
        if isinstance(body, dict):
            body = json.dumps(body)
            req.add_header("Content-Type", "application/json")
        req.data = body.encode("utf-8")
    
    try:
        opener = _build_safe_opener(_LimitedRedirectHandler())
        with opener.open(req, timeout=timeout) as response:
            status = response.status
            response_headers = dict(response.headers)
            payload, truncated = _read_bounded(response)
            content = payload.decode("utf-8", errors="replace")
            
            # Try to parse JSON
            try:
                content = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
            except (json.JSONDecodeError, UnicodeDecodeError):
                response_headers.get("Content-Type", "text/plain")
            
            lines = [
                "--- Response ---",
                f"Status: {status}",
                f"Headers: {json.dumps(response_headers, indent=2)}",
                "",
                "Body:",
                content[:10000],  # Limit output
            ]
            if truncated:
                lines.append(f"\n... [response truncated at {MAX_HTTP_RESPONSE_BYTES} bytes]")
            
            return ToolResult(ok=True, output="\n".join(lines))
    
    except urllib.error.HTTPError as e:
        payload, truncated = _read_bounded(e)
        suffix = f"\n... [response truncated at {MAX_HTTP_RESPONSE_BYTES} bytes]" if truncated else ""
        return ToolResult(ok=False, output=f"HTTP {e.code}: {e.reason}\n{payload.decode('utf-8', errors='replace')}{suffix}")
    except urllib.error.URLError as e:
        return ToolResult(ok=False, output=f"Network error: {e.reason}")
    except Exception as e:
        return ToolResult(ok=False, output=f"Error: {e}")


http_request_tool = ToolDefinition(
    name="http_request",
    description="Make HTTP requests (GET, POST, PUT, DELETE, etc.). Supports custom headers and JSON body.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Request URL"},
            "method": {"type": "string", "description": "HTTP method: GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS"},
            "headers": {"type": "object", "description": "Request headers as key-value pairs"},
            "body": {"type": "string", "description": "Request body (for POST, PUT, PATCH)"},
            "timeout": {"type": "number", "description": "Request timeout in seconds (default: 30)"}
        },
        "required": ["url"]
    },
    validator=_validate_http_request,
    run=_run_http_request,
)
