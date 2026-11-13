"""Form review records: local facts only, never an authorization authority."""

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field

from ecommerce_product_image_creator.storage import Store


class ReviewError(ValueError):
    """Sanitized review validation failure."""


@dataclass(frozen=True)
class FormReview:
    review_id: str
    task_id: str
    form_revision: int
    artifact_id: str
    artifact_digest: str
    decision: str
    source: str
    created_at: float
    evidence_reference: str | None
    expires_at: float | None = None
    invalidated: bool = False
    invalidation_reason: str | None = None
    trust_state: str = field(default="unverified", init=False)


def _schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS form_review_detail (
        review_id TEXT PRIMARY KEY, decision TEXT NOT NULL CHECK(decision IN ('accept','reject','pending')),
        created_at REAL NOT NULL, evidence_reference TEXT, invalidation_reason TEXT, expires_at REAL,
        trust_state TEXT NOT NULL DEFAULT 'unverified' CHECK(trust_state='unverified'),
        FOREIGN KEY(review_id) REFERENCES form_review(review_id))""")


def save_review(store: Store, review: FormReview) -> None:
    if (
        not all(
            (
                review.review_id,
                review.task_id,
                review.artifact_id,
                review.artifact_digest,
                review.source,
            )
        )
        or review.form_revision < 1
        or review.decision not in {"accept", "reject", "pending"}
        or review.created_at <= 0
        or not math.isfinite(review.created_at)
        or review.invalidated
        or review.invalidation_reason is not None
        or (
            review.expires_at is not None
            and (
                not math.isfinite(review.expires_at)
                or review.expires_at <= review.created_at
            )
        )
    ):
        raise ReviewError("Invalid review record")
    try:
        with closing(store._connect()) as db, db:
            _schema(db)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT t.task_id,t.revision,a.sha256,t.upstream_status,t.delivery_status,p.stage FROM artifact a JOIN attempt t ON t.attempt_id=a.attempt_id JOIN prepared_execution p ON p.task_id=t.task_id AND p.revision=t.revision WHERE a.artifact_id=?""",
                (review.artifact_id,),
            ).fetchone()
            if row != (
                review.task_id,
                review.form_revision,
                review.artifact_digest,
                "succeeded",
                "delivered",
                "form",
            ):
                raise ReviewError("Review artifact association mismatch")
            prior = db.execute(
                "SELECT 1 FROM form_review WHERE review_id=?", (review.review_id,)
            ).fetchone()
            if prior:
                raise ReviewError("Review identifier already exists")
            db.execute(
                "INSERT INTO form_review VALUES(?,?,?,?,?,?)",
                (
                    review.review_id,
                    review.task_id,
                    review.form_revision,
                    review.artifact_id,
                    review.artifact_digest,
                    review.source,
                ),
            )
            db.execute(
                "INSERT INTO form_review_detail(review_id,decision,created_at,evidence_reference,invalidation_reason,expires_at) VALUES(?,?,?,?,?,?)",
                (
                    review.review_id,
                    review.decision,
                    review.created_at,
                    review.evidence_reference,
                    None,
                    review.expires_at,
                ),
            )
    except sqlite3.Error:
        raise ReviewError("Review storage failure") from None


def get_review(store: Store, review_id: str) -> FormReview:
    try:
        with closing(store._connect()) as db:
            row = db.execute(
                """SELECT r.review_id,r.task_id,r.form_revision,r.artifact_id,r.artifact_sha256,d.decision,r.source,d.created_at,d.evidence_reference,d.invalidation_reason,d.expires_at,EXISTS(SELECT 1 FROM form_review_invalidation i WHERE i.review_id=r.review_id) FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id WHERE r.review_id=?""",
                (review_id,),
            ).fetchone()
        if not row:
            raise ReviewError("Review not found")
        return FormReview(
            *row[:9],
            expires_at=row[10],
            invalidated=row[9] is not None or bool(row[11]),
            invalidation_reason=row[9],
        )
    except sqlite3.Error:
        raise ReviewError("Review storage failure") from None


def invalidate_review(store: Store, review_id: str, reason: str) -> None:
    if not reason.strip():
        raise ReviewError("Invalidation reason required")
    try:
        with closing(store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE form_review_detail SET invalidation_reason=? WHERE review_id=? AND invalidation_reason IS NULL",
                (reason, review_id),
            )
            if result.rowcount != 1:
                raise ReviewError("Review absent or already invalidated")
            db.execute(
                "INSERT OR IGNORE INTO form_review_invalidation VALUES(?)", (review_id,)
            )
    except sqlite3.Error:
        raise ReviewError("Review storage failure") from None
