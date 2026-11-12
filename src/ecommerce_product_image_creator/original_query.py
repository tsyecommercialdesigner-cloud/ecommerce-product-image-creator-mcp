"""One inert recovery observation, with independently verified identity and source."""

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from .execution_candidates import read_candidate
from .provider import ENDPOINT, parse_query
from .storage import Store


@dataclass(frozen=True)
class OriginalIdentity:
    root: str
    candidate_id: str
    task_id: str
    revision: int
    stage: str
    attempt_id: str
    digest: str
    provider: str
    model: str
    endpoint: str
    group: str
    upstream_id: str


@dataclass(frozen=True)
class QueryResult:
    state: str
    reason: str
    observation_id: int | None = None
    invocation_id: str | None = None
    adapter_invoked: bool = False


@dataclass(frozen=True)
class QueryObservation:
    observation_id: int
    state: str
    reason: str
    response_digest: str | None
    observed_at: float
    previous_id: int | None


def read_observations(
    store: Store, root: str, candidate_id: str, attempt_id: str
) -> tuple[QueryObservation, ...]:
    """Read append-only observation history by its stored ownership, without network."""
    with closing(store._connect()) as db:
        rows = db.execute(
            "SELECT observation_id,remote_state,reason,response_digest,observed_at,previous_id FROM recovery_observation WHERE root_operation_id=? AND candidate_id=? AND attempt_id=? ORDER BY observation_id",
            (root, candidate_id, attempt_id),
        ).fetchall()
    return tuple(QueryObservation(*row) for row in rows)


def _verify_original_binding(
    db: sqlite3.Connection, identity: OriginalIdentity
) -> bool:
    """No production provenance verifier has been established."""
    return False


def _query_adapter(identity: OriginalIdentity) -> Any:
    """No real query transport is configured."""
    raise RuntimeError("Query adapter unavailable")


def _supplier_cost_event(
    identity: OriginalIdentity, raw: bytes
) -> tuple[str, str] | None:
    """No production source has attested a stable cost event or account."""
    return None


def _query_account_scope(identity: OriginalIdentity) -> str | None:
    """No production account-scope attestation is available."""
    return None


def _after_query_received() -> None:
    """Isolated fault-injection seam before recording a query result."""


def _before_query_dispatch() -> None:
    """Isolated scheduling seam after the durable local boundary."""


def _after_query_final_check() -> None:
    """Isolated crash seam after the local final check."""


def _after_query_adapter() -> None:
    """Isolated crash seam after adapter return, before response marker."""


def _after_query_observation() -> None:
    """Isolated crash seam after observation transaction commit."""


def _record_query_stop(
    db: sqlite3.Connection,
    request_id: str,
    invocation_id: str,
    authorization_id: str,
    entered: bool,
    reason: str,
) -> None:
    """Write only from the controlled path before invoking the adapter."""
    expected = "entered" if entered else "intent"
    inserted = db.execute(
        "INSERT INTO query_dispatch_stop SELECT q.invocation_id,q.request_id,q.authorization_id,?,?, 'server_pre_dispatch',? FROM query_request q JOIN query_authorization_consumption c ON c.request_id=q.request_id AND c.authorization_id=q.authorization_id AND c.invocation_id=q.invocation_id JOIN query_invocation i ON i.invocation_id=q.invocation_id WHERE q.request_id=? AND q.invocation_id=? AND q.authorization_id=? AND i.status=?",
        (
            int(entered),
            reason,
            time.time(),
            request_id,
            invocation_id,
            authorization_id,
            expected,
        ),
    )
    if inserted.rowcount != 1:
        raise ValueError("query_stop_identity_conflict")


def _chain_is_valid(db: sqlite3.Connection, identity: OriginalIdentity) -> bool:
    """Inspect persisted ownership and all predecessor links without changing history."""
    rows = db.execute(
        "SELECT observation_id,root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,provider,model,endpoint,grouping,upstream_id,previous_id FROM recovery_observation WHERE attempt_id=? ORDER BY observation_id",
        (identity.attempt_id,),
    ).fetchall()
    expected = (
        identity.root,
        identity.candidate_id,
        identity.task_id,
        identity.revision,
        identity.stage,
        identity.attempt_id,
        identity.digest,
        identity.provider,
        identity.model,
        identity.endpoint,
        identity.group,
        identity.upstream_id,
    )
    previous = None
    for row in rows:
        if row[1:13] != expected or row[13] != previous:
            return False
        previous = row[0]
    crossing = db.execute(
        "SELECT 1 FROM recovery_observation parent JOIN recovery_observation child ON child.previous_id=parent.observation_id WHERE parent.attempt_id=? AND child.attempt_id<>parent.attempt_id LIMIT 1",
        (identity.attempt_id,),
    ).fetchone()
    return not crossing


