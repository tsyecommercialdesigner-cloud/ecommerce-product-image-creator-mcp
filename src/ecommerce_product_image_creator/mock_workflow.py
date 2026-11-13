"""Offline composite Mock bookkeeping; never grants human or billing authorization."""

import math
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecommerce_product_image_creator.domain import (
    ExecutionSnapshot,
    PromptRevision,
    Revision,
)
from ecommerce_product_image_creator.preparation import (
    check_mock_review_current,
    read_prepared,
)
from ecommerce_product_image_creator.storage import (
    FileSafetyError,
    ResourceBudget,
    Store,
    read_reference,
)


class MockWorkflowError(ValueError):
    """Sanitized workflow failure."""


@dataclass(frozen=True)
class MockReview:
    review_id: str
    task_id: str
    revision: int
    artifact_id: str
    digest: str
    decision: str
    source: str
    created_at: float
    evidence_reference: str | None
    expires_at: float | None = None
    view: str | None = None
    feedback: str | None = None
    trust_state: str = field(default="unverified", init=False)


def _mock_review_source_verified(review: MockReview) -> bool:
    return False


def _revision_source_verified(
    source: str, request_id: str, review: MockReview, reason: str
) -> bool:
    return False


def _schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS mock_review (
      review_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
      artifact_id TEXT NOT NULL, digest TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('accept','reject','pending')),
      source TEXT NOT NULL, created_at REAL NOT NULL, evidence_reference TEXT, expires_at REAL,
      view TEXT, feedback TEXT, invalidation_reason TEXT,
      trust_state TEXT NOT NULL DEFAULT 'unverified' CHECK(trust_state='unverified'),
      FOREIGN KEY(task_id,revision) REFERENCES revision(task_id,revision), FOREIGN KEY(artifact_id) REFERENCES artifact(artifact_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS mock_revision_request (
      request_id TEXT PRIMARY KEY, review_id TEXT NOT NULL, reason TEXT NOT NULL,
      source TEXT NOT NULL, created_at REAL NOT NULL, target_revision INTEGER,
      trust_state TEXT NOT NULL DEFAULT 'unverified' CHECK(trust_state='unverified'),
      FOREIGN KEY(review_id) REFERENCES mock_review(review_id))""")


def _checked_file(root: Path, path: Path, digest: str, budget: ResourceBudget) -> None:
    try:
        try:
            image = read_reference(root, path, "image/png", budget)
        except FileSafetyError:
            image = read_reference(root, path, "image/jpeg", budget)
        if image.sha256 != digest:
            raise MockWorkflowError("Artifact digest changed")
    except (FileSafetyError, OSError):
        raise MockWorkflowError("Artifact unavailable or invalid") from None


def register_mock_artifact(
    store: Store,
    task_id: str,
    revision: int,
    attempt_id: str,
    artifact_id: str,
    root: Path,
    path: Path,
    mime: str,
    digest: str,
    budget: ResourceBudget,
) -> None:
    if not all((task_id, attempt_id, artifact_id, digest)):
        raise MockWorkflowError("Invalid artifact association")
    try:
        image = read_reference(root, path, mime, budget)
        if image.sha256 != digest:
            raise MockWorkflowError("Artifact digest mismatch")
        saved = read_prepared(store, task_id, revision)
        if (
            saved["stage"] != "mock"
            or saved["digest_version"] != 2
            or not saved["form_source"]
        ):
            raise MockWorkflowError("Mock snapshot missing")
        with closing(store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if (
                read_prepared(store, task_id, revision)["snapshot_payload"]
                != saved["snapshot_payload"]
            ):
                raise MockWorkflowError("Mock snapshot changed")
            check_mock_review_current(db, task_id, revision)
            row = db.execute(
                "SELECT task_id,revision,upstream_status,delivery_status FROM attempt WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row not in [
                (task_id, revision, "succeeded", "not_available"),
                (task_id, revision, "succeeded", "pending"),
            ]:
                raise MockWorkflowError("Attempt cannot register Mock artifact")
            if db.execute(
                "SELECT 1 FROM artifact WHERE attempt_id=? OR artifact_id=?",
                (attempt_id, artifact_id),
            ).fetchone():
                raise MockWorkflowError("Artifact already registered or conflicts")
            db.execute(
                "INSERT INTO artifact VALUES(?,?,?,?)",
                (artifact_id, attempt_id, str(path), digest),
            )
            db.execute(
                "UPDATE attempt SET delivery_status='delivered' WHERE attempt_id=?",
                (attempt_id,),
            )
    except MockWorkflowError:
        raise
    except (ValueError, sqlite3.Error, OSError, MemoryError):
        raise MockWorkflowError("Cannot register Mock artifact safely") from None


def save_mock_review(store: Store, review: MockReview) -> None:
    if (
        not all(
            (
                review.review_id,
                review.task_id,
                review.artifact_id,
                review.digest,
                review.source.strip(),
            )
        )
        or review.revision < 1
        or review.decision not in {"accept", "reject", "pending"}
        or review.view not in {None, "F", "L", "R", "B", "T", "Q"}
        or not math.isfinite(review.created_at)
        or review.created_at <= 0
        or (
            review.expires_at is not None
            and (
                not math.isfinite(review.expires_at)
                or review.expires_at <= review.created_at
            )
        )
    ):
        raise MockWorkflowError("Invalid Mock review")
    try:
        with closing(store._connect()) as db, db:
            _schema(db)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT a.task_id,a.revision,a.upstream_status,a.delivery_status,x.sha256,p.stage FROM artifact x JOIN attempt a ON x.attempt_id=a.attempt_id JOIN prepared_execution p ON p.task_id=a.task_id AND p.revision=a.revision WHERE x.artifact_id=?""",
                (review.artifact_id,),
            ).fetchone()
            if row != (
                review.task_id,
                review.revision,
                "succeeded",
                "delivered",
                review.digest,
                "mock",
            ):
                raise MockWorkflowError("Mock review association mismatch")
            db.execute(
                "INSERT INTO mock_review(review_id,task_id,revision,artifact_id,digest,decision,source,created_at,evidence_reference,expires_at,view,feedback) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    review.review_id,
                    review.task_id,
                    review.revision,
                    review.artifact_id,
                    review.digest,
                    review.decision,
                    review.source,
                    review.created_at,
                    review.evidence_reference,
                    review.expires_at,
                    review.view,
                    review.feedback,
                ),
            )
    except sqlite3.Error:
        raise MockWorkflowError("Cannot save Mock review") from None


