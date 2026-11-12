"""Offline form revision records; production revision-source verification denies."""

import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from ecommerce_product_image_creator.domain import (
    ExecutionSnapshot,
    PromptRevision,
    Revision,
)
from ecommerce_product_image_creator.preparation import (
    _reject_review_conflict,
    read_prepared,
)
from ecommerce_product_image_creator.storage import (
    FileSafetyError,
    ResourceBudget,
    Store,
    read_reference,
)
from ecommerce_product_image_creator.tasks import FORM_OUTPUT_CONTRACT


class FormRevisionError(ValueError):
    """Sanitized failure without exposing user content."""


def _revision_source_verified(
    source: str,
    request_id: str,
    task_id: str,
    revision: int,
    attempt_id: str,
    artifact_id: str,
    digest: str,
    review_id: str,
    reason: str,
) -> bool:
    """No real Host revision evidence verifier is configured."""
    return False


def _schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS form_revision_request (
        request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
        attempt_id TEXT NOT NULL, artifact_id TEXT NOT NULL, artifact_digest TEXT NOT NULL,
        review_id TEXT NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL,
        created_at REAL NOT NULL, target_revision INTEGER, trust_state TEXT NOT NULL DEFAULT 'unverified' CHECK(trust_state='unverified'),
        FOREIGN KEY(task_id,source_revision) REFERENCES revision(task_id,revision),
        FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
        FOREIGN KEY(artifact_id) REFERENCES artifact(artifact_id),
        FOREIGN KEY(review_id) REFERENCES form_review(review_id),
        FOREIGN KEY(task_id,target_revision) REFERENCES revision(task_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS form_revision_origin (
        task_id TEXT NOT NULL, revision INTEGER NOT NULL, previous_revision INTEGER NOT NULL,
        previous_attempt_id TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(task_id,revision),
        FOREIGN KEY(task_id,revision) REFERENCES revision(task_id,revision),
        FOREIGN KEY(previous_attempt_id) REFERENCES attempt(attempt_id))""")


def record_revision_request(
    store: Store,
    request_id: str,
    task_id: str,
    source_revision: int,
    attempt_id: str,
    artifact_id: str,
    artifact_digest: str,
    review_id: str,
    reason: str,
    source: str,
    created_at: float,
) -> None:
    """Persist an unverified request, without treating any caller text as approval."""
    if (
        not all(
            (
                request_id,
                task_id,
                attempt_id,
                artifact_id,
                artifact_digest,
                review_id,
                reason.strip(),
                source.strip(),
            )
        )
        or source_revision < 1
        or not math.isfinite(created_at)
        or created_at <= 0
    ):
        raise FormRevisionError("Invalid revision request")
    try:
        with closing(store._connect()) as db, db:
            _schema(db)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT a.task_id,a.revision,a.upstream_status,a.delivery_status,x.sha256,p.stage,r.task_id,r.form_revision,r.artifact_id,r.artifact_sha256,d.decision
                FROM artifact x JOIN attempt a ON x.attempt_id=a.attempt_id
                JOIN prepared_execution p ON p.task_id=a.task_id AND p.revision=a.revision
                JOIN form_review r ON r.review_id=? JOIN form_review_detail d ON d.review_id=r.review_id
                WHERE x.artifact_id=? AND a.attempt_id=?""",
                (review_id, artifact_id, attempt_id),
            ).fetchone()
            if row != (
                task_id,
                source_revision,
                "succeeded",
                "delivered",
                artifact_digest,
                "form",
                task_id,
                source_revision,
                artifact_id,
                artifact_digest,
                "reject",
            ):
                raise FormRevisionError(
                    "Revision source does not match rejected artifact"
                )
            db.execute(
                "INSERT INTO form_revision_request(request_id,task_id,source_revision,attempt_id,artifact_id,artifact_digest,review_id,reason,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    task_id,
                    source_revision,
                    attempt_id,
                    artifact_id,
                    artifact_digest,
                    review_id,
                    reason,
                    source,
                    created_at,
                ),
            )
    except sqlite3.IntegrityError:
        raise FormRevisionError(
            "Revision request conflicts or already exists"
        ) from None
    except sqlite3.Error:
        raise FormRevisionError("Cannot record revision request") from None


