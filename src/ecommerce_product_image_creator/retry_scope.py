"""Persisted retry scopes; production trust adapters deliberately reject."""

import json
import math
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from typing import Any

from .domain import (
    Confirmation,
    ExecutionSnapshot,
    PromptRevision,
    Reference,
    Revision,
    Task,
)
from .execution_candidates import read_candidate
from .storage import Store
from .tasks import LocalEligibility, OperationScope


@dataclass(frozen=True)
class Grant:
    candidate_id: str
    digest: str
    model: str
    endpoint: str
    group: str
    max_attempts: int


@dataclass(frozen=True)
class HostScope:
    scope_id: str
    root: str
    task_id: str
    revision: int
    stage: str
    input_digest: str
    grants: tuple[Grant, ...]
    max_total: int
    expires_at: float
    source: str
    reference: str


@dataclass(frozen=True)
class CostScope(HostScope):
    currency: str
    max_minor: int
    per_attempt_minor: int
    price_reference: str


def _verify_host_source(db: sqlite3.Connection, value: HostScope) -> bool:
    return False


def _verify_cost_source(db: sqlite3.Connection, value: CostScope) -> bool:
    return False


def _verify_price_source(db: sqlite3.Connection, value: CostScope) -> bool:
    return False


def _verify_candidate_capability(
    db: sqlite3.Connection, candidate_id: str, snapshot: ExecutionSnapshot
) -> bool:
    """No real provider combination is verified by this module."""
    return False


def _verify_revocation_source(
    db: sqlite3.Connection, scope_id: str, reference: str
) -> bool:
    return False


def _schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS retry_scope_evidence (scope_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('host','cost')), payload TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS retry_scope_revocation (scope_id TEXT PRIMARY KEY, reason TEXT NOT NULL, FOREIGN KEY(scope_id) REFERENCES retry_scope_evidence(scope_id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS retry_scope_reservation (operation_id TEXT PRIMARY KEY, root TEXT NOT NULL, position INTEGER NOT NULL, candidate_id TEXT NOT NULL, host_id TEXT NOT NULL, cost_id TEXT NOT NULL, currency TEXT NOT NULL, reserved_minor INTEGER NOT NULL, attempt_id TEXT, UNIQUE(root,position), FOREIGN KEY(operation_id) REFERENCES local_eligibility(operation_id), FOREIGN KEY(host_id) REFERENCES retry_scope_evidence(scope_id), FOREIGN KEY(cost_id) REFERENCES retry_scope_evidence(scope_id))"
    )
    for table in ("retry_scope_evidence", "retry_scope_revocation"):
        for action in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable retry scope'); END"
            )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS retry_reservation_no_delete BEFORE DELETE ON retry_scope_reservation BEGIN SELECT RAISE(ABORT,'immutable retry reservation'); END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS retry_reservation_no_overwrite BEFORE UPDATE ON retry_scope_reservation WHEN OLD.attempt_id IS NOT NULL OR NEW.attempt_id IS NULL OR OLD.operation_id IS NOT NEW.operation_id OR OLD.root IS NOT NEW.root OR OLD.position IS NOT NEW.position OR OLD.candidate_id IS NOT NEW.candidate_id OR OLD.host_id IS NOT NEW.host_id OR OLD.cost_id IS NOT NEW.cost_id OR OLD.currency IS NOT NEW.currency OR OLD.reserved_minor IS NOT NEW.reserved_minor BEGIN SELECT RAISE(ABORT,'immutable retry reservation'); END"
    )