def invalidate_mock_review(store: Store, review_id: str, reason: str) -> None:
    if not reason.strip():
        raise MockWorkflowError("Invalid invalidation reason")
    try:
        with closing(store._connect()) as db, db:
            row = db.execute(
                "UPDATE mock_review SET invalidation_reason=? WHERE review_id=? AND invalidation_reason IS NULL",
                (reason, review_id),
            )
            if row.rowcount != 1:
                raise MockWorkflowError("Review absent or invalidated")
    except sqlite3.Error:
        raise MockWorkflowError("Cannot invalidate review") from None


def _load_review(
    db: sqlite3.Connection, review_id: str
) -> tuple[MockReview, str | None]:
    row = db.execute(
        "SELECT review_id,task_id,revision,artifact_id,digest,decision,source,created_at,evidence_reference,expires_at,view,feedback,invalidation_reason FROM mock_review WHERE review_id=?",
        (review_id,),
    ).fetchone()
    if not row:
        raise MockWorkflowError("Mock review not found")
    return MockReview(*row[:12]), row[12]


def read_mock_review_state(store: Store, review_id: str) -> dict[str, Any]:
    """Report the record and current applicability without asserting approval."""
    try:
        with closing(store._connect()) as db:
            review, invalid = _load_review(db, review_id)
            rows = db.execute(
                "SELECT decision,expires_at,invalidation_reason,review_id FROM mock_review WHERE task_id=? AND revision=? AND artifact_id=? AND digest=? ORDER BY rowid",
                (review.task_id, review.revision, review.artifact_id, review.digest),
            ).fetchall()
            active = {
                r[0]
                for r in rows
                if r[0] in {"accept", "reject"}
                and r[2] is None
                and (r[1] is None or r[1] > time.time())
            }
            state = (
                "conflict"
                if len(active) > 1
                else "invalidated"
                if invalid is not None
                else "expired"
                if review.expires_at is not None and review.expires_at <= time.time()
                else "superseded"
                if rows and rows[-1][3] != review_id
                else review.decision
            )
            return {"review": review, "status": state, "source_trust": "unverified"}
    except sqlite3.Error:
        raise MockWorkflowError("Cannot read Mock review") from None


