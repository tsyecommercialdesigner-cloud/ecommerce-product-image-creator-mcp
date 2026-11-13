"""Offline generation billing evidence; never grants execution authority."""

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .domain import ExecutionSnapshot
from .query_usage import _add_exact, _label
from .storage import Store


@dataclass(frozen=True)
class GenerationCostEvidence:
    evidence_id: str
    task_id: str
    revision: int
    stage: str
    root: str
    candidate_id: str
    attempt_id: str
    execution_digest: str
    provider: str
    model: str
    endpoint: str
    group: str
    account_scope: str
    source: str
    evidence_reference: str
    event_id: str | None
    amount: str | None
    currency: str | None
    usage_quantity: str | None = None
    usage_unit: str | None = None


def _verify_evidence_source(
    db: sqlite3.Connection, evidence: GenerationCostEvidence
) -> bool:
    """No production billing identity, scope or source verifier exists."""
    return False


def _exact_number(value: str) -> str:
    if len(value) > 64:
        raise ValueError("invalid_quantity")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid_quantity") from None
    if not number.is_finite() or number < 0 or not -18 <= number.adjusted() <= 18:
        raise ValueError("invalid_quantity")
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if number.is_zero() else text


def _schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS generation_cost_evidence (
        evidence_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
        stage TEXT NOT NULL, root TEXT NOT NULL, candidate_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL, execution_digest TEXT NOT NULL, provider TEXT NOT NULL,
        model TEXT NOT NULL, endpoint TEXT NOT NULL, grouping TEXT NOT NULL,
        account_scope TEXT NOT NULL, source TEXT NOT NULL, evidence_reference TEXT NOT NULL,
        event_id TEXT, amount TEXT, currency TEXT, usage_quantity TEXT, usage_unit TEXT,
        recorded_at REAL NOT NULL,
        UNIQUE(provider,account_scope,event_id),
        FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
        FOREIGN KEY(candidate_id) REFERENCES execution_candidate(candidate_id),
        FOREIGN KEY(root) REFERENCES operation(operation_id))""")
    for action in ("UPDATE", "DELETE"):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS generation_cost_no_{action.lower()} BEFORE {action} ON generation_cost_evidence BEGIN SELECT RAISE(ABORT,'immutable generation cost'); END"
        )
    db.execute("""CREATE TABLE IF NOT EXISTS supplier_cost_raw (
        evidence_id TEXT PRIMARY KEY, raw_amount TEXT NOT NULL,
        response_digest TEXT NOT NULL, unit_source TEXT NOT NULL,
        FOREIGN KEY(evidence_id) REFERENCES generation_cost_evidence(evidence_id))""")
    for action in ("UPDATE", "DELETE"):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS supplier_cost_raw_no_{action.lower()} BEFORE {action} ON supplier_cost_raw BEGIN SELECT RAISE(ABORT,'immutable supplier cost'); END"
        )


def record_generation_cost(
    store: Store,
    evidence: GenerationCostEvidence,
    *,
    _raw: tuple[str, bytes] | None = None,
) -> str:
    """Bind one verified event to an actual attempt; never send or alter it."""
    e = evidence
    if (
        not all(
            _label(x)
            for x in (
                e.evidence_id,
                e.provider,
                e.account_scope,
                e.source,
                e.evidence_reference,
            )
        )
        or (e.event_id is not None and not _label(e.event_id))
        or (e.amount is None) != (e.currency is None)
        or (e.usage_quantity is None) != (e.usage_unit is None)
        or (e.currency is not None and not re.fullmatch(r"[A-Z]{3}", e.currency))
        or (e.usage_unit is not None and not _label(e.usage_unit))
    ):
        raise ValueError("invalid_cost_evidence")
    amount = _exact_number(e.amount) if e.amount is not None else None
    quantity = _exact_number(e.usage_quantity) if e.usage_quantity is not None else None
    values = (
        e.task_id,
        e.revision,
        e.stage,
        e.root,
        e.candidate_id,
        e.attempt_id,
        e.execution_digest,
        e.provider,
        e.model,
        e.endpoint,
        e.group,
        e.account_scope,
        e.source,
        e.evidence_reference,
        e.event_id,
        amount,
        e.currency,
        quantity,
        e.usage_unit,
    )
    with closing(store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _schema(db)
        candidate = db.execute(
            "SELECT root_operation_id,task_id,revision,source_candidate_id,snapshot_payload,execution_digest FROM execution_candidate WHERE candidate_id=?",
            (e.candidate_id,),
        ).fetchone()
        if (
            not candidate
            or candidate[:3] != (e.root, e.task_id, e.revision)
            or candidate[5] != e.execution_digest
        ):
            raise ValueError("candidate_identity_mismatch")
        snapshot = ExecutionSnapshot.from_payload(candidate[4])
        if (
            snapshot.digest_version != 2
            or snapshot.execution_digest != e.execution_digest
            or (
                snapshot.task_id,
                snapshot.revision,
                snapshot.stage,
                snapshot.model,
                snapshot.endpoint,
                snapshot.group,
            )
            != (e.task_id, e.revision, e.stage, e.model, e.endpoint, e.group)
        ):
            raise ValueError("snapshot_mismatch")
        attempt = db.execute(
            "SELECT c.execution_digest,c.model,c.endpoint,a.task_id,a.revision,o.digest,t.phase,t.logical_operation_id,t.task_id,t.revision,t.execution_digest,t.reason,t.source FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id JOIN operation o ON o.attempt_id=a.attempt_id LEFT JOIN attempt_transport_evidence t ON t.attempt_id=a.attempt_id WHERE c.operation_id=? AND c.attempt_id=?",
            (e.root, e.attempt_id),
        ).fetchone()
        association = db.execute(
            "SELECT candidate_id,root_operation_id FROM candidate_attempt WHERE attempt_id=?",
            (e.attempt_id,),
        ).fetchone()
        if (
            attempt
            != (
                e.execution_digest,
                e.model,
                e.endpoint,
                e.task_id,
                e.revision,
                e.execution_digest,
                "entered",
                e.root,
                e.task_id,
                e.revision,
                e.execution_digest,
                "dispatch_boundary",
                "server_pre_dispatch",
            )
            or (candidate[3] is None and association is not None)
            or (candidate[3] is not None and association != (e.candidate_id, e.root))
        ):
            raise ValueError("attempt_identity_mismatch")
        from .send_evidence import verified_linked_stop

        if verified_linked_stop(
            db, e.root, e.attempt_id, e.task_id, e.revision, e.execution_digest
        ):
            raise ValueError("conflicting_transport_evidence")
        if e.provider != "candidate_gateway" or not _verify_evidence_source(db, e):
            raise ValueError("evidence_source_unverified")
        if _raw is not None and not _verify_supplier_response(db, e, _raw[1]):
            raise ValueError("supplier_response_source_unverified")
        if _raw is not None:
            response = json.loads(_raw[1])
            response_data = response.get("data") if isinstance(response, dict) else None
            bound = db.execute(
                "SELECT upstream_id FROM attempt WHERE attempt_id=? AND task_id=? AND revision=?",
                (e.attempt_id, e.task_id, e.revision),
            ).fetchone()
            if (
                not isinstance(response_data, dict)
                or not bound
                or not bound[0]
                or response_data.get("task_id") != bound[0]
            ):
                raise ValueError("supplier_response_identity_conflict")
        prior = db.execute(
            "SELECT evidence_id,task_id,revision,stage,root,candidate_id,attempt_id,execution_digest,provider,model,endpoint,grouping,account_scope,source,evidence_reference,event_id,amount,currency,usage_quantity,usage_unit FROM generation_cost_evidence WHERE evidence_id=? OR (event_id IS NOT NULL AND provider=? AND account_scope=? AND event_id=?)",
            (e.evidence_id, e.provider, e.account_scope, e.event_id),
        ).fetchall()
        if prior:
            previous = prior[0]
            same_event = (
                len(prior) == 1
                and e.event_id is not None
                and previous[1:14] == values[:13]
                and previous[15:] == values[14:]
            )
            if len(prior) != 1 or (
                previous[1:] != values
                and not (
                    (previous[0] != e.evidence_id or _raw is not None) and same_event
                )
            ):
                raise ValueError("generation_cost_conflict")
            result_id = str(prior[0][0])
            if _raw is not None:
                _record_raw(db, result_id, *_raw)
            return result_id
        db.execute(
            "INSERT INTO generation_cost_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e.evidence_id, *values, time.time()),
        )
        if _raw is not None:
            _record_raw(db, e.evidence_id, *_raw)
        return e.evidence_id


def _verify_supplier_response(
    db: sqlite3.Connection, evidence: GenerationCostEvidence, raw: bytes
) -> bool:
    """Production has no independently verified response and event source."""
    return False


def _record_raw(
    db: sqlite3.Connection, evidence_id: str, raw_amount: str, raw: bytes
) -> None:
    values = (
        raw_amount,
        hashlib.sha256(raw).hexdigest(),
        "provider_contract_sunburst_thb",
    )
    previous = db.execute(
        "SELECT raw_amount,response_digest,unit_source FROM supplier_cost_raw WHERE evidence_id=?",
        (evidence_id,),
    ).fetchone()
    if previous is not None and (
        _exact_number(previous[0]) != _exact_number(raw_amount)
        or previous[2] != values[2]
    ):
        raise ValueError("supplier_cost_raw_conflict")
    if previous is None:
        db.execute(
            "INSERT INTO supplier_cost_raw VALUES(?,?,?,?)", (evidence_id, *values)
        )


class _JsonNumber(str):
    pass


def record_supplier_response_cost(
    store: Store,
    root: str,
    candidate_id: str,
    task_id: str,
    revision: int,
    attempt_id: str,
    digest: str,
    raw: bytes,
    event_id: str,
    account_scope: str,
) -> str:
    """Persist a verified exact response amount, never authorizing a request."""
    from .execution_candidates import read_candidate
    from .provider import ENDPOINT

    if not isinstance(raw, bytes) or len(raw) > 1_000_000 or not _label(event_id):
        raise ValueError("invalid_supplier_response")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate_response_field")
            out[key] = value
        return out

    try:
        payload = json.loads(
            raw,
            parse_float=_JsonNumber,
            parse_int=_JsonNumber,
            parse_constant=lambda _: None,
            object_pairs_hook=unique_pairs,
        )
    except (ValueError, UnicodeDecodeError):
        raise ValueError("invalid_supplier_response") from None
    data = payload.get("data") if isinstance(payload, dict) else None
    candidate = read_candidate(store, candidate_id)
    snap = candidate.snapshot
    if (
        candidate.root_operation_id != root
        or (snap.task_id, snap.revision, snap.execution_digest)
        != (task_id, revision, digest)
        or snap.model != "gpt-image-2.5-sunburst"
        or snap.endpoint != ENDPOINT
    ):
        raise ValueError("supplier_contract_unverified")
    with closing(store._connect()) as db:
        upstream = db.execute(
            "SELECT upstream_id FROM attempt WHERE attempt_id=? AND task_id=? AND revision=?",
            (attempt_id, task_id, revision),
        ).fetchone()
    if (
        not isinstance(payload, dict)
        or ("code" in payload and payload["code"] != "success")
        or not isinstance(data, dict)
        or not upstream
        or not upstream[0]
        or data.get("task_id") != upstream[0]
        or data.get("status") not in ("SUCCESS", "FAILURE")
        or data.get("currency", "THB") != "THB"
    ):
        raise ValueError("supplier_response_identity_conflict")
    amount = data.get("cost")
    if not isinstance(amount, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", amount
    ):
        raise ValueError("invalid_cost_evidence")
    _exact_number(amount)
    evidence_id = (
        "supplier-"
        + hashlib.sha256((account_scope + "\0" + event_id).encode()).hexdigest()
    )
    evidence = GenerationCostEvidence(
        evidence_id,
        task_id,
        revision,
        snap.stage,
        root,
        candidate_id,
        attempt_id,
        digest,
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        account_scope,
        "supplier_response",
        hashlib.sha256(raw).hexdigest(),
        event_id,
        amount,
        "THB",
    )
    return record_generation_cost(store, evidence, _raw=(amount, raw))


def read_supplier_response_costs(
    store: Store, root: str, attempt_id: str
) -> list[dict[str, str]]:
    with closing(store._connect()) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='supplier_cost_raw'"
        ).fetchone():
            return []
        rows = db.execute(
            "SELECT r.evidence_id,r.raw_amount,e.currency,r.response_digest,r.unit_source FROM supplier_cost_raw r JOIN generation_cost_evidence e ON e.evidence_id=r.evidence_id WHERE e.root=? AND e.attempt_id=? ORDER BY e.rowid",
            (root, attempt_id),
        ).fetchall()
    return [
        {
            "evidence_id": row[0],
            "raw_amount": row[1],
            "currency": row[2],
            "unit": "฿",
            "response_digest": row[3],
            "unit_source": row[4],
            "source_status": "configured_verifier",
            "billing_completeness": "unverified",
        }
        for row in rows
    ]


def summarize_generation_cost(
    store: Store,
    task_id: str,
    *,
    root: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, object]:
    """Read scoped events and report subtotal separately from unverified completeness."""
    with closing(store._connect()) as db:
        clauses = ["a.task_id=?"]
        params: list[object] = [task_id]
        if root is not None:
            clauses.append("c.operation_id=?")
            params.append(root)
        if attempt_id is not None:
            clauses.append("a.attempt_id=?")
            params.append(attempt_id)
        attempts = db.execute(
            "SELECT DISTINCT c.operation_id,a.attempt_id FROM attempt a LEFT JOIN operation_attempt c ON c.attempt_id=a.attempt_id WHERE "
            + " AND ".join(clauses),
            params,
        ).fetchall()
        if not attempts:
            raise ValueError("attempt_scope_not_found")
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_cost_evidence'"
        ).fetchone()
        result: dict[str, object] = {}
        totals: dict[str, Decimal] = {}
        for operation, attempt in attempts:
            rows = (
                db.execute(
                    "SELECT event_id,amount,currency FROM generation_cost_evidence WHERE task_id=? AND root=? AND attempt_id=?",
                    (task_id, operation, attempt),
                ).fetchall()
                if exists
                else []
            )
            per: dict[str, Decimal] = {}
            for event_id, amount, currency in rows:
                if event_id is not None and amount is not None and currency is not None:
                    per[currency] = _add_exact(
                        per.get(currency, Decimal(0)), Decimal(amount)
                    )
                    totals[currency] = _add_exact(
                        totals.get(currency, Decimal(0)), Decimal(amount)
                    )
            result[attempt] = {
                "root": operation,
                "known_subtotal": {key: str(value) for key, value in per.items()},
                "evidence_count": len(rows),
                "source_verification": "configured_verifier" if rows else "unknown",
                "cost_status": "incomplete" if rows else "unknown",
                "total_verified": False,
            }
    return {
        "task_id": task_id,
        "root": root,
        "attempts": result,
        "known_subtotal": {key: str(value) for key, value in totals.items()},
        "total_verified": False,
        "billing_completeness": "unverified",
    }
