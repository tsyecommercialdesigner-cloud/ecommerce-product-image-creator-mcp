"""Fail-closed artifact download primitives; production sources remain unverified."""

import hashlib
import ipaddress
import math
import re
import time
from contextlib import closing
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from . import artifact_locator, preparation
from .storage import ResourceBudget, Store, validate_image_bytes


class DownloadError(ValueError):
    """Sanitized failure while inspecting a controlled response."""


@dataclass(frozen=True)
class DownloadLimits:
    max_bytes: int
    chunk_bytes: int
    connect_seconds: float
    read_seconds: float
    total_seconds: float
    max_header_bytes: int | None = None
    dns_seconds: float | None = None
    dns_max_answers: int | None = None

    def __post_init__(self) -> None:
        if (
            self.max_bytes <= 0
            or self.chunk_bytes <= 0
            or self.chunk_bytes > self.max_bytes
            or (self.max_header_bytes is not None and self.max_header_bytes <= 0)
            or ((self.dns_seconds is None) != (self.dns_max_answers is None))
            or (
                self.dns_seconds is not None
                and (not math.isfinite(self.dns_seconds) or self.dns_seconds <= 0)
            )
            or (self.dns_max_answers is not None and self.dns_max_answers <= 0)
            or any(
                not math.isfinite(x) or x <= 0
                for x in (self.connect_seconds, self.read_seconds, self.total_seconds)
            )
        ):
            raise ValueError("download_limits_invalid")


@dataclass(frozen=True)
class DownloadCandidate:
    locator_id: str
    sha256: str
    mime: str
    data: bytes = field(repr=False)


def _verify_download_scope(
    locator: artifact_locator.LocatorEvidence, url: str, digest: str, mime: str
) -> bool:
    """Production has no trusted URL, digest, access or charge authorization."""
    return False


def _resolve(host: str, port: int) -> tuple[str, ...]:
    """No production DNS resolver is enabled."""
    raise ValueError("download_resolver_unavailable")


def _verify_dns_policy(host: str, port: int) -> bool:
    """No production DNS/network policy has been approved."""
    return False


def _open_pinned(
    url: str, hostname: str, addresses: tuple[str, ...], limits: DownloadLimits
) -> Any:
    """Open a fresh verified connection; the default source and resolver reject."""
    from .pinned_https import open_pinned

    return open_pinned(url, hostname, addresses, limits)


def _verify_peer(response: Any, hostname: str, addresses: tuple[str, ...]) -> bool:
    """Accept only the dedicated verified one-shot transport response."""
    from .pinned_https import PinnedResponse

    return (
        isinstance(response, PinnedResponse)
        and response.hostname == hostname
        and response.peer in addresses
    )


def _monotonic() -> float:
    return time.monotonic()


def _target(url: str) -> tuple[str, int]:
    if (
        not isinstance(url, str)
        or not url.isascii()
        or any(ord(c) <= 32 or ord(c) == 127 for c in url)
        or "\\" in url
    ):
        raise ValueError("download_url_invalid")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise ValueError("download_url_invalid")
        host = parsed.hostname
        if (
            not re.fullmatch(r"[a-z0-9.-]+", host)
            or host.endswith(".")
            or ".." in host
            or any(
                not part or part.startswith("-") or part.endswith("-")
                for part in host.split(".")
            )
        ):
            raise ValueError("download_url_invalid")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("download_ip_literal_rejected")
        return host, 443
    except (ValueError, UnicodeError):
        raise ValueError("download_url_invalid") from None