def _check_review(db: sqlite3.Connection, review_id: str, decision: str) -> MockReview:
    review, invalid = _load_review(db, review_id)
    rows = db.execute(
        "SELECT review_id,decision,expires_at,invalidation_reason FROM mock_review WHERE task_id=? AND revision=? AND artifact_id=? AND digest=? ORDER BY rowid",
        (review.task_id, review.revision, review.artifact_id, review.digest),
    ).fetchall()
    active = {
        r[1]
        for r in rows
        if r[1] in {"accept", "reject"}
        and r[3] is None
        and (r[2] is None or r[2] > time.time())
    }
    if len(active) > 1:
        raise MockWorkflowError("Conflicting reviews require clarification")
    if (
        not rows
        or rows[-1][0] != review_id
        or invalid is not None
        or (review.expires_at is not None and review.expires_at <= time.time())
        or review.decision != decision
        or not _mock_review_source_verified(review)
    ):
        raise MockWorkflowError("Mock review is not current and verified")
    return review


def check_mock_approval(
    store: Store,
    review_id: str,
    task_id: str,
    revision: int,
    artifact_id: str,
    digest: str,
    artifact_root: Path,
    budget: ResourceBudget,
) -> None:
    """Check a specific current artifact and trusted review; never authorize billing."""
    try:
        with closing(store._connect()) as db:
            review = _check_review(db, review_id, "accept")
            check_mock_review_current(db, task_id, revision)
            saved = read_prepared(store, task_id, revision)
            source = saved["form_source"]
            source_path = (
                db.execute(
                    "SELECT path FROM artifact WHERE artifact_id=?",
                    (source["artifact_id"],),
                ).fetchone()
                if source
                else None
            )
            if not source_path:
                raise MockWorkflowError("Form source missing")
            _checked_file(artifact_root, Path(source_path[0]), source["sha256"], budget)
            latest = db.execute(
                "SELECT MAX(revision) FROM revision WHERE task_id=?", (task_id,)
            ).fetchone()
            artifact = db.execute(
                "SELECT x.path,a.task_id,a.revision,a.upstream_status,a.delivery_status,x.sha256 FROM artifact x JOIN attempt a ON a.attempt_id=x.attempt_id WHERE x.artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if (
                (review.task_id, review.revision, review.artifact_id, review.digest)
                != (task_id, revision, artifact_id, digest)
                or latest != (revision,)
                or not artifact
                or artifact[1:] != (task_id, revision, "succeeded", "delivered", digest)
            ):
                raise MockWorkflowError("Mock approval does not match current artifact")
            _checked_file(artifact_root, Path(artifact[0]), digest, budget)
    except MockWorkflowError:
        raise
    except (sqlite3.Error, ValueError, OSError):
        raise MockWorkflowError("Cannot verify Mock approval") from None


def record_mock_revision_request(
    store: Store,
    request_id: str,
    review: MockReview,
    reason: str,
    source: str,
    created_at: float,
) -> None:
    if (
        not all((request_id, reason.strip(), source.strip()))
        or not math.isfinite(created_at)
        or created_at <= 0
    ):
        raise MockWorkflowError("Invalid Mock revision request")
    try:
        with closing(store._connect()) as db, db:
            _schema(db)
            db.execute("BEGIN IMMEDIATE")
            stored, _ = _load_review(db, review.review_id)
            if stored != review or stored.decision != "reject":
                raise MockWorkflowError("Mock revision source mismatch")
            db.execute(
                "INSERT INTO mock_revision_request(request_id,review_id,reason,source,created_at) VALUES(?,?,?,?,?)",
                (request_id, review.review_id, reason, source, created_at),
            )
    except sqlite3.Error:
        raise MockWorkflowError("Cannot record Mock revision request") from None


