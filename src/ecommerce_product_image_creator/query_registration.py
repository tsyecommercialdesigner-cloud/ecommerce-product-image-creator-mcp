"""Non-executable query intent registration with independently verified authorization."""

import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass

from . import original_query, recovery
from .storage import Store
from .task_view import _SnapshotStore


@dataclass(frozen=True)
class QueryRequest:
    request_id: str
    task_id: str
    revision: int
    stage: str
    root_operation_id: str
    candidate_id: str
    attempt_id: str
    execution_digest_v2: str
    trusted_upstream_id: str
    provider: str
    model: str
    endpoint: str
    group: str
    account_scope: str
    query_action_type: str
    query_authorization_reference: str


@dataclass(frozen=True)
class QueryAuthorization:
    authorization_id: str
    request_id: str
    attempt_id: str
    execution_digest_v2: str
    provider: str
    account_scope: str
    query_action_type: str
    max_invocations: int
    source_reference: str
    expires_at: float
    cost_risk: str
    amount_limit: str | None = None
    currency: str | None = None


@dataclass(frozen=True)
class Registration:
    request_id: str
    invocation_id: str
    status: str
    created: bool


def _verify_query_authorization(
    db: sqlite3.Connection, evidence: QueryAuthorization
) -> bool:
    """Production has no authenticated query fee authorization channel."""
    return False


def _verify_query_revocation(
    db: sqlite3.Connection, authorization_id: str, reference: str
) -> bool:
    return False


def _before_commit() -> None:
    """Fault injection seam; never an execution hook."""


def _payload(value: QueryRequest | QueryAuthorization) -> str:
    return json.dumps(
        asdict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _label(value: str) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value)
    )


def record_authorization(store: Store, evidence: QueryAuthorization) -> str:
    """Persist independently verified authorization; never creates a query intent."""
    e = evidence
    if (
        not all(
            _label(x)
            for x in (
                e.authorization_id,
                e.request_id,
                e.attempt_id,
                e.provider,
                e.account_scope,
                e.source_reference,
            )
        )
        or e.query_action_type != "query_original"
        or not re.fullmatch(r"[0-9a-f]{64}", e.execution_digest_v2)
        or type(e.max_invocations) is not int
        or e.max_invocations != 1
        or not isinstance(e.expires_at, (int, float))
        or not math.isfinite(e.expires_at)
        or e.expires_at <= time.time()
        or not isinstance(e.cost_risk, str)
        or not e.cost_risk.strip()
        or len(e.cost_risk) > 512
        or e.amount_limit is not None
        or e.currency is not None
    ):
        raise ValueError("invalid_query_authorization")
    payload = _payload(e)
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not _verify_query_authorization(db, e):
                raise ValueError("query_authorization_unverified")
            prior = db.execute(
                "SELECT evidence_payload FROM query_authorization WHERE authorization_id=?",
                (e.authorization_id,),
            ).fetchone()
            if prior:
                if prior[0] != payload:
                    raise ValueError("query_authorization_conflict")
                db.rollback()
                return e.authorization_id
            db.execute(
                "INSERT INTO query_authorization VALUES (?,?,?,?)",
                (e.authorization_id, e.request_id, payload, time.time()),
            )
            db.commit()
            return e.authorization_id
        except BaseException:
            db.rollback()
            raise


