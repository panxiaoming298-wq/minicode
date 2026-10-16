"""Shared public-HTTP validation, pinned connections and bounded body decoding.

This module deliberately has no tool-registry dependencies, so HTTP workers
can start promptly and the fetch and search tools share identical protections.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any

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
    deadline = time.monotonic() + timeout if isinstance(timeout, (int, float)) else None
    for family, socktype, proto, _canonname, sockaddr in _validated_address_infos(host, port):
        connection = None
        try:
            connection = socket.socket(family, socktype, proto)
            if timeout is not _DEFAULT_SOCKET_TIMEOUT:
                if timeout is not None and not isinstance(timeout, (int, float)):
                    raise TypeError("timeout must be numeric, None, or the default sentinel")
                remaining = deadline - time.monotonic() if deadline is not None else timeout
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("Connection deadline reached")
                connection.settimeout(remaining)
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
        from minicode.web_transport import proxy_connection_factory
        factory = proxy_connection_factory(request.full_url, secure=False)
        return self.do_open(factory or _SafeHTTPConnection, request)


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        from minicode.web_transport import proxy_connection_factory
        factory = proxy_connection_factory(request.full_url, secure=True)
        return self.do_open(
            factory or _SafeHTTPSConnection,
            request,
            context=self._context,
            check_hostname=self._check_hostname,
        )


def _build_safe_opener(*handlers) -> urllib.request.OpenerDirector:
    """Use only destination-pinning handlers, including configured HTTP proxies."""

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _SafeHTTPHandler(),
        _SafeHTTPSHandler(),
        *handlers,
    )


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


class _ContentDecodeError(ValueError):
    """The HTTP body cannot be presented as usable source text."""


def _read_response_body(response, byte_limit: int) -> tuple[bytes, bool]:
    """Bound both wire bytes and decoded bytes, including concatenated gzip members."""
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip", "x-gzip", "deflate"}:
        raise _ContentDecodeError(f"Unsupported Content-Encoding: {encoding}")
    declared = response.headers.get("Content-Length")
    try:
        declared_truncated = bool(declared) and int(declared) > byte_limit
    except ValueError:
        declared_truncated = False
    raw = response.read(byte_limit + 1)
    wire_truncated = declared_truncated or len(raw) > byte_limit
    raw = raw[:byte_limit]
    if encoding in {"", "identity"}:
        return raw, wire_truncated

    is_gzip = encoding in {"gzip", "x-gzip"}
    output = bytearray()
    # An empty compressed response is malformed too. Never return binary bytes
    # as a successful text result, and never call an unbounded decompress/flush.
    while True:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if is_gzip else zlib.MAX_WBITS)
        try:
            output.extend(decoder.decompress(raw, byte_limit + 1 - len(output)))
        except zlib.error as error:
            raise _ContentDecodeError(f"Invalid {encoding} response: {error}") from error
        if len(output) > byte_limit:
            return bytes(output[:byte_limit]), True
        if not decoder.eof:
            if wire_truncated:
                return bytes(output), True
            raise _ContentDecodeError(f"Incomplete {encoding} response")
        raw = decoder.unused_data
        if not raw:
            return bytes(output), wire_truncated
        if not is_gzip:
            raise _ContentDecodeError("Unexpected bytes after deflate response")