def prepare_mock_revision(
    store: Store,
    request_id: str,
    prompt: str,
    artifact_root: Path,
    budget: ResourceBudget,
) -> dict[str, Any]:
    from ecommerce_product_image_creator import preparation

    if not request_id or not isinstance(prompt, str) or not prompt.strip():
        raise MockWorkflowError("Invalid Mock revision prompt")
    try:
        with closing(store._connect()) as db, db:
            preparation._tables(db)
            _schema(db)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT review_id,reason,source,target_revision FROM mock_revision_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row or row[3] is not None:
                raise MockWorkflowError("Revision request missing or consumed")
            review = _check_review(db, row[0], "reject")
            if not _revision_source_verified(row[2], request_id, review, row[1]):
                raise MockWorkflowError("Revision request source unverified")
            latest = db.execute(
                "SELECT MAX(revision) FROM revision WHERE task_id=?", (review.task_id,)
            ).fetchone()
            current = db.execute(
                "SELECT status,input_digest,instructions FROM task WHERE task_id=?",
                (review.task_id,),
            ).fetchone()
            if (
                not latest
                or latest[0] != review.revision
                or not current
                or current[0] not in {"draft", "awaiting_confirmation", "ready"}
            ):
                raise MockWorkflowError("Mock revision request is stale")
            saved = read_prepared(store, review.task_id, review.revision)
            snap = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
            if (
                saved["digest_version"] != 2
                or snap.digest_version != 2
                or snap.stage != "mock"
                or saved["input_digest"] != current[1]
                or saved["instructions"] != current[2]
            ):
                raise MockWorkflowError("Mock snapshot is invalid")
            check_mock_review_current(db, review.task_id, review.revision)
            artifact = db.execute(
                "SELECT x.path,x.sha256,a.attempt_id,a.task_id,a.revision,a.upstream_status,a.delivery_status FROM artifact x JOIN attempt a ON a.attempt_id=x.attempt_id WHERE x.artifact_id=?",
                (review.artifact_id,),
            ).fetchone()
            if (
                not artifact
                or artifact[1] != review.digest
                or artifact[3:]
                != (review.task_id, review.revision, "succeeded", "delivered")
            ):
                raise MockWorkflowError("Mock artifact association mismatch")
            _checked_file(artifact_root, Path(artifact[0]), review.digest, budget)
            form_source = saved["form_source"]
            source_path = db.execute(
                "SELECT path FROM artifact WHERE artifact_id=?",
                (form_source["artifact_id"],),
            ).fetchone()
            if not source_path:
                raise MockWorkflowError("Form source missing")
            _checked_file(
                artifact_root, Path(source_path[0]), form_source["sha256"], budget
            )
            newrev = review.revision + 1
            newprompt = PromptRevision.from_text(
                review.task_id, newrev, newrev, "mock", prompt
            )
            snapshot = ExecutionSnapshot.create(
                Revision(review.task_id, newrev, saved["input_digest"]),
                newprompt,
                "mock",
                snap.model,
                snap.endpoint,
                snap.group,
                references=snap.references,
                operation_scope=snap.operation_scope,
            )
            db.execute(
                "INSERT INTO revision VALUES(?,?,?)",
                (review.task_id, newrev, saved["input_digest"]),
            )
            db.execute(
                "INSERT INTO prepared_execution VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    review.task_id,
                    newrev,
                    "mock",
                    prompt,
                    newprompt.prompt_digest,
                    snapshot.execution_digest,
                    snap.model,
                    snap.endpoint,
                    snap.group,
                ),
            )
            db.execute(
                "INSERT INTO prepared_snapshot_v2 VALUES(?,?,?)",
                (review.task_id, newrev, snapshot.to_payload()),
            )
            for table, column in (
                ("prepared_input_v2", "context"),
                ("prepared_scope_v2", "scope"),
                ("prepared_conflicts", "reported"),
            ):
                db.execute(
                    f"INSERT INTO {table}(task_id,revision,{column}) SELECT task_id,?,{column} FROM {table} WHERE task_id=? AND revision=?",
                    (newrev, review.task_id, review.revision),
                )
            db.execute(
                "INSERT INTO prepared_reference SELECT task_id,?,position,ref_id,category,mime,sha256,data FROM prepared_reference WHERE task_id=? AND revision=?",
                (newrev, review.task_id, review.revision),
            )
            db.execute(
                "INSERT INTO prepared_mock_source SELECT task_id,?,form_revision,artifact_id,review_id,artifact_sha256 FROM prepared_mock_source WHERE task_id=? AND revision=?",
                (newrev, review.task_id, review.revision),
            )
            db.execute(
                "UPDATE mock_revision_request SET target_revision=? WHERE request_id=? AND target_revision IS NULL",
                (newrev, request_id),
            )
            db.execute(
                "UPDATE task SET status='awaiting_confirmation' WHERE task_id=?",
                (review.task_id,),
            )
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_eligibility'"
            ).fetchone():
                db.execute(
                    "UPDATE local_eligibility SET invalidated=1 WHERE task_id=? AND consumed=0",
                    (review.task_id,),
                )
        return {
            "task_id": review.task_id,
            "revision": newrev,
            "prompt_digest": newprompt.prompt_digest,
            "execution_digest": snapshot.execution_digest,
            "source_trust": "unverified",
            "authorization": "unverified",
        }
    except MockWorkflowError:
        raise
    except (sqlite3.Error, ValueError, OSError, MemoryError):
        raise MockWorkflowError("Cannot safely prepare Mock revision") from None