def revoke_authorization(
    store: Store, authorization_id: str, source_reference: str
) -> None:
    if not _label(authorization_id) or not _label(source_reference):
        raise ValueError("invalid_query_revocation")
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not _verify_query_revocation(db, authorization_id, source_reference):
                raise ValueError("query_revocation_unverified")
            prior = db.execute(
                "SELECT source_reference FROM query_authorization_revocation WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
            if prior:
                if prior[0] != source_reference:
                    raise ValueError("query_revocation_conflict")
            else:
                if not db.execute(
                    "SELECT 1 FROM query_authorization WHERE authorization_id=?",
                    (authorization_id,),
                ).fetchone():
                    raise ValueError("query_authorization_missing")
                db.execute(
                    "INSERT INTO query_authorization_revocation VALUES (?,?,?)",
                    (authorization_id, source_reference, time.time()),
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise


def register_request(store: Store, request: QueryRequest) -> Registration:
    """Atomically consume one query authorization and register a non-executable intent."""
    r = request
    if (
        not all(
            _label(x)
            for x in (
                r.request_id,
                r.task_id,
                r.root_operation_id,
                r.candidate_id,
                r.attempt_id,
                r.trusted_upstream_id,
                r.provider,
                r.account_scope,
                r.query_authorization_reference,
            )
        )
        or type(r.revision) is not int
        or r.revision < 1
        or r.stage not in ("form", "mock")
        or r.query_action_type != "query_original"
        or not re.fullmatch(r"[0-9a-f]{64}", r.execution_digest_v2)
    ):
        raise ValueError("invalid_query_request")
    scope = _payload(r)
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            prior = db.execute(
                "SELECT scope_payload,invocation_id FROM query_request WHERE request_id=?",
                (r.request_id,),
            ).fetchone()
            if prior:
                if prior[0] != scope:
                    raise ValueError("query_request_conflict")
                row = db.execute(
                    "SELECT i.status FROM query_invocation i JOIN query_authorization_consumption c ON c.invocation_id=i.invocation_id JOIN query_authorization a ON a.authorization_id=c.authorization_id WHERE i.invocation_id=? AND c.request_id=? AND c.authorization_id=? AND a.request_id=c.request_id",
                    (prior[1], r.request_id, r.query_authorization_reference),
                ).fetchone()
                if not row:
                    raise ValueError("query_registration_incomplete")
                db.rollback()
                return Registration(r.request_id, prior[1], row[0], False)
            authrow = db.execute(
                "SELECT evidence_payload FROM query_authorization WHERE authorization_id=? AND request_id=?",
                (r.query_authorization_reference, r.request_id),
            ).fetchone()
            if not authrow:
                raise ValueError("query_authorization_missing")
            e = QueryAuthorization(**json.loads(authrow[0]))
            if (
                e.attempt_id,
                e.execution_digest_v2,
                e.provider,
                e.account_scope,
                e.query_action_type,
            ) != (
                r.attempt_id,
                r.execution_digest_v2,
                r.provider,
                r.account_scope,
                r.query_action_type,
            ) or e.max_invocations != 1:
                raise ValueError("query_authorization_scope_mismatch")
            if (
                e.expires_at <= time.time()
                or db.execute(
                    "SELECT 1 FROM query_authorization_revocation WHERE authorization_id=?",
                    (e.authorization_id,),
                ).fetchone()
                or not _verify_query_authorization(db, e)
            ):
                raise ValueError("query_authorization_invalid")
            if db.execute(
                "SELECT 1 FROM query_authorization_consumption WHERE authorization_id=?",
                (e.authorization_id,),
            ).fetchone():
                raise ValueError("query_authorization_consumed")
            snapshot = _SnapshotStore(store.path, db)
            identity = original_query._identity(
                snapshot,
                r.root_operation_id,
                r.candidate_id,
                r.task_id,
                r.revision,
                r.attempt_id,
                r.execution_digest_v2,
            )
            if (
                identity is None
                or (
                    identity.stage,
                    identity.upstream_id,
                    identity.provider,
                    identity.model,
                    identity.endpoint,
                    identity.group,
                )
                != (
                    r.stage,
                    r.trusted_upstream_id,
                    r.provider,
                    r.model,
                    r.endpoint,
                    r.group,
                )
                or original_query._query_account_scope(identity) != r.account_scope
            ):
                raise ValueError("query_identity_unverified")
            if not original_query._chain_is_valid(db, identity):
                raise ValueError("query_observation_conflict")
            observations = db.execute(
                "SELECT 1 FROM recovery_observation WHERE attempt_id=? LIMIT 1",
                (r.attempt_id,),
            ).fetchone()
            if observations:
                resolved = recovery.resolve_recorded(
                    snapshot,
                    r.root_operation_id,
                    r.candidate_id,
                    r.task_id,
                    r.revision,
                    r.stage,
                    r.attempt_id,
                    r.execution_digest_v2,
                )
                if resolved["state"] in ("conflict", "unverified"):
                    raise ValueError("query_observation_unverified")
            invocation_id = uuid.uuid4().hex
            now = time.time()
            db.execute(
                "INSERT INTO query_invocation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    invocation_id,
                    r.root_operation_id,
                    r.candidate_id,
                    r.task_id,
                    r.revision,
                    r.stage,
                    r.attempt_id,
                    r.execution_digest_v2,
                    r.provider,
                    r.model,
                    r.endpoint,
                    r.group,
                    r.account_scope,
                    r.trusted_upstream_id,
                    "configured_query_adapter",
                    "verified_for_intent",
                    "intent",
                    now,
                    now,
                    None,
                ),
            )
            db.execute(
                "INSERT INTO query_request VALUES (?,?,?,?,?)",
                (r.request_id, scope, e.authorization_id, invocation_id, now),
            )
            baseline = db.execute(
                "SELECT observation_id FROM recovery_observation WHERE attempt_id=? ORDER BY observation_id DESC LIMIT 1",
                (r.attempt_id,),
            ).fetchone()
            db.execute(
                "INSERT INTO query_request_observation_baseline VALUES (?,?)",
                (r.request_id, baseline[0] if baseline else None),
            )
            db.execute(
                "INSERT INTO query_authorization_consumption VALUES (?,?,?,?)",
                (e.authorization_id, r.request_id, invocation_id, now),
            )
            _before_commit()
            db.commit()
            return Registration(r.request_id, invocation_id, "intent", True)
        except BaseException:
            db.rollback()
            raise
