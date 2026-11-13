"""Disabled-by-default single-request transport for entered Team attempts."""

from __future__ import annotations

import http.client
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import config, provider


@dataclass(frozen=True)
class TeamSettings:
    enabled: bool = True
    generation_enabled: bool = False
    timeout_seconds: float = 30.0
    response_limit: int = 65536

    @classmethod
    def from_environment(cls) -> TeamSettings:
        # Mode defaults to Team; the paid generation switch stays independently off.
        return cls(
            enabled=config.team_mode_enabled(),
            generation_enabled=os.environ.get("ENABLE_REAL_GENERATION", "").lower()
            == "true",
        )


@dataclass(frozen=True)
class TeamRequest:
    operation_id: str
    attempt_id: str
    execution_digest: str
    model: str
    request: provider.SerializedRequest = field(repr=False)
    db: sqlite3.Connection | None = field(default=None, repr=False, compare=False)


def _verify_team_authorization(request: TeamRequest) -> bool:
    """Check the already-entered intent inside the existing write transaction."""
    db = request.db
    if db is None or not db.in_transaction:
        return False
    row = db.execute(
        "SELECT o.digest,a.upstream_status,a.upstream_id,t.phase,oa.model,oa.endpoint "
        "FROM operation o JOIN attempt a ON a.attempt_id=o.attempt_id "
        "JOIN operation_attempt oa ON oa.attempt_id=a.attempt_id "
        "JOIN attempt_transport_evidence t ON t.attempt_id=a.attempt_id "
        "WHERE o.operation_id=? AND a.attempt_id=? AND oa.sequence=1",
        (request.operation_id, request.attempt_id),
    ).fetchone()
    return row == (
        request.execution_digest,
        "submission_uncertain",
        None,
        "entered",
        request.model,
        provider.ENDPOINT,
    )


def _open_connection(host: str, timeout: float) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(host, timeout=timeout)


def query_once(upstream_id: str, endpoint: str, settings: TeamSettings) -> bytes:
    """Query the original gateway task once; URL and task ID cannot be caller URLs."""
    if (
        not settings.enabled
        or not settings.generation_enabled
        or os.environ.get("ENABLE_TEAM_QUERY", "").lower() != "true"
    ):
        raise ValueError("team_query_disabled")
    if (
        endpoint != provider.ENDPOINT
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", upstream_id)
        or settings.timeout_seconds <= 0
        or settings.response_limit <= 0
    ):
        raise ValueError("team_query_invalid")
    credential = config.load_config().api_key
    host = urlsplit(provider.ENDPOINT).hostname
    if host is None:
        raise ValueError("team_query_invalid")
    connection = None
    try:
        connection = _open_connection(host, settings.timeout_seconds)
        connection.request(
            "GET",
            f"/v1/images/tasks/{upstream_id}",
            headers={"Authorization": "Bearer " + credential},
        )
        response = connection.getresponse()
        body = response.read(settings.response_limit + 1)
        if response.status != 200 or len(body) > settings.response_limit:
            raise ValueError("team_query_response_invalid")
        return body
    except (OSError, http.client.HTTPException):
        raise ValueError("team_query_transport_failed") from None
    finally:
        if connection is not None:
            connection.close()


def send_once(request: TeamRequest, settings: TeamSettings) -> provider.ProviderResult:
    """Send at most one POST when all gates pass; never retry or follow redirects.

    The existing submission transaction supplies the entered attempt identity.
    """
    if not settings.enabled or not settings.generation_enabled:
        raise ValueError("team_generation_disabled")
    if not _verify_team_authorization(request):
        raise ValueError("team_authorization_unverified")
    wire = request.request
    if (
        wire.method != "POST"
        or wire.url != provider.ENDPOINT
        or request.model not in provider.MODELS
        or not request.operation_id
        or not request.attempt_id
        or len(request.execution_digest) != 64
        or settings.timeout_seconds <= 0
        or settings.response_limit <= 0
        or set(wire.headers) != {"Content-Type"}
        or not wire.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    ):
        raise ValueError("team_request_invalid")
    credential = config.load_config().api_key
    target = urlsplit(wire.url)
    if target.hostname is None:
        raise ValueError("team_request_invalid")
    connection = None
    try:
        connection = _open_connection(target.hostname, settings.timeout_seconds)
        path = target.path + ("?" + target.query if target.query else "")
        connection.request(
            "POST",
            path,
            body=wire.body,
            headers={**wire.headers, "Authorization": "Bearer " + credential},
        )
        response = connection.getresponse()
        body = response.read(settings.response_limit + 1)
        if len(body) > settings.response_limit:
            return provider.transport_unknown()
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return provider.transport_unknown()
        return provider.parse_submission(response.status, payload)
    except (OSError, http.client.HTTPException):
        return provider.transport_unknown()
    finally:
        if connection is not None:
            connection.close()