def read_mock_revision_chain(store: Store, request_id: str) -> dict[str, Any]:
    try:
        with closing(store._connect()) as db:
            row = db.execute(
                "SELECT review_id,reason,source,created_at,target_revision,trust_state FROM mock_revision_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row:
                raise MockWorkflowError("Revision request not found")
            review, _ = _load_review(db, row[0])
            saved = read_prepared(store, review.task_id, review.revision)
            source = saved["form_source"]
            source_attempt = db.execute(
                "SELECT attempt_id FROM artifact WHERE artifact_id=?",
                (review.artifact_id,),
            ).fetchone()
            if source_attempt is None:
                raise MockWorkflowError("Mock source attempt is missing")
            attempts = (
                db.execute(
                    "SELECT attempt_id,upstream_status,delivery_status FROM attempt WHERE task_id=? AND revision=? ORDER BY rowid",
                    (review.task_id, row[4]),
                ).fetchall()
                if row[4]
                else []
            )
        return {
            "request_id": request_id,
            "source_review": review.review_id,
            "task_id": review.task_id,
            "source_revision": review.revision,
            "source_artifact": review.artifact_id,
            "source_attempt": source_attempt[0],
            "source_digest": review.digest,
            "form_source": source,
            "packaging_versions": [
                {"ref_id": r["ref_id"], "version": r.get("version")}
                for r in saved["references"]
                if r["category"] == "artwork"
            ],
            "reason": row[1],
            "source": row[2],
            "created_at": row[3],
            "target_revision": row[4],
            "trust_state": row[5],
            "attempts": [
                {"attempt_id": a, "upstream": u, "delivery": d} for a, u, d in attempts
            ],
        }
    except (sqlite3.Error, ValueError):
        raise MockWorkflowError("Cannot read Mock revision chain") from None


def read_mock_candidate(
    store: Store, task_id: str, revision: int, attempt_id: str
) -> dict[str, Any]:
    """Return local linkage and stored observations, without verifying file delivery."""
    try:
        saved = read_prepared(store, task_id, revision)
        if (
            saved["stage"] != "mock"
            or saved["digest_version"] != 2
            or not saved["form_source"]
        ):
            raise MockWorkflowError("Mock snapshot missing")
        with closing(store._connect()) as db:
            row = db.execute(
                "SELECT upstream_status,delivery_status FROM attempt WHERE attempt_id=? AND task_id=? AND revision=?",
                (attempt_id, task_id, revision),
            ).fetchone()
            if not row:
                raise MockWorkflowError("Mock attempt association mismatch")
            artifacts = db.execute(
                "SELECT artifact_id,sha256 FROM artifact WHERE attempt_id=? ORDER BY rowid",
                (attempt_id,),
            ).fetchall()
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mock_review'"
            ).fetchone()
            latest = (
                db.execute(
                    "SELECT review_id FROM mock_review WHERE task_id=? AND revision=? AND artifact_id=? ORDER BY rowid DESC LIMIT 1",
                    (task_id, revision, artifacts[0][0]),
                ).fetchone()
                if table and len(artifacts) == 1
                else None
            )
        review = read_mock_review_state(store, latest[0]) if latest else None
        return {
            "task_id": task_id,
            "revision": revision,
            "attempt_id": attempt_id,
            "upstream": row[0],
            "delivery": row[1],
            "file_state": "unverified",
            "artifacts": [{"artifact_id": a, "digest": d} for a, d in artifacts],
            "form_source": saved["form_source"],
            "packaging_versions": [
                {"ref_id": r["ref_id"], "version": r.get("version")}
                for r in saved["references"]
                if r["category"] == "artwork"
            ],
            "review_status": review["status"]
            if review
            else "pending"
            if len(artifacts) == 1 and row[1] == "delivered"
            else "unavailable",
            "review_source": "unverified",
        }
    except MockWorkflowError:
        raise
    except (sqlite3.Error, ValueError):
        raise MockWorkflowError("Cannot read Mock candidate") from None
