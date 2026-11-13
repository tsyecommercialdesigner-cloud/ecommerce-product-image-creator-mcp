"""Offline query evidence; never grants authority or infers a zero bill."""

import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

from .storage import Store


@dataclass(frozen=True)
class BillingEvidence:
    invocation_id: str
    root: str
    candidate_id: str
    attempt_id: str
    execution_digest: str
    provider: str
    account_scope: str
    event_id: str
    amount: str | None
    currency: str | None
    usage_quantity: str | None
    usage_unit: str | None
    source: str


@dataclass(frozen=True)
class UsageEvidence:
    evidence_id: str
    invocation_id: str
    root: str
    candidate_id: str
    attempt_id: str
    execution_digest: str
    provider: str
    account_scope: str
    quantity: str
    unit: str
    source: str


def _verify_evidence_source(
    db: sqlite3.Connection, evidence: BillingEvidence | UsageEvidence
) -> bool:
    """Production has no independently verified billing or usage source."""
    return False


def _number(value: str) -> str:
    if len(value) > 64:
        raise ValueError("invalid_quantity")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid_quantity") from None
    if (
        not amount.is_finite()
        or amount < 0
        or amount.adjusted() > 18
        or amount.adjusted() < -18
    ):
        raise ValueError("invalid_quantity")
    value = format(amount, "f")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return "0" if amount.is_zero() else value


def _add_exact(left: Decimal, right: Decimal) -> Decimal:
    a, b = left.as_tuple(), right.as_tuple()
    assert isinstance(a.exponent, int) and isinstance(b.exponent, int)
    precision = (
        max(
            len(a.digits) + max(0, a.exponent - b.exponent),
            len(b.digits) + max(0, b.exponent - a.exponent),
        )
        + 1
    )
    with localcontext() as context:
        context.prec = max(precision, 28)
        return left + right


def _label(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value))


def _verify_owner(db: sqlite3.Connection, e: BillingEvidence | UsageEvidence) -> None:
    row = db.execute(
        "SELECT root_operation_id,candidate_id,attempt_id,execution_digest,provider,account_scope,status FROM query_invocation WHERE invocation_id=?",
        (e.invocation_id,),
    ).fetchone()
    if (
        row is None
        or row[:6]
        != (
            e.root,
            e.candidate_id,
            e.attempt_id,
            e.execution_digest,
            e.provider,
            e.account_scope,
        )
        or row[6] not in {"entered", "response_received", "observed"}
    ):
        raise ValueError("evidence_identity_mismatch")
    if not all(
        _label(value) for value in (e.provider, e.account_scope, e.source)
    ) or not _verify_evidence_source(db, e):
        raise ValueError("evidence_source_unverified")


def record_billing_evidence(store: Store, evidence: BillingEvidence) -> str:
    """Record one authenticated scoped event; duplicate evidence is idempotent."""
    e = evidence
    if (
        not _label(e.event_id)
        or (e.amount is None) != (e.currency is None)
        or (e.usage_quantity is None) != (e.usage_unit is None)
    ):
        raise ValueError("invalid_billing_evidence")
    if e.currency is not None and not re.fullmatch(r"[A-Z]{3}", e.currency):
        raise ValueError("invalid_billing_evidence")
    if e.usage_unit is not None and not _label(e.usage_unit):
        raise ValueError("invalid_billing_evidence")
    amount = _number(e.amount) if e.amount is not None else None
    quantity = _number(e.usage_quantity) if e.usage_quantity is not None else None
    values = (e.invocation_id, amount, e.currency, quantity, e.usage_unit, e.source)
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _verify_owner(db, e)
            prior = db.execute(
                "SELECT invocation_id,amount,currency,usage_quantity,usage_unit,source FROM query_billing_event WHERE provider=? AND account_scope=? AND event_id=?",
                (e.provider, e.account_scope, e.event_id),
            ).fetchone()
            if prior is not None:
                if prior != values:
                    raise ValueError("billing_event_conflict")
                db.rollback()
                return e.event_id
            db.execute(
                "INSERT INTO query_billing_event VALUES (?,?,?,?,?,?,?,?,?,?)",
                (e.provider, e.account_scope, e.event_id, *values, time.time()),
            )
            db.commit()
            return e.event_id
        except BaseException:
            db.rollback()
            raise


def record_usage_evidence(store: Store, evidence: UsageEvidence) -> str:
    e = evidence
    if not _label(e.evidence_id) or not _label(e.unit):
        raise ValueError("invalid_usage_evidence")
    values = (
        e.invocation_id,
        e.provider,
        e.account_scope,
        _number(e.quantity),
        e.unit,
        e.source,
    )
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _verify_owner(db, e)
            prior = db.execute(
                "SELECT invocation_id,provider,account_scope,quantity,unit,source FROM query_usage_evidence WHERE evidence_id=?",
                (e.evidence_id,),
            ).fetchone()
            if prior is not None:
                if prior != values:
                    raise ValueError("usage_evidence_conflict")
                db.rollback()
                return e.evidence_id
            db.execute(
                "INSERT INTO query_usage_evidence VALUES (?,?,?,?,?,?,?,?)",
                (e.evidence_id, *values, time.time()),
            )
            db.commit()
            return e.evidence_id
        except BaseException:
            db.rollback()
            raise


def summarize_query_evidence(
    store: Store, root: str, candidate_id: str, attempt_id: str
) -> dict[str, object]:
    """Known evidence is not an exhaustive or reconciled supplier bill."""
    with closing(store._connect()) as db:
        calls = db.execute(
            "SELECT invocation_id,status,account_scope,observation_id FROM query_invocation WHERE root_operation_id=? AND candidate_id=? AND attempt_id=? ORDER BY rowid",
            (root, candidate_id, attempt_id),
        ).fetchall()
        amounts = db.execute(
            "SELECT b.currency,b.amount FROM query_billing_event b JOIN query_invocation q ON q.invocation_id=b.invocation_id WHERE q.root_operation_id=? AND q.candidate_id=? AND q.attempt_id=? AND b.amount IS NOT NULL",
            (root, candidate_id, attempt_id),
        ).fetchall()
        usage_count = db.execute(
            "SELECT count(*) FROM query_usage_evidence u JOIN query_invocation q ON q.invocation_id=u.invocation_id WHERE q.root_operation_id=? AND q.candidate_id=? AND q.attempt_id=?",
            (root, candidate_id, attempt_id),
        ).fetchone()[0]
    totals: dict[str, Decimal] = {}
    for currency, amount in amounts:
        totals[currency] = _add_exact(totals.get(currency, Decimal(0)), Decimal(amount))
    return {
        "invocation_count": len(calls),
        "invocations": calls,
        "usage_evidence_count": usage_count,
        "known_event_amounts": {key: str(value) for key, value in totals.items()},
        "cost_status": "unknown",
        "billing_completeness": "unverified",
    }
