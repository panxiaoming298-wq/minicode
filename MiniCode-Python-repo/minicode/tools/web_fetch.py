from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from minicode.tooling import ToolDefinition, ToolResult

MAX_CONTENT_LENGTH = 50000
MAX_RESPONSE_BYTES = MAX_CONTENT_LENGTH * 4
MAX_REDIRECTS = 5
_DEFAULT_SOCKET_TIMEOUT: object = getattr(
    socket, "_GLOBAL_DEFAULT_TIMEOUT", object()
)


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Reject malformed URLs and numeric non-public network targets."""

    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            return False, "Only http and https URLs are allowed"
        hostname = parsed.hostname
        if not hostname:
            return False, "Invalid URL: no hostname"
        if parsed.username is not None or parsed.password is not None:
            return False, "Credentials in URLs are not allowed"

        normalized_hostname = hostname.casefold().rstrip(".")
        if normalized_hostname == "localhost" or normalized_hostname.endswith(".localhost"):
            return False, f"Access to internal addresses blocked: {hostname}"

        numeric_addresses: list[str] = []
        try:
            numeric_addresses.append(str(ipaddress.ip_address(normalized_hostname)))
        except ValueError:
            try:
                numeric_addresses.extend(
                    str(info[4][0])
                    for info in socket.getaddrinfo(
                        normalized_hostname,
                        0,
                        type=socket.SOCK_STREAM,
                        flags=socket.AI_NUMERICHOST,
                    )
                )
            except socket.gaierror:
                # A normal hostname is validated again after DNS resolution by
                # _safe_create_connection, immediately before the socket opens.
                pass

        for address in numeric_addresses:
            if not ipaddress.ip_address(address.split("%", 1)[0]).is_global:
                return False, f"Access to internal addresses blocked: {hostname}"
        return True, "OK"
    except Exception as error:  # noqa: BLE001 - validation must fail closed
        return False, f"URL validation failed: {error}"


def _validated_address_infos(host: str, port: int) -> list[tuple[Any, ...]]:
    """Resolve a host and return only when every address is globally routable."""

    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"Unable to resolve host: {host}")
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if not ipaddress.ip_address(address).is_global:
            raise OSError(f"Access to internal addresses blocked: {host} resolved to {address}")
    return infos


def _safe_create_connection(
    address: tuple[str, int],
    timeout: object = _DEFAULT_SOCKET_TIMEOUT,
    source_address: tuple[str, int] | None = None,
    *,
    all_errors: bool = False,
) -> socket.socket:
    """Connect to one of the exact public IPs validated for this request."""

    host, port = address
    errors: list[OSError] = []
    for family, socktype, proto, _canonname, sockaddr in _validated_address_infos(host, port):
        connection = None
        try:
            connection = socket.socket(family, socktype, proto)
            if timeout is not _DEFAULT_SOCKET_TIMEOUT:
                if timeout is not None and not isinstance(timeout, (int, float)):
                    raise TypeError("timeout must be numeric, None, or the default sentinel")
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as error:
            errors.append(error)
            if connection is not None:
                connection.close()
    if not errors:
        raise OSError(f"Unable to connect to host: {host}")
    if all_errors and len(errors) > 1:
        raise ExceptionGroup("create_connection failed", errors)
    raise errors[-1]


class _SafeHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _safe_create_connection


class _SafeHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _safe_create_connection


class _SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_SafeHTTPConnection, request)


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(
            _SafeHTTPSConnection,
            request,
            context=self._context,
            check_hostname=self._check_hostname,
        )


def _build_safe_opener(*handlers) -> urllib.request.OpenerDirector:
    """Build an opener that never inherits ambient proxies and validates sockets."""

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _SafeHTTPHandler(),
        _SafeHTTPSHandler(),
        *handlers,
    )


def _validate(input_data: dict) -> dict:
    url = input_data.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("url is required")
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    max_chars = int(input_data.get("max_chars", 10000))
    if max_chars < 100 or max_chars > MAX_CONTENT_LENGTH:
        raise ValueError(f"max_chars must be between 100 and {MAX_CONTENT_LENGTH}")
    return {"url": url, "max_chars": max_chars}


class _LimitedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        self.redirect_count = 0

    def redirect_request(self, request, fp, code, message, headers, new_url):
        self.redirect_count += 1
        if self.redirect_count > MAX_REDIRECTS:
            raise urllib.error.HTTPError(
                request.full_url,
                code,
                f"Too many redirects (>{MAX_REDIRECTS})",
                headers,
                fp,
            )
        is_safe, reason = _is_safe_url(new_url)
        if not is_safe:
            raise urllib.error.HTTPError(request.full_url, 403, reason, headers, fp)
        return super().redirect_request(request, fp, code, message, headers, new_url)


def _normalize_request_url(url: str) -> str:
    """Encode Unicode paths/queries while keeping an already-escaped URL intact."""

    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname or ""
    ascii_hostname = hostname.encode("idna").decode("ascii")
    if ":" in ascii_hostname and not ascii_hostname.startswith("["):
        ascii_hostname = f"[{ascii_hostname}]"
    userinfo = ""
    if parsed.username is not None:
        userinfo = urllib.parse.quote(parsed.username, safe="")
        if parsed.password is not None:
            userinfo += ":" + urllib.parse.quote(parsed.password, safe="")
        userinfo += "@"
    port = f":{parsed.port}" if parsed.port is not None else ""
    netloc = f"{userinfo}{ascii_hostname}{port}"
    path = urllib.parse.quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parsed.query, safe="=&?/%:+,;@!$'()*-._~")
    fragment = urllib.parse.quote(parsed.fragment, safe="?/%:+,;@!$&'()*=-._~")
    return urllib.parse.urlunsplit((parsed.scheme, netloc, path, query, fragment))


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        _normalize_request_url(url),
        headers={
            "User-Agent": "MiniCode-Python/0.5.0 (local coding assistant)",
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
        },
    )


def _is_transient_disconnect(error: urllib.error.URLError) -> bool:
    reason = error.reason
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

    opener = _build_safe_opener(_LimitedRedirectHandler())
    attempts = 0
    while attempts < 2:
        attempts += 1
        try:
            with opener.open(_request(url), timeout=30) as response:
                content_type = response.headers.get("Content-Type", "")
                charset = "utf-8"
                if "charset=" in content_type:
                    charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()

                byte_limit = min(MAX_RESPONSE_BYTES, max_chars * 4)
                declared_length = response.headers.get("Content-Length")
                declared_truncated = False
                if declared_length:
                    try:
                        declared_truncated = int(declared_length) > byte_limit
                    except ValueError:
                        pass
                raw = response.read(byte_limit + 1)
                raw_truncated = declared_truncated or len(raw) > byte_limit
                raw = raw[:byte_limit]
                try:
                    text = raw.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    text = raw.decode("utf-8", errors="replace")
                if "text/html" in content_type:
                    text = _extract_text_from_html(text)

                hostname = (urllib.parse.urlsplit(url).hostname or "").casefold()
                if hostname == "news.google.com" and text.strip().casefold() == "google news":
                    return ToolResult(
                        ok=False,
                        output="\n".join(
                            [
                                "WEB_FETCH_EMPTY_CONTENT",
                                "Google News 的文章跳转页没有提供正文。请使用搜索结果中的来源、标题和日期，"
                                "不要重复读取同一个 Google News 地址。",
                                f"URL: {url}",
                            ]
                        ),
                    )

                truncated = raw_truncated or len(text) > max_chars
                if truncated:
                    text = text[:max_chars] + f"\n\n... [Content truncated at {max_chars} chars]"
                header_lines = [
                    f"URL: {url}",
                    f"CONTENT_TYPE: {content_type}",
                    f"STATUS: {response.status}",
                    f"CHARS: {len(text)}",
                    f"TRUNCATED: {'yes' if truncated else 'no'}",
                ]
                if attempts > 1:
                    header_lines.append(f"ATTEMPTS: {attempts}")
                return ToolResult(ok=True, output="\n".join([*header_lines, "", text]))
        except urllib.error.HTTPError as error:
            return ToolResult(ok=False, output=f"HTTP Error {error.code}: {error.reason}\nURL: {url}")
        except urllib.error.URLError as error:
            if attempts < 2 and _is_transient_disconnect(error):
                continue
            return _network_failure(url, error, attempts)
        except Exception as error:  # noqa: BLE001 - return tool evidence instead of crashing the turn
            return ToolResult(ok=False, output=f"WEB_FETCH_ERROR\n读取网址时发生错误: {error}\nURL: {url}")

    return ToolResult(ok=False, output=f"WEB_FETCH_ERROR\n读取网址失败。\nURL: {url}")


def _extract_text_from_html(payload: str) -> str:
    """Extract compact, readable text from an HTML response."""

    import html
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", payload, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


web_fetch_tool = ToolDefinition(
    name="web_fetch",
    description=(
        "Fetch a known public URL and extract HTML, JSON, or plain text. This is network-backed and can fail "
        "when the target blocks clients, resets TLS, rate-limits, or is offline; a failure is not evidence that "
        "the tool is missing. Prefer current_time for date/time questions instead of calling a time website."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The public URL to fetch"},
            "max_chars": {"type": "number", "description": "Maximum characters to return (default: 10000)"},
        },
        "required": ["url"],
    },
    validator=_validate,
    run=_run,
)
