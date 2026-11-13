"""One-shot HTTPS on a prevalidated numeric IP; never uses proxies or a pool."""

import http.client
import io
import ipaddress
import socket
import ssl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from urllib.parse import urlsplit


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _allow_test_loopback(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int
) -> bool:
    """Tests may replace this private seam; production never accepts local targets."""
    return False


class PinnedTransportError(ValueError):
    """Sanitized transport boundary failure."""


class _DeadlineReader(io.RawIOBase):
    """Bound every TLS read, including status and header reads."""

    def __init__(
        self,
        sock: ssl.SSLSocket,
        deadline: float,
        read_timeout: float,
        header_limit: int,
    ):
        super().__init__()
        self._sock = sock
        self._deadline = deadline
        self._read_timeout = read_timeout
        self.header_remaining: int | None = header_limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise PinnedTransportError("download_deadline_exceeded")
        self._sock.settimeout(min(remaining, self._read_timeout))
        if self.header_remaining is not None and self.header_remaining <= 0:
            raise PinnedTransportError("download_headers_too_large")
        target = memoryview(buffer)
        if self.header_remaining is not None:
            target = target[: self.header_remaining]
        try:
            received = self._sock.recv_into(target)
            if self.header_remaining is not None:
                self.header_remaining -= received
            return received
        except TimeoutError:
            reason = (
                "download_deadline_exceeded"
                if remaining <= self._read_timeout
                else "download_read_timeout"
            )
            raise PinnedTransportError(reason) from None


class _DeadlineSocketView:
    def __init__(
        self,
        sock: ssl.SSLSocket,
        deadline: float,
        read_timeout: float,
        header_limit: int,
    ):
        self.reader = _DeadlineReader(sock, deadline, read_timeout, header_limit)

    def makefile(self, _mode: str) -> io.BufferedReader:
        return io.BufferedReader(self.reader)


class PinnedResponse:
    def __init__(
        self,
        response: http.client.HTTPResponse,
        sock: ssl.SSLSocket,
        peer: str,
        hostname: str,
        deadline: float,
        read_timeout: float,
    ):
        self._response = response
        self._sock = sock
        self.peer = peer
        self.hostname = hostname
        self.deadline = deadline
        self.read_timeout = read_timeout
        self.status_code = response.status
        self.headers = response.headers

    def iter_raw(self, *, chunk_size: int) -> Iterator[bytes]:
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise PinnedTransportError("download_deadline_exceeded")
            self._sock.settimeout(min(remaining, self.read_timeout))
            chunk = self._response.read1(chunk_size)
            if not chunk:
                return
            yield chunk


@contextmanager
def open_pinned(
    url: str, hostname: str, addresses: tuple[str, ...], limits: Any, *, port: int = 443
) -> Iterator[PinnedResponse]:
    """Connect to the first validated numeric IP, verify peer, TLS SNI and hostname.

    Port override is an internal seam for isolated tests; the normal caller uses 443.
    No fallback or retries: a failed connection is a single failed download action.
    """
    if not addresses or not 1 <= port <= 65535:
        raise PinnedTransportError("download_peer_unverified")
    if limits.max_header_bytes is None or limits.max_header_bytes <= 0:
        raise PinnedTransportError("download_header_budget_required")
    address = ipaddress.ip_address(addresses[0])
    if any(
        (
            not ipaddress.ip_address(item).is_global
            or ipaddress.ip_address(item).is_multicast
            or ipaddress.ip_address(item).is_reserved
            or getattr(ipaddress.ip_address(item), "ipv4_mapped", None) is not None
        )
        and not _allow_test_loopback(ipaddress.ip_address(item), port)
        for item in addresses
    ) or (port != 443 and not _allow_test_loopback(address, port)):
        raise PinnedTransportError("download_peer_unverified")
    deadline = time.monotonic() + limits.total_seconds
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    raw = socket.socket(family, socket.SOCK_STREAM)
    tls: ssl.SSLSocket | None = None
    response: http.client.HTTPResponse | None = None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PinnedTransportError("download_deadline_exceeded")
        raw.settimeout(min(limits.connect_seconds, remaining))
        try:
            raw.connect((str(address), port))
        except TimeoutError:
            raise PinnedTransportError(
                "download_deadline_exceeded"
                if remaining <= limits.connect_seconds
                else "download_connect_timeout"
            ) from None
        except OSError:
            raise PinnedTransportError("download_connect_failed") from None
        actual = ipaddress.ip_address(raw.getpeername()[0])
        if actual != address or str(actual) not in addresses:
            raise PinnedTransportError("download_peer_unverified")
        context = _ssl_context()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise PinnedTransportError("download_tls_unverified")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PinnedTransportError("download_deadline_exceeded")
        raw.settimeout(min(limits.connect_seconds, remaining))
        try:
            tls = context.wrap_socket(raw, server_hostname=hostname)
        except ssl.SSLCertVerificationError:
            raise PinnedTransportError("download_tls_unverified") from None
        except TimeoutError:
            raise PinnedTransportError(
                "download_deadline_exceeded"
                if remaining <= limits.connect_seconds
                else "download_tls_timeout"
            ) from None
        if ipaddress.ip_address(tls.getpeername()[0]) != address:
            raise PinnedTransportError("download_peer_unverified")
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PinnedTransportError("download_deadline_exceeded")
        tls.settimeout(min(limits.read_seconds, remaining))
        request = f"GET {path} HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\nAccept-Encoding: identity\r\n\r\n".encode(
            "ascii"
        )
        try:
            tls.sendall(request)
        except TimeoutError:
            raise PinnedTransportError("download_write_timeout") from None
        socket_view = _DeadlineSocketView(
            tls, deadline, limits.read_seconds, limits.max_header_bytes
        )
        response = http.client.HTTPResponse(
            cast(socket.socket, socket_view), method="GET"
        )
        try:
            response.begin()
        except http.client.HTTPException:
            raise PinnedTransportError("download_protocol_invalid") from None
        socket_view.reader.header_remaining = None
        if (
            response.version != 11
            or response.headers.defects
            or len(response.headers.get_all("Content-Length", [])) > 1
            or len(response.headers.get_all("Content-Type", [])) > 1
            or response.headers.get_all("Transfer-Encoding", [])
            or len(response.headers.get_all("Content-Encoding", [])) > 1
            or any(
                value.strip().lower() != "identity"
                for value in response.headers.get_all("Content-Encoding", [])
            )
        ):
            raise PinnedTransportError("download_headers_invalid")
        yield PinnedResponse(
            response, tls, str(actual), hostname, deadline, limits.read_seconds
        )
    finally:
        if response is not None:
            response.close()
        if tls is not None:
            tls.close()
        raw.close()