def _identity(
    store: Store,
    root: str,
    candidate_id: str,
    task_id: str,
    revision: int,
    attempt_id: str,
    digest: str,
) -> OriginalIdentity | None:
    candidate = read_candidate(store, candidate_id)
    snap = candidate.snapshot
    if (
        candidate.root_operation_id != root
        or (snap.task_id, snap.revision, snap.execution_digest, snap.digest_version)
        != (task_id, revision, digest, 2)
        or snap.endpoint != ENDPOINT
    ):
        return None
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT a.upstream_id,a.upstream_status,c.sequence,c.model,c.endpoint FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id JOIN operation o ON o.attempt_id=a.attempt_id WHERE c.operation_id=? AND c.attempt_id=? AND c.execution_digest=? AND a.task_id=? AND a.revision=? AND o.digest=c.execution_digest AND o.task_id=a.task_id AND o.revision=a.revision",
            (root, attempt_id, digest, task_id, revision),
        ).fetchone()
        association = db.execute(
            "SELECT candidate_id,root_operation_id FROM candidate_attempt WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if (
            not row
            or row[3:5] != (snap.model, snap.endpoint)
            or not isinstance(row[0], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row[0])
            or (candidate.source_candidate_id is None and association is not None)
            or (
                candidate.source_candidate_id is not None
                and association != (candidate_id, root)
            )
        ):
            return None
        duplicate = db.execute(
            "SELECT 1 FROM attempt WHERE upstream_id=? AND attempt_id<>? LIMIT 1",
            (row[0], attempt_id),
        ).fetchone()
        if duplicate:
            return None
        identity = OriginalIdentity(
            root,
            candidate_id,
            task_id,
            revision,
            snap.stage,
            attempt_id,
            digest,
            "candidate_gateway",
            snap.model,
            snap.endpoint,
            snap.group,
            row[0],
        )
        return identity if _verify_original_binding(db, identity) else None


