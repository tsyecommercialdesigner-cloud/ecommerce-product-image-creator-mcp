"""Offline form version bookkeeping; does not generate, authorize, or approve."""

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from ecommerce_product_image_creator.storage import (
    FileSafetyError,
    ResourceBudget,
    Store,
    read_reference,
)


class FormWorkflowError(ValueError):
    """Sanitized local workflow error."""


def register_form_artifact(
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
    """Register an existing safely read file, never generate or write image data."""
    if not all((task_id, attempt_id, artifact_id, digest)):
        raise FormWorkflowError("Artifact association is incomplete")
    try:
        image = read_reference(root, path, mime, budget)
        if image.sha256 != digest:
            raise FormWorkflowError("Artifact digest mismatch")
        with closing(store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT a.task_id,a.revision,a.upstream_status,a.delivery_status,p.stage FROM attempt a JOIN prepared_execution p ON p.task_id=a.task_id AND p.revision=a.revision WHERE a.attempt_id=?""",
                (attempt_id,),
            ).fetchone()
            if row != (
                task_id,
                revision,
                "succeeded",
                "not_available",
                "form",
            ) and row != (task_id, revision, "succeeded", "pending", "form"):
                raise FormWorkflowError(
                    "Attempt is not eligible for form artifact registration"
                )
            if db.execute(
                "SELECT 1 FROM artifact WHERE attempt_id=? OR artifact_id=?",
                (attempt_id, artifact_id),
            ).fetchone():
                raise FormWorkflowError("Artifact already registered or conflicts")
            db.execute(
                "INSERT INTO artifact VALUES(?,?,?,?)",
                (artifact_id, attempt_id, str(path), digest),
            )
            db.execute(
                "UPDATE attempt SET delivery_status='delivered' WHERE attempt_id=?",
                (attempt_id,),
            )
    except (sqlite3.Error, OSError, FileSafetyError):
        raise FormWorkflowError("Unable to register form artifact safely") from None


def link_form_revision(
    store: Store,
    task_id: str,
    revision: int,
    previous_revision: int,
    previous_attempt_id: str,
    reason: str,
) -> None:
    """Record a revision relationship and supplied reason without proving its source."""
    if (
        not task_id
        or not previous_attempt_id
        or not reason.strip()
        or previous_revision >= revision
    ):
        raise FormWorkflowError("Invalid revision relationship")
    try:
        with closing(store._connect()) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS form_revision_origin (task_id TEXT NOT NULL, revision INTEGER NOT NULL, previous_revision INTEGER NOT NULL, previous_attempt_id TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(task_id,revision), FOREIGN KEY(task_id,revision) REFERENCES revision(task_id,revision), FOREIGN KEY(previous_attempt_id) REFERENCES attempt(attempt_id))"""
            )
            db.execute("BEGIN IMMEDIATE")
            stages = db.execute(
                "SELECT revision FROM prepared_execution WHERE task_id=? AND stage='form' AND revision IN (?,?)",
                (task_id, previous_revision, revision),
            ).fetchall()
            prior = db.execute(
                "SELECT task_id,revision FROM attempt WHERE attempt_id=?",
                (previous_attempt_id,),
            ).fetchone()
            if {row[0] for row in stages} != {previous_revision, revision} or prior != (
                task_id,
                previous_revision,
            ):
                raise FormWorkflowError("Revision origin does not match task")
            db.execute(
                "INSERT INTO form_revision_origin VALUES(?,?,?,?,?)",
                (task_id, revision, previous_revision, previous_attempt_id, reason),
            )
    except sqlite3.IntegrityError:
        raise FormWorkflowError("Revision origin already exists or conflicts") from None
    except sqlite3.Error:
        raise FormWorkflowError("Unable to record revision origin") from None


def read_form_state(
    store: Store,
    task_id: str,
    *,
    revision: int | None = None,
    artifact_root: Path | None = None,
    budget: ResourceBudget | None = None,
) -> dict[str, Any]:
    """Return local stored observations; review source remains unverified."""
    if (artifact_root is None) != (budget is None):
        raise FormWorkflowError("Both artifact root and resource budget are required")
    try:
        with closing(store._connect()) as db:
            latest = db.execute(
                "SELECT MAX(revision) FROM prepared_execution WHERE task_id=? AND stage='form'",
                (task_id,),
            ).fetchone()
            if not latest or latest[0] is None:
                raise FormWorkflowError("Form revision not found")
            revision = latest[0] if revision is None else revision
            if not db.execute(
                "SELECT 1 FROM prepared_execution WHERE task_id=? AND revision=? AND stage='form'",
                (task_id, revision),
            ).fetchone():
                raise FormWorkflowError("Form revision not found")
            attempts = db.execute(
                "SELECT attempt_id,upstream_status,delivery_status,upstream_id FROM attempt WHERE task_id=? AND revision=? ORDER BY rowid",
                (task_id, revision),
            ).fetchall()
            has_review = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='form_review_detail'"
            ).fetchone()
            result = []
            for attempt_id, upstream, delivery, upstream_id in attempts:
                artifacts = db.execute(
                    "SELECT artifact_id,sha256,path FROM artifact WHERE attempt_id=? ORDER BY rowid",
                    (attempt_id,),
                ).fetchall()
                state = (
                    "pending"
                    if len(artifacts) == 1 and delivery == "delivered"
                    else "unavailable"
                )
                if len(artifacts) > 1:
                    state = "conflict"
                if has_review and len(artifacts) == 1:
                    records = db.execute(
                        """SELECT d.decision,d.invalidation_reason,d.expires_at,EXISTS(SELECT 1 FROM form_review_invalidation i WHERE i.review_id=r.review_id) FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id WHERE r.task_id=? AND r.form_revision=? AND r.artifact_id=? AND r.artifact_sha256=? ORDER BY r.rowid""",
                        (task_id, revision, artifacts[0][0], artifacts[0][1]),
                    ).fetchall()
                    active = {
                        r[0]
                        for r in records
                        if r[1] is None
                        and not r[3]
                        and (r[2] is None or r[2] > time.time())
                        and r[0] in {"accept", "reject"}
                    }
                    if len(active) > 1:
                        state = "conflict"
                    elif records:
                        last = records[-1]
                        state = (
                            "invalidated"
                            if last[1] is not None or last[3]
                            else "expired"
                            if last[2] is not None and last[2] <= time.time()
                            else last[0]
                        )
                file_state = "unverified"
                if (
                    artifact_root is not None
                    and budget is not None
                    and len(artifacts) == 1
                ):
                    path = Path(artifacts[0][2])
                    try:
                        try:
                            image = read_reference(
                                artifact_root, path, "image/png", budget
                            )
                        except FileSafetyError:
                            image = read_reference(
                                artifact_root, path, "image/jpeg", budget
                            )
                        file_state = (
                            "readable"
                            if image.sha256 == artifacts[0][1]
                            else "missing_or_corrupt"
                        )
                    except (FileSafetyError, OSError):
                        file_state = "missing_or_corrupt"
                result.append(
                    {
                        "attempt_id": attempt_id,
                        "upstream": upstream,
                        "delivery": delivery,
                        "upstream_id": upstream_id,
                        "artifacts": [
                            {"artifact_id": a, "digest": d} for a, d, _ in artifacts
                        ],
                        "file_state": file_state,
                        "review_applicability": "invalid_artifact"
                        if file_state == "missing_or_corrupt"
                        else "unverified",
                        "review": state,
                        "review_source": "unverified",
                    }
                )
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='form_revision_origin'"
            ).fetchone()
            origin = (
                db.execute(
                    "SELECT previous_revision,previous_attempt_id,reason FROM form_revision_origin WHERE task_id=? AND revision=?",
                    (task_id, revision),
                ).fetchone()
                if table
                else None
            )
            return {
                "task_id": task_id,
                "revision": revision,
                "attempts": result,
                "revision_origin": {
                    "revision": origin[0],
                    "attempt_id": origin[1],
                    "reason": origin[2],
                }
                if origin
                else None,
            }
    except sqlite3.Error:
        raise FormWorkflowError("Unable to read form state") from None