def revoke_scope(store: Store, scope_id: str, reference: str) -> None:
    if not scope_id or not reference:
        raise ValueError("invalid_revocation")
    with closing(store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if not _verify_revocation_source(db, scope_id, reference):
            raise ValueError("revocation_source_unverified")
        _schema(db)
        if not db.execute(
            "SELECT 1 FROM retry_scope_evidence WHERE scope_id=?", (scope_id,)
        ).fetchone():
            raise ValueError("retry_scope_unavailable")
        old = db.execute(
            "SELECT reason FROM retry_scope_revocation WHERE scope_id=?", (scope_id,)
        ).fetchone()
        if old and old[0] != reference:
            raise ValueError("revocation_conflict")
        if not old:
            db.execute(
                "INSERT INTO retry_scope_revocation VALUES(?,?)", (scope_id, reference)
            )


def _valid(store: Store, value: HostScope) -> None:
    if (
        not all(
            (
                value.scope_id,
                value.root,
                value.task_id,
                value.input_digest,
                value.source,
                value.reference,
            )
        )
        or value.stage not in ("form", "mock")
        or not math.isfinite(value.expires_at)
        or value.expires_at <= time.time()
        or not 1 <= value.max_total <= 6
        or not value.grants
        or len({g.candidate_id for g in value.grants}) != len(value.grants)
    ):
        raise ValueError("invalid_retry_scope")
    for grant in value.grants:
        candidate = read_candidate(store, grant.candidate_id)
        snap = candidate.snapshot
        if (
            candidate.root_operation_id != value.root
            or (
                snap.task_id,
                snap.revision,
                snap.stage,
                snap.input_digest,
                snap.execution_digest,
                snap.model,
                snap.endpoint,
                snap.group,
            )
            != (
                value.task_id,
                value.revision,
                value.stage,
                value.input_digest,
                grant.digest,
                grant.model,
                grant.endpoint,
                grant.group,
            )
            or not 1 <= grant.max_attempts <= 3
        ):
            raise ValueError("retry_scope_mismatch")


def _record(store: Store, value: HostScope, kind: str) -> None:
    _valid(store, value)
    if isinstance(value, CostScope) and (
        not re.fullmatch(r"[A-Z]{3}", value.currency)
        or type(value.max_minor) is not int
        or type(value.per_attempt_minor) is not int
        or not 0 < value.per_attempt_minor <= value.max_minor
        or value.max_minor > 9223372036854775807
        or not value.price_reference
    ):
        raise ValueError("invalid_cost_scope")
    with closing(store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if not (
            _verify_host_source(db, value)
            if kind == "host"
            else isinstance(value, CostScope)
            and _verify_cost_source(db, value)
            and _verify_price_source(db, value)
        ):
            raise ValueError("retry_scope_source_unverified")
        _schema(db)
        payload = json.dumps(asdict(value), sort_keys=True, separators=(",", ":"))
        old = db.execute(
            "SELECT kind,payload FROM retry_scope_evidence WHERE scope_id=?",
            (value.scope_id,),
        ).fetchone()
        if old and old != (kind, payload):
            raise ValueError("retry_scope_conflict")
        if not old:
            db.execute(
                "INSERT INTO retry_scope_evidence VALUES(?,?,?)",
                (value.scope_id, kind, payload),
            )


def record_host_scope(store: Store, value: HostScope) -> None:
    _record(store, value, "host")


def record_cost_scope(store: Store, value: CostScope) -> None:
    _record(store, value, "cost")


def _read(db: sqlite3.Connection, scope_id: str, kind: str) -> HostScope:
    row = db.execute(
        "SELECT payload FROM retry_scope_evidence WHERE scope_id=? AND kind=?",
        (scope_id, kind),
    ).fetchone()
    if (
        not row
        or db.execute(
            "SELECT 1 FROM retry_scope_revocation WHERE scope_id=?", (scope_id,)
        ).fetchone()
    ):
        raise ValueError("retry_scope_unavailable")
    values = json.loads(row[0])
    values["grants"] = tuple(Grant(**g) for g in values["grants"])
    value = CostScope(**values) if kind == "cost" else HostScope(**values)
    if value.expires_at <= time.time() or not (
        _verify_host_source(db, value)
        if kind == "host"
        else isinstance(value, CostScope)
        and _verify_cost_source(db, value)
        and _verify_price_source(db, value)
    ):
        raise ValueError("retry_scope_unverified_or_expired")
    return value


def reserve_eligibility(
    service: LocalEligibility,
    task: Task,
    revision: Revision,
    refs: list[Reference],
    prompt: PromptRevision,
    snapshot: ExecutionSnapshot,
    confirmation: Confirmation,
    operation_id: str,
    scope: OperationScope,
    *,
    root: str,
    candidate_id: str,
    host_id: str,
    cost_id: str,
) -> dict[str, Any]:
    """Derive one local qualification and reserve risk; never create authority."""
    from . import offline_submission

    if not confirmation.confirmation_id:
        raise ValueError("confirmation_id_required")
    candidate = read_candidate(service.store, candidate_id)
    alternate = candidate.source_candidate_id is not None
    service._validate(
        task,
        revision,
        refs,
        prompt,
        snapshot,
        confirmation,
        operation_id,
        scope,
        candidate_id=candidate_id if alternate else None,
    )
    with closing(service.store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _schema(db)
        service._current(db, task, revision)
        service._current_review(db, snapshot)
        offline_submission._check_business_gate(db, service, task, revision)
        if not offline_submission._verify_business_choices(
            snapshot
        ) or not _verify_candidate_capability(db, candidate_id, snapshot):
            raise ValueError("business_or_capability_unverified")
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_consumption'"
            ).fetchone()
            and db.execute(
                "SELECT 1 FROM risk_consumption WHERE linked_operation=?", (root,)
            ).fetchone()
        ):
            raise ValueError("risk_linked_operation_not_retryable")
        host = _read(db, host_id, "host")
        cost = _read(db, cost_id, "cost")
        if (
            not isinstance(cost, CostScope)
            or candidate.snapshot != snapshot
            or candidate.root_operation_id != root
        ):
            raise ValueError("retry_scope_mismatch")
        expected = (
            root,
            task.task_id,
            revision.revision,
            snapshot.stage,
            snapshot.input_digest,
        )
        if any(
            (v.root, v.task_id, v.revision, v.stage, v.input_digest) != expected
            for v in (host, cost)
        ):
            raise ValueError("retry_scope_mismatch")
        grants = [
            next(
                (
                    g
                    for g in v.grants
                    if g.candidate_id == candidate_id
                    and (g.digest, g.model, g.endpoint, g.group)
                    == (
                        snapshot.execution_digest,
                        snapshot.model,
                        snapshot.endpoint,
                        snapshot.group,
                    )
                ),
                None,
            )
            for v in (host, cost)
        ]
        if any(g is None for g in grants):
            raise ValueError("candidate_not_authorized")
        row = db.execute(
            "SELECT confirmation_id,execution_digest,scope_digest,expires_at,invalidated,consumed FROM local_eligibility WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row and (
            row[:3]
            != (confirmation.confirmation_id, snapshot.execution_digest, scope.digest())
            or row[3] <= time.time()
            or row[4:] != (0, 0)
        ):
            raise ValueError("eligibility_unavailable")
        existing = db.execute(
            "SELECT root,position,candidate_id,host_id,cost_id,currency,reserved_minor FROM retry_scope_reservation WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if existing:
            if (existing[0], existing[2], existing[3], existing[4]) != (
                root,
                candidate_id,
                host_id,
                cost_id,
            ):
                raise ValueError("reservation_conflict")
            return {"currency": existing[5], "reserved_minor": existing[6]}
        history = db.execute(
            "SELECT attempt_count FROM operation WHERE operation_id=? AND task_id=? AND revision=?",
            (root, task.task_id, revision.revision),
        ).fetchone()
        counts = db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id=? AND model=? AND endpoint=?",
            (root, snapshot.model, snapshot.endpoint),
        ).fetchone()
        if (
            not history
            or history[0] >= min(host.max_total, cost.max_total, 6)
            or (counts[0] if counts else 0)
            >= min(g.max_attempts for g in grants if g is not None)
        ):
            raise ValueError("retry_budget_exhausted")
        chain = db.execute(
            "SELECT attempt_id,execution_digest FROM operation_attempt WHERE operation_id=? ORDER BY sequence",
            (root,),
        ).fetchall()
        counters = db.execute(
            "SELECT SUM(count) FROM attempt_counter WHERE operation_id=?", (root,)
        ).fetchone()
        if len(chain) != history[0] or not counters or counters[0] != history[0]:
            raise ValueError("retry_history_incomplete")
        if not alternate and service.store.attempt_chain_status(root) not in {
            "initial_recorded",
            "second_recorded",
        }:
            raise ValueError("retry_history_incomplete")
        predecessor = chain[-1][0]
        previous = db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?", (predecessor,)
        ).fetchone()
        from .send_evidence import verified_recoverability

        if not previous or (
            previous[0] != "failed"
            and verified_recoverability(
                db, root, predecessor, task.task_id, revision.revision, chain[-1][1]
            )
            != "transient_local"
        ):
            raise ValueError("predecessor_unresolved")
        wait = db.execute(
            "SELECT started,deadline,observed FROM retry_wait WHERE operation_id=? AND attempt_id=?"
            if history[0] == 1
            else "SELECT started,deadline,observed FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
            (root, predecessor),
        ).fetchone()
        if not wait or time.time() < max(wait):
            raise ValueError("retry_wait_unavailable")
        if not alternate and not offline_submission._verify_retry_evidence(
            db, root, predecessor, snapshot, confirmation.confirmation_id
        ):
            raise ValueError("retry_evidence_unverified")
        if alternate:
            from . import candidate_preflight

            if not candidate_preflight.preflight(service, root, candidate_id).eligible:
                raise ValueError("candidate_preflight_unverified")
        position = history[0] + 1
        used = db.execute(
            "SELECT COALESCE(SUM(reserved_minor),0) FROM retry_scope_reservation WHERE cost_id=?",
            (cost_id,),
        ).fetchone()[0]
        if used + cost.per_attempt_minor > cost.max_minor:
            raise ValueError("cost_scope_exhausted")
        if not row:
            if db.execute(
                "SELECT 1 FROM operation WHERE operation_id=?", (operation_id,)
            ).fetchone():
                raise ValueError("operation_already_exists")
            db.execute(
                "UPDATE local_eligibility SET invalidated=1 WHERE task_id=? AND consumed=0",
                (task.task_id,),
            )
            db.execute(
                "INSERT INTO local_eligibility(operation_id,confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    confirmation.confirmation_id,
                    task.task_id,
                    revision.revision,
                    revision.input_digest,
                    prompt.prompt_revision,
                    prompt.prompt_digest,
                    snapshot.execution_digest,
                    scope.digest(),
                    min(host.expires_at, cost.expires_at),
                ),
            )
            if alternate:
                db.execute(
                    "INSERT INTO candidate_eligibility VALUES(?,?,?)",
                    (operation_id, candidate_id, root),
                )
        try:
            db.execute(
                "INSERT INTO retry_scope_reservation VALUES(?,?,?,?,?,?,?,?,NULL)",
                (
                    operation_id,
                    root,
                    position,
                    candidate_id,
                    host_id,
                    cost_id,
                    cost.currency,
                    cost.per_attempt_minor,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError("retry_position_occupied") from None
        return {"currency": cost.currency, "reserved_minor": cost.per_attempt_minor}


def read_reservation(store: Store, operation_id: str) -> dict[str, Any]:
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT r.currency,r.reserved_minor,r.attempt_id,r.root,c.task_id FROM retry_scope_reservation r JOIN execution_candidate c ON c.candidate_id=r.candidate_id WHERE r.operation_id=?",
            (operation_id,),
        ).fetchone()
    if not row:
        raise ValueError("reservation_unavailable")
    known: dict[str, object] = {}
    if row[2] is not None:
        from .generation_cost import summarize_generation_cost

        summary = summarize_generation_cost(
            store, row[4], root=row[3], attempt_id=row[2]
        )
        known = dict(summary["known_subtotal"])  # type: ignore[arg-type]
    return {
        "currency": row[0],
        "reserved_minor": row[1],
        "attempt_id": row[2],
        "actual_cost": None,
        "known_actual_subtotal": known,
        "billing_completeness": "unverified",
    }


def read_cost_scope_balance(store: Store, scope_id: str) -> dict[str, Any]:
    """Return held risk, not an assertion that billing is complete."""
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        scope = _read(db, scope_id, "cost")
        if not isinstance(scope, CostScope):
            raise TypeError("cost_scope_unavailable")
        reserved = db.execute(
            "SELECT COALESCE(SUM(reserved_minor),0) FROM retry_scope_reservation WHERE cost_id=?",
            (scope_id,),
        ).fetchone()[0]
        if reserved < 0 or reserved > scope.max_minor:
            raise ValueError("cost_reservation_inconsistent")
        return {
            "currency": scope.currency,
            "limit_minor": scope.max_minor,
            "reserved_minor": reserved,
            "available_minor": scope.max_minor - reserved,
            "billing_completeness": "unverified",
        }


def has_consumed_reservation(
    db: sqlite3.Connection, operation_id: str, root: str | None, attempt_id: str
) -> bool:
    """Check immutable replay identity without reauthorizing a historical send."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retry_scope_reservation'"
    ).fetchone():
        return False
    return bool(
        db.execute(
            "SELECT 1 FROM retry_scope_reservation WHERE operation_id=? AND root=? AND attempt_id=?",
            (operation_id, root, attempt_id),
        ).fetchone()
    )


def requires_reservation(db: sqlite3.Connection, root: str | None) -> bool:
    """Once scope evidence exists for a root, legacy retry cannot bypass it."""
    if (
        root is None
        or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retry_scope_evidence'"
        ).fetchone()
    ):
        return False
    for (payload,) in db.execute("SELECT payload FROM retry_scope_evidence"):
        try:
            value = json.loads(payload)
            if not isinstance(value, dict) or not isinstance(value.get("root"), str):
                return True
            if value.get("root") == root:
                return True
        except (TypeError, ValueError):
            return True
    return False


def verify_reserved(
    db: sqlite3.Connection,
    operation_id: str,
    root: str | None,
    snapshot: ExecutionSnapshot,
    attempt_id: str | None = None,
) -> bool:
    """Revalidate a reservation at an existing submit transaction boundary."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retry_scope_reservation'"
    ).fetchone():
        return False
    row = db.execute(
        "SELECT root,position,candidate_id,host_id,cost_id,currency,reserved_minor,attempt_id FROM retry_scope_reservation WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if not row:
        return False
    host = _read(db, row[3], "host")
    cost = _read(db, row[4], "cost")
    if (
        not isinstance(cost, CostScope)
        or row[0] != root
        or row[5:7] != (cost.currency, cost.per_attempt_minor)
        or row[7] != attempt_id
    ):
        raise ValueError("retry_reservation_mismatch")
    candidate = db.execute(
        "SELECT root_operation_id,task_id,revision,execution_digest,snapshot_payload FROM execution_candidate WHERE candidate_id=?",
        (row[2],),
    ).fetchone()
    if candidate != (
        root,
        snapshot.task_id,
        snapshot.revision,
        snapshot.execution_digest,
        snapshot.to_payload(),
    ):
        raise ValueError("retry_candidate_changed")
    if not _verify_candidate_capability(db, row[2], snapshot):
        raise ValueError("retry_candidate_capability_unverified")
    history = db.execute(
        "SELECT attempt_count FROM operation WHERE operation_id=? AND task_id=? AND revision=?",
        (root, snapshot.task_id, snapshot.revision),
    ).fetchone()
    expected_count = row[1] if attempt_id is not None else row[1] - 1
    if (
        not history
        or history[0] != expected_count
        or row[1] > min(host.max_total, cost.max_total, 6)
    ):
        raise ValueError("retry_reservation_position_changed")
    if (
        db.execute(
            "SELECT COALESCE(SUM(reserved_minor),0) FROM retry_scope_reservation WHERE cost_id=?",
            (row[4],),
        ).fetchone()[0]
        > cost.max_minor
    ):
        raise ValueError("retry_cost_reservation_exceeded")
    for value in (host, cost):
        if (
            value.root,
            value.task_id,
            value.revision,
            value.stage,
            value.input_digest,
        ) != (
            root,
            snapshot.task_id,
            snapshot.revision,
            snapshot.stage,
            snapshot.input_digest,
        ) or not any(
            (g.candidate_id, g.digest, g.model, g.endpoint, g.group)
            == (
                row[2],
                snapshot.execution_digest,
                snapshot.model,
                snapshot.endpoint,
                snapshot.group,
            )
            for g in value.grants
        ):
            raise ValueError("retry_reservation_scope_changed")
    return True