def prepare_requested_revision(
    store: Store,
    request_id: str,
    prompt: str,
    artifact_root: Path,
    budget: ResourceBudget,
) -> dict[str, Any]:
    """Consume one checked request and atomically create a v2 snapshot with frozen inputs."""
    from ecommerce_product_image_creator import preparation

    if not request_id or not isinstance(prompt, str) or not prompt.strip():
        raise FormRevisionError("Invalid revision prompt")
    try:
        with closing(store._connect()) as db, db:
            preparation._tables(db)
            _schema(db)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT task_id,source_revision,attempt_id,artifact_id,artifact_digest,review_id,reason,source,target_revision FROM form_revision_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row or row[8] is not None:
                raise FormRevisionError("Revision request missing or already consumed")
            (
                task_id,
                oldrev,
                oldattempt,
                artifact_id,
                digest,
                review_id,
                reason,
                source,
                _,
            ) = row
            if not _revision_source_verified(
                source,
                request_id,
                task_id,
                oldrev,
                oldattempt,
                artifact_id,
                digest,
                review_id,
                reason,
            ):
                raise FormRevisionError("Revision request source is unverified")
            latest = db.execute(
                "SELECT MAX(revision) FROM revision WHERE task_id=?", (task_id,)
            ).fetchone()
            current = db.execute(
                "SELECT status,input_digest,instructions FROM task WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if (
                not latest
                or latest[0] != oldrev
                or not current
                or current[0] not in {"draft", "awaiting_confirmation", "ready"}
            ):
                raise FormRevisionError("Revision request is stale")
            saved = read_prepared(store, task_id, oldrev)
            snap = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
            if (
                saved["digest_version"] != 2
                or snap.digest_version != 2
                or snap.stage != "form"
                or saved["input_digest"] != current[1]
                or saved["instructions"] != current[2]
            ):
                raise FormRevisionError("Original form snapshot is invalid")
            review = db.execute(
                """SELECT r.source,d.decision,d.invalidation_reason,d.expires_at,d.evidence_reference,x.path,x.sha256,a.task_id,a.revision,a.upstream_status,a.delivery_status,
                EXISTS(SELECT 1 FROM form_review_invalidation i WHERE i.review_id=r.review_id)
                FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id JOIN artifact x ON x.artifact_id=r.artifact_id JOIN attempt a ON a.attempt_id=x.attempt_id
                WHERE r.review_id=? AND r.task_id=? AND r.form_revision=? AND r.artifact_id=? AND r.artifact_sha256=? AND a.attempt_id=?""",
                (review_id, task_id, oldrev, artifact_id, digest, oldattempt),
            ).fetchone()
            _reject_review_conflict(db, task_id, oldrev, artifact_id, digest)
            newest = db.execute(
                "SELECT r.review_id FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id WHERE r.task_id=? AND r.form_revision=? ORDER BY r.rowid DESC LIMIT 1",
                (task_id, oldrev),
            ).fetchone()
            if (
                not review
                or newest != (review_id,)
                or review[1] != "reject"
                or review[2] is not None
                or (review[3] is not None and review[3] <= time.time())
                or review[6:] != (digest, task_id, oldrev, "succeeded", "delivered", 0)
                or not preparation._review_is_trusted(
                    review[0], task_id, review_id, digest, review[4]
                )
            ):
                raise FormRevisionError(
                    "Rejected artifact review is not current and verified"
                )
            try:
                image = read_reference(
                    artifact_root, Path(review[5]), "image/png", budget
                )
            except FileSafetyError:
                image = read_reference(
                    artifact_root, Path(review[5]), "image/jpeg", budget
                )
            if image.sha256 != digest:
                raise FormRevisionError("Source artifact digest changed")
            newrev = oldrev + 1
            promptrev = PromptRevision.from_text(
                task_id, newrev, newrev, "form", prompt
            )
            new = ExecutionSnapshot.create(
                Revision(task_id, newrev, saved["input_digest"]),
                promptrev,
                "form",
                snap.model,
                snap.endpoint,
                snap.group,
                references=snap.references,
                operation_scope=snap.operation_scope,
            )
            db.execute(
                "INSERT INTO revision VALUES(?,?,?)",
                (task_id, newrev, saved["input_digest"]),
            )
            db.execute(
                "INSERT INTO prepared_execution VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    newrev,
                    "form",
                    prompt,
                    promptrev.prompt_digest,
                    new.execution_digest,
                    snap.model,
                    snap.endpoint,
                    snap.group,
                ),
            )
            db.execute(
                "INSERT INTO prepared_snapshot_v2 VALUES(?,?,?)",
                (task_id, newrev, new.to_payload()),
            )
            for table, column in (
                ("prepared_input_v2", "context"),
                ("prepared_scope_v2", "scope"),
                ("prepared_conflicts", "reported"),
            ):
                db.execute(
                    f"INSERT INTO {table}(task_id,revision,{column}) SELECT task_id,?,{column} FROM {table} WHERE task_id=? AND revision=?",
                    (newrev, task_id, oldrev),
                )
            db.execute(
                "INSERT INTO prepared_reference SELECT task_id,?,position,ref_id,category,mime,sha256,data FROM prepared_reference WHERE task_id=? AND revision=?",
                (newrev, task_id, oldrev),
            )
            db.execute(
                "INSERT INTO form_revision_origin VALUES(?,?,?,?,?)",
                (task_id, newrev, oldrev, oldattempt, reason),
            )
            db.execute(
                "UPDATE form_revision_request SET target_revision=? WHERE request_id=? AND target_revision IS NULL",
                (newrev, request_id),
            )
            db.execute(
                "UPDATE task SET status='awaiting_confirmation' WHERE task_id=?",
                (task_id,),
            )
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_eligibility'"
            ).fetchone():
                db.execute(
                    "UPDATE local_eligibility SET invalidated=1 WHERE task_id=? AND consumed=0",
                    (task_id,),
                )
        return {
            "task_id": task_id,
            "revision": newrev,
            "prompt_revision": newrev,
            "prompt_digest": promptrev.prompt_digest,
            "execution_digest": new.execution_digest,
            "source_trust": "unverified",
            "authorization": "unverified",
            "form_output_contract": list(FORM_OUTPUT_CONTRACT),
        }
    except FormRevisionError:
        raise
    except (sqlite3.Error, ValueError, OSError, MemoryError):
        raise FormRevisionError("Cannot safely create requested revision") from None


def read_revision_chain(store: Store, request_id: str) -> dict[str, Any]:
    try:
        with closing(store._connect()) as db:
            row = db.execute(
                "SELECT task_id,source_revision,attempt_id,artifact_id,artifact_digest,review_id,reason,source,created_at,target_revision,trust_state FROM form_revision_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row:
                raise FormRevisionError("Revision request not found")
            attempts = (
                db.execute(
                    "SELECT attempt_id,upstream_status,delivery_status FROM attempt WHERE task_id=? AND revision=? ORDER BY rowid",
                    (row[0], row[9]),
                ).fetchall()
                if row[9]
                else []
            )
        keys = (
            "task_id",
            "source_revision",
            "source_attempt",
            "artifact_id",
            "artifact_digest",
            "review_id",
            "reason",
            "source",
            "created_at",
            "target_revision",
            "trust_state",
        )
        return {
            **dict(zip(keys, row, strict=True)),
            "attempts": [
                {"attempt_id": a, "upstream": u, "delivery": d} for a, u, d in attempts
            ],
        }
    except sqlite3.Error:
        raise FormRevisionError("Cannot read revision request") from None
