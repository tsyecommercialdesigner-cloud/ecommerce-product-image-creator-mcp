"""Request-local Team policy for explicit original-task refreshes."""

import contextvars
import hashlib
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import team_mode_enabled
from .provider import ENDPOINT

_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "team_query", default=False
)
_store: contextvars.ContextVar[object] = contextvars.ContextVar(
    "team_store", default=None
)
_response: contextvars.ContextVar[object] = contextvars.ContextVar(
    "team_response", default=None
)


@contextmanager
def explicit_refresh(store: object) -> Iterator[None]:
    token = _active.set(True)
    store_token = _store.set(store)
    response_token = _response.set(None)
    try:
        yield
    finally:
        _active.reset(token)
        _store.reset(store_token)
        _response.reset(response_token)


def capture(identity: object, observation: int, payload: object) -> None:
    if enabled():
        _response.set((identity, observation, payload))


def response() -> object:
    return _response.get() if enabled() else None


def store() -> object:
    return _store.get() if enabled() else None


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def enabled() -> bool:
    return (
        _active.get()
        and team_mode_enabled()
        and os.environ.get("TEAM_GATEWAY_URL") == ENDPOINT
        and all(
            os.environ.get(name, "").lower() == "true"
            for name in ("ENABLE_REAL_GENERATION", "ENABLE_TEAM_QUERY")
        )
    )


def original_binding(db: sqlite3.Connection, identity: object) -> bool:
    from .original_query import OriginalIdentity

    if (
        not enabled()
        or not isinstance(identity, OriginalIdentity)
        or identity.endpoint != ENDPOINT
    ):
        return False
    row = db.execute(
        "SELECT 1 FROM operation o JOIN attempt a ON a.attempt_id=o.attempt_id "
        "JOIN operation_attempt oa ON oa.attempt_id=a.attempt_id AND oa.sequence=1 "
        "JOIN local_eligibility e ON e.operation_id=o.operation_id "
        "JOIN attempt_transport_evidence t ON t.attempt_id=a.attempt_id "
        "WHERE o.operation_id=? AND o.task_id=? AND o.revision=? AND o.digest=? "
        "AND a.attempt_id=? AND a.upstream_id=? AND e.confirmation_id=? "
        "AND t.phase='entered' AND t.operation_id=o.operation_id AND oa.model=? AND oa.endpoint=?",
        (
            identity.root,
            identity.task_id,
            identity.revision,
            identity.digest,
            identity.attempt_id,
            identity.upstream_id,
            "team-" + identity.root,
            identity.model,
            identity.endpoint,
        ),
    ).fetchone()
    return row is not None


def authorization(db: sqlite3.Connection, evidence: object) -> bool:
    from .query_registration import QueryAuthorization

    if not enabled() or not isinstance(evidence, QueryAuthorization):
        return False
    if (
        evidence.source_reference,
        evidence.account_scope,
        evidence.provider,
        evidence.query_action_type,
        evidence.max_invocations,
    ) != (
        "team-explicit-query",
        "team-gateway",
        "candidate_gateway",
        "query_original",
        1,
    ):
        return False
    return (
        db.execute(
            "SELECT 1 FROM operation o JOIN local_eligibility e ON e.operation_id=o.operation_id "
            "JOIN attempt_transport_evidence t ON t.attempt_id=o.attempt_id "
            "WHERE o.attempt_id=? AND o.digest=? AND e.confirmation_id='team-' || o.operation_id "
            "AND t.phase='entered'",
            (evidence.attempt_id, evidence.execution_digest_v2),
        ).fetchone()
        is not None
    )