def _addresses(
    host: str, port: int, limits: DownloadLimits | None = None
) -> tuple[str, ...]:
    from .dns_isolation import DNSResolutionError, resolve_in_process

    try:
        if (
            limits is not None
            and limits.dns_seconds is not None
            and limits.dns_max_answers is not None
        ):
            if not _verify_dns_policy(host, port):
                raise DNSResolutionError("download_dns_unverified")
            raw = resolve_in_process(
                host,
                port,
                min(limits.dns_seconds, limits.total_seconds),
                limits.dns_max_answers,
            )
        else:
            raw = _resolve(host, port)
        if not raw:
            raise ValueError("dns_unavailable")
        result = []
        for item in raw:
            if not isinstance(item, str) or "%" in item:
                raise ValueError("dns_forbidden")
            ip = ipaddress.ip_address(item)
            if (
                not ip.is_global
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
                or ip.is_loopback
                or ip.is_link_local
                or getattr(ip, "ipv4_mapped", None) is not None
            ):
                raise ValueError("dns_forbidden")
            result.append(str(ip))
        return tuple(dict.fromkeys(result))
    except DNSResolutionError:
        raise
    except (ValueError, OSError):
        raise ValueError("download_dns_unverified") from None


def fetch_candidate(
    store: Store,
    locator_id: str,
    url: str,
    digest: str,
    mime: str,
    limits: DownloadLimits,
    image_budget: ResourceBudget,
) -> DownloadCandidate:
    """Acquire an offline candidate only; this does not publish or update SQLite."""
    if (
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        or mime not in ("image/png", "image/jpeg")
        or limits.max_bytes > image_budget.max_file_bytes
    ):
        raise ValueError("download_scope_invalid")
    evidence = artifact_locator.read_locator(store, locator_id)
    if (
        evidence is None
        or evidence.expires <= time.time()
        or not _verify_download_scope(evidence, url, digest, mime)
    ):
        raise ValueError("download_source_unverified")
    with closing(store._connect()) as db:
        if not artifact_locator._checked(db, store, evidence):
            raise ValueError("download_source_unverified")
        if evidence.stage == "mock":
            preparation.check_mock_review_current(
                db, evidence.task_id, evidence.revision
            )
    host, port = _target(url)
    start = _monotonic()
    addresses = (
        _addresses(host, port, limits)
        if limits.dns_seconds is not None
        else _addresses(host, port)
    )
    remaining = limits.total_seconds - (_monotonic() - start)
    if remaining <= 0:
        raise DownloadError("download_deadline_exceeded")
    transport_limits = replace(limits, total_seconds=remaining)
    try:
        with _open_pinned(url, host, addresses, transport_limits) as response:
            if not _verify_peer(response, host, addresses):
                raise DownloadError("download_peer_unverified")
            if response.status_code != 200:
                raise DownloadError("download_status_rejected")
            if (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != mime
            ):
                raise DownloadError("download_mime_mismatch")
            length = response.headers.get("content-length")
            if length is not None and (
                not length.isascii()
                or not length.isdigit()
                or int(length) > limits.max_bytes
            ):
                raise DownloadError("download_size_invalid")
            data = bytearray()
            for chunk in response.iter_raw(chunk_size=limits.chunk_bytes):
                if _monotonic() - start > limits.total_seconds:
                    raise DownloadError("download_deadline_exceeded")
                if len(data) + len(chunk) > limits.max_bytes:
                    raise DownloadError("download_size_invalid")
                data.extend(chunk)
            if _monotonic() - start > limits.total_seconds or (
                length is not None and len(data) != int(length)
            ):
                raise DownloadError("download_incomplete")
    except DownloadError:
        raise
    except Exception as exc:  # noqa: BLE001 - sanitize errors from untrusted transport
        from .pinned_https import PinnedTransportError

        if isinstance(exc, PinnedTransportError):
            raise DownloadError(str(exc)) from None
        raise ValueError("download_failed") from None
    result = bytes(data)
    if hashlib.sha256(result).hexdigest() != digest:
        raise ValueError("download_digest_mismatch")
    validate_image_bytes(result, mime, image_budget)
    with closing(store._connect()) as db:
        if (
            artifact_locator.read_locator(store, locator_id) != evidence
            or evidence.expires <= time.time()
            or not _verify_download_scope(evidence, url, digest, mime)
            or not artifact_locator._checked(db, store, evidence)
        ):
            raise ValueError("download_source_changed")
        if evidence.stage == "mock":
            preparation.check_mock_review_current(
                db, evidence.task_id, evidence.revision
            )
    return DownloadCandidate(locator_id, digest, mime, result)
