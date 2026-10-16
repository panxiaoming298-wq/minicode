"""Bounded public HTTP reads, with pinned destinations through trusted proxies.

Search requests run in short-lived processes so DNS, TLS and slow response
bodies cannot outlive their deadline or a canceled tool invocation. Secrets
travel over stdin, never command arguments or diagnostic output.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


class WebRequestError(RuntimeError):
    def __init__(self, code: str, message: str, *, retry_after: str = "",
                 status: int | None = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.status = status
        self.headers = headers or {}


@dataclass(frozen=True)
class WebResponse:
    text: str
    status: int
    headers: dict[str, str]


def _quota_headers(headers) -> dict[str, str]:
    # Do not send cookies, tokens or arbitrary upstream headers to the parent.
    return {name: str(headers[name])[:1024] for name in (
        "x-ratelimit-limit", "x-ratelimit-policy", "x-ratelimit-remaining", "x-ratelimit-reset",
    ) if headers is not None and headers.get(name) is not None}


_scope: ContextVar[tuple[float | None, object | None]] = ContextVar("web_request_scope", default=(None, None))


@contextmanager
def request_scope(deadline: float, cancellation=None):
    token = _scope.set((deadline, cancellation))
    try:
        yield
    finally:
        _scope.reset(token)


def proxy_for_url(url: str) -> str:
    """Only local operator configuration can select a proxy; URLs cannot."""
    setting = os.environ.get("MINI_CODE_WEB_PROXY", "auto").strip()
    if setting.casefold() in {"direct", "none", "off"}:
        return ""
    if setting.casefold() not in {"", "auto"}:
        selected = setting
    else:
        parsed = urllib.parse.urlsplit(url)
        if urllib.request.proxy_bypass(parsed.hostname or ""):
            return ""
        selected = urllib.request.getproxies().get(parsed.scheme, "")
    if selected and "://" not in selected:
        selected = "http://" + selected
    if selected:
        parsed = urllib.parse.urlsplit(selected)
        if parsed.scheme != "http" or not parsed.hostname:
            raise WebRequestError("proxy_config", "MINI_CODE_WEB_PROXY requires an HTTP CONNECT proxy or 'direct'.")
        try:
            _ = parsed.port  # urllib validates the range when the property is accessed.
        except ValueError as error:
            raise WebRequestError("proxy_config", "Invalid proxy port.") from error
    return selected


def _connect_proxy(connection, proxy_url: str) -> None:
    from minicode.web_http import _validated_address_infos

    # Resolve the target ourselves and CONNECT to that exact public address.
    # The proxy is trusted infrastructure, not an exception for private targets.
    infos = _validated_address_infos(connection.host, connection.port)
    infos.sort(key=lambda info: info[0] != socket.AF_INET)
    target = str(infos[0][4][0])
    proxy = urllib.parse.urlsplit(proxy_url)
    headers = {}
    if proxy.username is not None:
        credentials = urllib.parse.unquote(proxy.username) + ":" + urllib.parse.unquote(proxy.password or "")
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode("ascii")
    connection.set_tunnel(target, connection.port, headers)
    connection.sock = socket.create_connection((proxy.hostname, proxy.port or 80), connection.timeout)
    try:
        connection._tunnel()
    except BaseException:
        connection.close()
        raise


class _ProxyHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, proxy_url: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_url = proxy_url

    def connect(self):
        _connect_proxy(self, self.proxy_url)


class _ProxyHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, proxy_url: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_url = proxy_url

    def connect(self):
        _connect_proxy(self, self.proxy_url)
        try:
            # Certificate validation and SNI use the original DNS name, not the
            # pinned IP used by CONNECT. TLS verification is never disabled.
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.close()
            raise


def proxy_connection_factory(url: str, *, secure: bool):
    proxy = proxy_for_url(url)
    if not proxy:
        return None
    connection = _ProxyHTTPSConnection if secure else _ProxyHTTPConnection
    return lambda *args, **kwargs: connection(*args, proxy_url=proxy, **kwargs)


def request_text(url: str, *, headers: dict[str, str] | None = None,
                 body: dict | None = None, timeout: float = 10, max_bytes: int = 2_000_000) -> str:
    return request_response(url, headers=headers, body=body, timeout=timeout, max_bytes=max_bytes).text


def request_response(url: str, *, headers: dict[str, str] | None = None,
                     body: dict | None = None, timeout: float = 10, max_bytes: int = 2_000_000) -> WebResponse:
    deadline, cancellation = _scope.get()
    expires = min(deadline or float("inf"), time.monotonic() + timeout)
    if cancellation is not None and cancellation.is_set():
        raise WebRequestError("canceled", "Search canceled before the request.")
    if expires <= time.monotonic():
        raise WebRequestError("timeout", "Search request deadline reached.")
    payload = json.dumps({"url": url, "headers": headers or {}, "body": body,
                          "timeout": timeout, "max_bytes": max_bytes}).encode("utf-8")
    process = subprocess.Popen(
        [sys.executable, "-B", "-m", "minicode.web_transport"],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    first = True
    try:
        while True:
            if cancellation is not None and cancellation.is_set():
                raise WebRequestError("canceled", "Search canceled; the HTTP process was stopped.")
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise WebRequestError("timeout", "Search request timed out; the HTTP process was stopped.")
            try:
                output, _ = process.communicate(payload if first else None, timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                first = False
        try:
            receipt = json.loads(output)
        except (ValueError, UnicodeError) as error:
            raise WebRequestError("invalid_response", "HTTP worker returned an invalid response.") from error
        if not receipt.get("ok"):
            raise WebRequestError(receipt.get("code", "network"), receipt.get("error", "Request failed."),
                                  retry_after=receipt.get("retry_after", ""), status=receipt.get("status"),
                                  headers=receipt.get("headers", {}))
        return WebResponse(receipt["text"], receipt["status"], receipt.get("headers", {}))
    finally:
        if process.poll() is None:
            process.kill()
        # communicate joins Windows pipe-reader threads as well as the child.
        process.communicate()


def _worker_request(data: dict) -> dict:
    from minicode.web_http import (
        _build_safe_opener,
        _ContentDecodeError,
        _is_safe_url,
        _LimitedRedirectHandler,
        _normalize_request_url,
        _read_response_body,
    )

    safe, reason = _is_safe_url(data["url"])
    if not safe:
        return {"ok": False, "code": "blocked", "error": reason}

    class Redirects(_LimitedRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, new_url):
            # Search API credentials must not follow a redirect, even to a
            # sibling host. Public unauthenticated GETs retain safe redirects.
            if data.get("body") is not None or any(
                key.lower() in {"authorization", "x-api-key", "x-subscription-token"}
                for key in data.get("headers", {})
            ):
                raise WebRequestError("redirect", "Authenticated API request was redirected; check the provider endpoint.")
            return super().redirect_request(request, fp, code, message, headers, new_url)

    headers = {"User-Agent": "MiniCode-Python/0.5.0 (local coding assistant)",
               "Accept-Encoding": "gzip, deflate", **data.get("headers", {})}
    body = data.get("body")
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(_normalize_request_url(data["url"]), headers=headers,
        data=json.dumps(body).encode("utf-8") if body is not None else None)
    try:
        with _build_safe_opener(Redirects()).open(request, timeout=data["timeout"]) as response:
            raw, truncated = _read_response_body(response, data["max_bytes"])
            if truncated:
                raise WebRequestError("response_limit", "Search response exceeded the size limit.")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return {"ok": True, "text": text, "status": response.status, "headers": _quota_headers(response.headers)}
    except urllib.error.HTTPError as error:
        with error:
            code = ("authentication" if error.code in {401, 403} else
                    "rate_limited" if error.code == 429 else "http_error")
            return {"ok": False, "code": code, "error": f"HTTP {error.code}",
                    "status": error.code, "headers": _quota_headers(error.headers),
                    "retry_after": error.headers.get("Retry-After", "") if error.headers else ""}
    except WebRequestError as error:
        return {"ok": False, "code": error.code, "error": str(error)}
    except _ContentDecodeError:
        return {"ok": False, "code": "invalid_response", "error": "Invalid compressed response."}
    except Exception as error:  # noqa: BLE001 - subprocess boundary returns sanitized diagnostics
        reason = getattr(error, "reason", error)
        code = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "network"
        # Do not serialize request headers, proxy credentials, or arbitrary
        # server error bodies into a model-visible diagnostic.
        return {"ok": False, "code": code, "error": f"{type(reason).__name__}: connection failed"}


if __name__ == "__main__":
    # Keep exception identity shared with the safe opener's lazily imported
    # transport helpers when this file is executed with -m.
    from minicode.web_transport import _worker_request as execute_request
    request_data = json.loads(sys.stdin.buffer.read(100_000))
    sys.stdout.buffer.write(json.dumps(execute_request(request_data), ensure_ascii=True).encode("utf-8"))
