from __future__ import annotations

import http.client
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from minicode.tooling import ToolDefinition, ToolResult
from minicode.tools._validation import validate_contract
from minicode.web_http import (
    _build_safe_opener,
    _ContentDecodeError,
    _is_safe_url,
    _LimitedRedirectHandler,
    _normalize_request_url,
    _read_response_body,
    _validated_address_infos,  # noqa: F401 - preserve the previous fetch helper entry point
)

MAX_CONTENT_LENGTH = 50000
WEB_FETCH_INPUT = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "url": {"type": "string", "minLength": 1, "description": "The public http or https URL to fetch"},
        "max_chars": {"type": "integer", "minimum": 100, "maximum": MAX_CONTENT_LENGTH, "default": 10000,
                      "description": "Maximum characters to return (integer 100-50000, default: 10000)"},
    },
    "required": ["url"],
}
MAX_RESPONSE_BYTES = MAX_CONTENT_LENGTH * 4
def _validate(input_data: dict) -> dict:
    validate_contract(input_data, WEB_FETCH_INPUT)
    url = input_data["url"]
    if not url.startswith(("http://", "https://")):
        raise ValueError("$.url: must start with http:// or https://")
    max_chars = input_data.get("max_chars", 10000)
    return {"url": url, "max_chars": max_chars}


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        _normalize_request_url(url),
        headers={
            "User-Agent": "MiniCode-Python/0.5.0 (local coding assistant)",
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
    )


def _is_transient_disconnect(error: urllib.error.URLError) -> bool:
    reason = error.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(reason, (ssl.SSLError, socket.timeout, TimeoutError, ConnectionResetError, http.client.RemoteDisconnected)):
        return True
    text = str(reason).lower()
    return any(
        marker in text
        for marker in ("unexpected_eof", "eof occurred", "connection reset", "remote end closed", "timed out")
    )


def _network_failure(url: str, error: urllib.error.URLError, attempts: int) -> ToolResult:
    reason = " ".join(str(error.reason).split())[:400]
    lowered = reason.lower()
    if isinstance(error.reason, ssl.SSLError) or any(token in lowered for token in ("ssl", "tls", "unexpected_eof", "eof occurred")):
        code = "WEB_FETCH_TLS_ERROR"
        message = "目标站点在安全连接完成前断开了连接。这通常是目标服务或当前网络链路的问题，不代表 web_fetch 工具不存在。"
    else:
        code = "WEB_FETCH_NETWORK_ERROR"
        message = "无法连接目标网址。请检查网络、稍后重试，或改用另一个可信来源。"
    return ToolResult(
        ok=False,
        failureKind="transient_error" if _is_transient_disconnect(error) else "remote_rejected",
        output="\n".join(
            [
                code,
                message,
                f"原始错误: {reason or error.__class__.__name__}",
                f"URL: {url}",
                f"尝试次数: {attempts}",
            ]
        ),
    )


def _run(input_data: dict, context) -> ToolResult:
    url = input_data["url"]
    max_chars = input_data["max_chars"]
    is_safe, reason = _is_safe_url(url)
    if not is_safe:
        return ToolResult(ok=False, output=f"Security Error: {reason}\nURL: {url}")

    from minicode.tool_recovery import retry_read
    result = retry_read(lambda: _fetch_once(url, max_chars),
        cancellation=getattr(context, "cancellation_event", None), notify=getattr(context, "recovery_observer", None))
    if result.ok and result.toolRecovery:
        result.output += f"\nATTEMPTS: {len(result.toolRecovery['attempts']) + 1}"
    return result


def _fetch_once(url: str, max_chars: int) -> ToolResult:
    from minicode.tool_recovery import retry_after_seconds
    try:
        with _build_safe_opener(_LimitedRedirectHandler()).open(_request(url), timeout=30) as response:
            content_type = response.headers.get("Content-Type", "")
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
            is_html = content_type.split(";", 1)[0].strip().casefold() in {"text/html", "application/xhtml+xml"}
            # HTML headers can contain more CSS/JS than the requested text
            # length. Bound wire bytes independently, then limit readable text.
            byte_limit = MAX_RESPONSE_BYTES if is_html else min(MAX_RESPONSE_BYTES, max_chars * 4)
            raw, raw_truncated = _read_response_body(response, byte_limit)
            try:
                text = raw.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = raw.decode("utf-8", errors="replace")
            if is_html:
                text = _extract_text_from_html(text)
            hostname = (urllib.parse.urlsplit(url).hostname or "").casefold()
            if hostname == "news.google.com" and text.strip().casefold() == "google news":
                return ToolResult(ok=False, output="\n".join([
                    "WEB_FETCH_EMPTY_CONTENT", ("Google News 的文章跳转页没有提供正文。请使用搜索结果中的来源、标题和日期，"
                    "不要重复读取同一个 Google News 地址。"), f"URL: {url}"]))
            text_truncated = len(text) > max_chars
            truncated = raw_truncated or text_truncated
            if text_truncated:
                text = text[:max_chars] + f"\n\n... [Content truncated at {max_chars} chars]"
            elif raw_truncated:
                text += f"\n\n... [Response truncated at {byte_limit} bytes; extracted text may be incomplete]"
            return ToolResult(ok=True, output="\n".join([
                f"URL: {url}", f"CONTENT_TYPE: {content_type}", f"STATUS: {response.status}",
                f"CHARS: {len(text)}", f"TRUNCATED: {'yes' if truncated else 'no'}", "", text]))
    except _ContentDecodeError as error:
        return ToolResult(ok=False, failureKind="unsupported_content", output=(
            "WEB_FETCH_CONTENT_ENCODING_ERROR\n"
            "服务器返回的压缩正文无法解码，不能作为有效网页内容。请换用其他来源。\n"
            f"原因: {error}\nURL: {url}"))
    except urllib.error.HTTPError as error:
        with error:
            return ToolResult(ok=False, output=f"HTTP Error {error.code}: {error.reason}\nURL: {url}",
                failureKind="transient_error" if error.code in {408, 429, 500, 502, 503, 504} else "remote_rejected",
                retryAfterSeconds=retry_after_seconds(error.headers.get("Retry-After")) if error.headers else None)
    except urllib.error.URLError as error:
        return _network_failure(url, error, 1)
    except (TimeoutError, ConnectionError, http.client.RemoteDisconnected) as error:
        return _network_failure(url, urllib.error.URLError(error), 1)
    except Exception as error:  # noqa: BLE001 - retain the read error
        return ToolResult(ok=False, output=f"WEB_FETCH_ERROR\n读取网址时发生错误: {error}\nURL: {url}")


def _extract_text_from_html(payload: str) -> str:
    """Extract compact, readable text from an HTML response."""

    import re
    from html.parser import HTMLParser

    class ReadableText(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.hidden: list[str] = []
            self.parts: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag in {"script", "style", "noscript", "template"}:
                self.hidden.append(tag)

        def handle_endtag(self, tag):
            if self.hidden and tag == self.hidden[-1]:
                self.hidden.pop()

        def handle_data(self, data):
            if not self.hidden:
                self.parts.append(data)

    parser = ReadableText()
    parser.feed(payload)
    parser.close()
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


web_fetch_tool = ToolDefinition(
    name="web_fetch",
    description=(
        "Fetch a known public URL and extract HTML, JSON, or plain text. This is network-backed and can fail "
        "when the target blocks clients, resets TLS, rate-limits, or is offline; a failure is not evidence that "
        "the tool is missing. Prefer current_time for date/time questions instead of calling a time website."
    ),
    input_schema=WEB_FETCH_INPUT,
    validator=_validate,
    run=_run,
)