def query_once(
    store: Store,
    root: str,
    candidate_id: str,
    task_id: str,
    revision: int,
    attempt_id: str,
    digest: str,
    *,
    request_id: str | None = None,
) -> QueryResult:
    """One query at most; observations never change generation or delivery history."""
    from . import query_registration, recovery
    from .task_view import _SnapshotStore

    invocation_id: str | None = None
    entered = False
    adapter_invoked = False
    if not request_id:
        return QueryResult("unverified", "query_request_required")
    try:
        with closing(store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT q.scope_payload,q.authorization_id,q.invocation_id,a.evidence_payload,i.status,i.observation_id FROM query_request q JOIN query_authorization a ON a.authorization_id=q.authorization_id JOIN query_authorization_consumption c ON c.authorization_id=q.authorization_id AND c.request_id=q.request_id AND c.invocation_id=q.invocation_id JOIN query_invocation i ON i.invocation_id=q.invocation_id WHERE q.request_id=?",
                (request_id,),
            ).fetchone()
            if not row:
                return QueryResult("unverified", "query_registration_required")
            request = query_registration.QueryRequest(**json.loads(row[0]))
            auth = query_registration.QueryAuthorization(**json.loads(row[3]))
            invocation_id = row[2]
            if not isinstance(invocation_id, str) or not invocation_id:
                return QueryResult("unverified", "query_registration_incomplete")
            invocation_scope = db.execute(
                "SELECT root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,provider,model,endpoint,grouping,account_scope,upstream_id FROM query_invocation WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if invocation_scope != (
                request.root_operation_id,
                request.candidate_id,
                request.task_id,
                request.revision,
                request.stage,
                request.attempt_id,
                request.execution_digest_v2,
                request.provider,
                request.model,
                request.endpoint,
                request.group,
                request.account_scope,
                request.trusted_upstream_id,
            ):
                return QueryResult(
                    "unverified",
                    "invocation_scope_conflict",
                    invocation_id=invocation_id,
                )
            if (
                request.request_id,
                request.root_operation_id,
                request.candidate_id,
                request.task_id,
                request.revision,
                request.attempt_id,
                request.execution_digest_v2,
                request.query_authorization_reference,
            ) != (
                request_id,
                root,
                candidate_id,
                task_id,
                revision,
                attempt_id,
                digest,
                row[1],
            ):
                return QueryResult(
                    "unverified",
                    "query_request_scope_conflict",
                    invocation_id=invocation_id,
                )
            snapshot = _SnapshotStore(store.path, db)
            identity = _identity(
                snapshot, root, candidate_id, task_id, revision, attempt_id, digest
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
                    request.stage,
                    request.trusted_upstream_id,
                    request.provider,
                    request.model,
                    request.endpoint,
                    request.group,
                )
                or _query_account_scope(identity) != request.account_scope
            ):
                return QueryResult(
                    "unverified",
                    "identity_or_source_unverified",
                    invocation_id=invocation_id,
                )
            if not _chain_is_valid(db, identity):
                return QueryResult(
                    "conflict", "observation_chain_invalid", invocation_id=invocation_id
                )
            stop = db.execute(
                "SELECT entered,reason,recorded_at FROM query_dispatch_stop WHERE invocation_id=? AND request_id=? AND authorization_id=? AND source='server_pre_dispatch'",
                (invocation_id, request_id, auth.authorization_id),
            ).fetchone()
            if stop:
                revocation = db.execute(
                    "SELECT source_reference,recorded_at FROM query_authorization_revocation WHERE authorization_id=?",
                    (auth.authorization_id,),
                ).fetchone()
                if (
                    stop[0] not in (0, 1)
                    or row[4] != ("entered" if stop[0] else "intent")
                    or stop[1] not in ("revoked", "expired")
                    or not isinstance(stop[2], (int, float))
                    or not 0 < stop[2] < float("inf")
                    or not query_registration._verify_query_authorization(db, auth)
                    or (
                        auth.authorization_id,
                        auth.request_id,
                        auth.attempt_id,
                        auth.execution_digest_v2,
                        auth.provider,
                        auth.account_scope,
                        auth.query_action_type,
                        auth.max_invocations,
                    )
                    != (
                        request.query_authorization_reference,
                        request_id,
                        attempt_id,
                        digest,
                        request.provider,
                        request.account_scope,
                        request.query_action_type,
                        1,
                    )
                    or (stop[1] == "expired" and auth.expires_at > stop[2])
                    or (
                        stop[1] == "revoked"
                        and (
                            not revocation
                            or revocation[1] > stop[2]
                            or not query_registration._verify_query_revocation(
                                db, auth.authorization_id, revocation[0]
                            )
                        )
                    )
                ):
                    return QueryResult(
                        "unverified", "query_stop_conflict", invocation_id=invocation_id
                    )
                return QueryResult(
                    "stopped", "stopped_before_dispatch", invocation_id=invocation_id
                )
            if row[4] == "observed" and row[5] is not None:
                resolved = recovery.resolve_recorded(
                    snapshot,
                    root,
                    candidate_id,
                    task_id,
                    revision,
                    identity.stage,
                    attempt_id,
                    digest,
                )
                if resolved["state"] in ("conflict", "unverified"):
                    return QueryResult(
                        str(resolved["state"]),
                        str(resolved["reason"]),
                        row[5],
                        invocation_id,
                    )
                observed = db.execute(
                    "SELECT remote_state,reason FROM recovery_observation WHERE observation_id=? AND root_operation_id=? AND candidate_id=? AND task_id=? AND revision=? AND stage=? AND attempt_id=? AND execution_digest=?",
                    (
                        row[5],
                        root,
                        candidate_id,
                        task_id,
                        revision,
                        identity.stage,
                        attempt_id,
                        digest,
                    ),
                ).fetchone()
                if not observed:
                    return QueryResult(
                        "unverified",
                        "observation_identity_mismatch",
                        row[5],
                        invocation_id,
                    )
                return QueryResult(observed[0], observed[1], row[5], invocation_id)
            if row[4] != "intent":
                return QueryResult(
                    "unknown", "query_may_have_executed", invocation_id=invocation_id
                )
            baseline = db.execute(
                "SELECT observation_id FROM query_request_observation_baseline WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if baseline is None:
                return QueryResult(
                    "unverified", "query_baseline_missing", invocation_id=invocation_id
                )
            revoked = db.execute(
                "SELECT 1 FROM query_authorization_revocation WHERE authorization_id=?",
                (auth.authorization_id,),
            ).fetchone()
            expired = auth.expires_at <= time.time()
            auth_matches = (
                auth.authorization_id,
                auth.request_id,
                auth.attempt_id,
                auth.execution_digest_v2,
                auth.provider,
                auth.account_scope,
                auth.query_action_type,
                auth.max_invocations,
            ) == (
                request.query_authorization_reference,
                request_id,
                attempt_id,
                digest,
                request.provider,
                request.account_scope,
                request.query_action_type,
                1,
            )
            auth_verified = query_registration._verify_query_authorization(db, auth)
            if not auth_matches or expired or revoked or not auth_verified:
                if (revoked or expired) and auth_matches and auth_verified:
                    _record_query_stop(
                        db,
                        request_id,
                        invocation_id,
                        auth.authorization_id,
                        False,
                        "revoked" if revoked else "expired",
                    )
                    return QueryResult(
                        "stopped",
                        "stopped_before_dispatch",
                        invocation_id=invocation_id,
                    )
                return QueryResult(
                    "unverified",
                    "query_authorization_invalid",
                    invocation_id=invocation_id,
                )
            observations = db.execute(
                "SELECT 1 FROM recovery_observation WHERE attempt_id=? LIMIT 1",
                (attempt_id,),
            ).fetchone()
            if observations and recovery.resolve_recorded(
                snapshot,
                root,
                candidate_id,
                task_id,
                revision,
                identity.stage,
                attempt_id,
                digest,
            )["state"] in ("conflict", "unverified"):
                return QueryResult(
                    "conflict", "observation_unverified", invocation_id=invocation_id
                )
            latest = db.execute(
                "SELECT observation_id FROM recovery_observation WHERE attempt_id=? ORDER BY observation_id DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
            if (
                latest
                and latest[0] != baseline[0]
                and recovery.resolve_recorded(
                    snapshot,
                    root,
                    candidate_id,
                    task_id,
                    revision,
                    identity.stage,
                    attempt_id,
                    digest,
                )["state"]
                in ("succeeded", "failed")
            ):
                return QueryResult(
                    "conflict", "original_terminal_changed", invocation_id=invocation_id
                )
            updated = db.execute(
                "UPDATE query_invocation SET status='entered',updated_at=? WHERE invocation_id=? AND status='intent'",
                (time.time(), invocation_id),
            )
            if updated.rowcount != 1:
                return QueryResult(
                    "unverified",
                    "invocation_state_conflict",
                    invocation_id=invocation_id,
                )
        entered = True
        _before_query_dispatch()
        with closing(store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = _SnapshotStore(store.path, db)
            current = _identity(
                snapshot, root, candidate_id, task_id, revision, attempt_id, digest
            )
            revoked = db.execute(
                "SELECT 1 FROM query_authorization_revocation WHERE authorization_id=?",
                (auth.authorization_id,),
            ).fetchone()
            expired = auth.expires_at <= time.time()
            auth_verified = query_registration._verify_query_authorization(db, auth)
            status = db.execute(
                "SELECT status FROM query_invocation WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            binding_valid = (
                current == identity
                and _query_account_scope(identity) == request.account_scope
                and _chain_is_valid(db, identity)
                and auth_verified
                and status == ("entered",)
            )
            if not binding_valid or expired or revoked:
                if binding_valid and (revoked or expired):
                    _record_query_stop(
                        db,
                        request_id,
                        invocation_id,
                        auth.authorization_id,
                        True,
                        "revoked" if revoked else "expired",
                    )
                    return QueryResult(
                        "stopped",
                        "stopped_before_dispatch",
                        invocation_id=invocation_id,
                    )
                return QueryResult(
                    "unverified",
                    "query_pre_dispatch_check_failed",
                    invocation_id=invocation_id,
                )
            if db.execute(
                "SELECT 1 FROM recovery_observation WHERE attempt_id=? LIMIT 1",
                (attempt_id,),
            ).fetchone() and recovery.resolve_recorded(
                snapshot,
                root,
                candidate_id,
                task_id,
                revision,
                identity.stage,
                attempt_id,
                digest,
            )["state"] in ("conflict", "unverified"):
                return QueryResult(
                    "conflict", "observation_unverified", invocation_id=invocation_id
                )
            latest = db.execute(
                "SELECT observation_id FROM recovery_observation WHERE attempt_id=? ORDER BY observation_id DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
            if (
                latest
                and latest[0] != baseline[0]
                and recovery.resolve_recorded(
                    snapshot,
                    root,
                    candidate_id,
                    task_id,
                    revision,
                    identity.stage,
                    attempt_id,
                    digest,
                )["state"]
                in ("succeeded", "failed")
            ):
                return QueryResult(
                    "conflict", "original_terminal_changed", invocation_id=invocation_id
                )
        _after_query_final_check()
        payload = None
        raw_response = None
        try:
            adapter_invoked = True
            payload = _query_adapter(identity)
            if isinstance(payload, bytes):
                raw_response = payload
                payload = json.loads(payload)
            _after_query_adapter()
            with closing(store._connect()) as db, db:
                db.execute(
                    "UPDATE query_invocation SET status='response_received',updated_at=? WHERE invocation_id=? AND status='entered'",
                    (time.time(), invocation_id),
                )
            _after_query_received()
            normalized = parse_query(200, payload, identity.upstream_id)
            state = (
                normalized.state
                if normalized.state in {"pending", "running", "failed", "succeeded"}
                else "unknown"
            )
            raw_digest = hashlib.sha256(
                raw_response
                if raw_response is not None
                else json.dumps(
                    payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
            reason = "observed" if state != "unknown" else "response_uninterpretable"
        except Exception:  # noqa: BLE001 - sanitized observation; no generation retry
            state, reason, raw_digest = "query_error", "query_failed", None
        with closing(store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if _identity(
                store, root, candidate_id, task_id, revision, attempt_id, digest
            ) != identity or not _verify_original_binding(db, identity):
                return QueryResult(
                    "unknown",
                    "identity_changed_after_query",
                    invocation_id=invocation_id,
                    adapter_invoked=adapter_invoked,
                )
            if not _chain_is_valid(db, identity):
                return QueryResult(
                    "conflict",
                    "observation_chain_invalid",
                    invocation_id=invocation_id,
                    adapter_invoked=adapter_invoked,
                )
            original = db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            previous = db.execute(
                "SELECT observation_id,remote_state FROM recovery_observation WHERE attempt_id=? ORDER BY observation_id DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
            terminal = db.execute(
                "SELECT remote_state FROM recovery_observation WHERE attempt_id=? AND remote_state IN ('succeeded','failed') ORDER BY observation_id LIMIT 1",
                (attempt_id,),
            ).fetchone()
            historic = (
                terminal[0]
                if terminal
                else (
                    original[0]
                    if original and original[0] in {"succeeded", "failed"}
                    else None
                )
            )
            if (
                historic
                and state in {"pending", "running", "succeeded", "failed"}
                and state != historic
            ):
                state, reason = "conflict", "terminal_state_conflict"
            if (
                isinstance(payload, dict)
                and isinstance(payload.get("data"), dict)
                and payload["data"].get("task_id") not in (None, identity.upstream_id)
            ):
                state, reason = "conflict", "upstream_identity_conflict"
            if previous and previous[1] == "conflict":
                state, reason = "conflict", "unresolved_prior_conflict"
            row = db.execute(
                "INSERT INTO recovery_observation(root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,provider,model,endpoint,grouping,upstream_id,source,response_digest,remote_state,reason,observed_at,previous_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    root,
                    candidate_id,
                    task_id,
                    revision,
                    identity.stage,
                    attempt_id,
                    digest,
                    identity.provider,
                    identity.model,
                    identity.endpoint,
                    identity.group,
                    identity.upstream_id,
                    "configured_query_adapter",
                    raw_digest,
                    state,
                    reason,
                    time.time(),
                    previous[0] if previous else None,
                ),
            )
            db.execute(
                "UPDATE query_invocation SET status='observed',observation_id=?,updated_at=? WHERE invocation_id=? AND status IN ('entered','response_received')",
                (row.lastrowid, time.time(), invocation_id),
            )
            result = QueryResult(
                state, reason, row.lastrowid, invocation_id, adapter_invoked
            )
        _after_query_observation()
        if raw_response is not None and result.state in {"succeeded", "failed"}:
            try:
                from .generation_cost import record_supplier_response_cost

                event = _supplier_cost_event(identity, raw_response)
                if event is not None:
                    record_supplier_response_cost(
                        store,
                        root,
                        candidate_id,
                        task_id,
                        revision,
                        attempt_id,
                        digest,
                        raw_response,
                        *event,
                    )
            except (ValueError, sqlite3.Error, TypeError):
                pass  # The observation is retained; unavailable cost remains unknown.
        return result
    except (ValueError, sqlite3.Error, OSError, TypeError):
        if entered:
            return QueryResult(
                "unknown",
                "query_result_unrecorded",
                invocation_id=invocation_id,
                adapter_invoked=adapter_invoked,
            )
        return QueryResult(
            "unverified", "identity_or_storage_unverified", invocation_id=invocation_id
        )
