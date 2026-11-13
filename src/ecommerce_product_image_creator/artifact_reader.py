"""Read local artifact observations without asserting delivery or authorization."""

import base64
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from ecommerce_product_image_creator.preparation import read_prepared
from ecommerce_product_image_creator.preview_evidence import read_preview
from ecommerce_product_image_creator.storage import (
    FileSafetyError,
    ResourceBudget,
    Store,
    read_reference,
)


class ArtifactReadError(ValueError):
    """Sanitized artifact identity or storage error."""


def _table(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _file_state(
    root: Path,
    path: Path,
    digest: str,
    budget: ResourceBudget,
    expected_mime: str | None,
    include_content: bool = False,
) -> dict[str, str | None]:
    if (
        not root.is_absolute()
        or not path.is_absolute()
        or not path.is_relative_to(root)
        or ".." in path.parts
    ):
        return {"state": "unsafe_path", "mime": None}
    try:
        if not path.resolve(strict=False).is_relative_to(root.resolve(strict=True)):
            return {"state": "unsafe_path", "mime": None}
        path.lstat()
    except FileNotFoundError:
        return {"state": "missing", "mime": None}
    except OSError:
        return {"state": "invalid_or_unreadable", "mime": None}
    for mime in [expected_mime] if expected_mime else ["image/png", "image/jpeg"]:
        try:
            image = read_reference(root, path, mime, budget)
            state = "verified" if image.sha256 == digest else "digest_mismatch"
            return {
                "state": state,
                "mime": image.mime,
                "reference": (
                    f"data:{image.mime};base64,{base64.b64encode(image.data).decode('ascii')}"
                    if include_content and state == "verified"
                    else None
                ),
            }
        except (FileSafetyError, OSError):
            continue
    return {"state": "invalid_or_unreadable", "mime": None}


def _review(
    db: sqlite3.Connection,
    stage: str,
    task_id: str,
    revision: int,
    artifact_id: str,
    digest: str,
) -> dict[str, Any]:
    rows: list[Any] = []
    if stage == "form" and _table(db, "form_review_detail"):
        rows = db.execute(
            """SELECT r.review_id,d.decision,r.source,d.created_at,d.expires_at,
            COALESCE(d.invalidation_reason, CASE WHEN EXISTS(SELECT 1 FROM form_review_invalidation i WHERE i.review_id=r.review_id) THEN 'invalidated' END), NULL, NULL
            FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id
            WHERE r.task_id=? AND r.form_revision=? AND r.artifact_id=? AND r.artifact_sha256=? ORDER BY r.rowid""",
            (task_id, revision, artifact_id, digest),
        ).fetchall()
    elif stage == "mock" and _table(db, "mock_review"):
        rows = db.execute(
            """SELECT review_id,decision,source,created_at,expires_at,invalidation_reason,view,feedback FROM mock_review
            WHERE task_id=? AND revision=? AND artifact_id=? AND digest=? ORDER BY rowid""",
            (task_id, revision, artifact_id, digest),
        ).fetchall()
    if not rows:
        return {
            "state": "pending",
            "decision": "pending",
            "source": None,
            "trust_state": "unverified",
            "feedback": [],
        }
    now = time.time()
    active = {
        r[1]
        for r in rows
        if r[1] in ("accept", "reject")
        and r[5] is None
        and (r[4] is None or r[4] > now)
    }
    latest = rows[-1]
    state = (
        "conflict"
        if len(active) > 1
        else "invalidated"
        if latest[5] is not None
        else "expired"
        if latest[4] is not None and latest[4] <= now
        else latest[1]
    )
    result = {
        "state": state,
        "decision": latest[1],
        "review_id": latest[0],
        "source": latest[2],
        "trust_state": "unverified",
        "feedback": [
            {"review_id": r[0], "view": r[6], "text": r[7]}
            for r in rows
            if r[6] is not None or r[7] is not None
        ],
    }
    if stage == "form" and latest[2] == "team-responsible-user":
        detail = db.execute(
            "SELECT evidence_reference FROM form_review_detail WHERE review_id=?",
            (latest[0],),
        ).fetchone()
        if (
            detail
            and detail[0]
            == "concept_prototype;internal_mock_allowed;unverified=back,sides,zipper"
        ):
            result["usage_scope"] = "concept_prototype"
            from .preparation import _review_is_trusted

            result["mock_permitted"] = state == "accept" and _review_is_trusted(
                latest[2], task_id, latest[0], digest, detail[0]
            )
            result["unverified_structure"] = ["back", "sides", "zipper"]
    return result


def read_artifact(
    store: Store,
    task_id: str,
    stage: Literal["form", "mock"],
    revision: int,
    attempt_id: str,
    artifact_id: str,
    artifact_root: Path,
    budget: ResourceBudget,
    *,
    expected_mime: str | None = None,
    include_content: bool = False,
) -> dict[str, Any]:
    """Inspect one registered artifact. The trusted caller supplies the root, never a file path."""
    if (
        stage not in ("form", "mock")
        or not all((task_id, attempt_id, artifact_id))
        or revision < 1
        or expected_mime not in (None, "image/png", "image/jpeg")
    ):
        raise ArtifactReadError("Invalid artifact query")
    try:
        saved = read_prepared(store, task_id, revision)
        if saved["stage"] != stage:
            raise ArtifactReadError("Artifact association mismatch")
        with closing(store._connect()) as db:
            db.execute("BEGIN")
            row = db.execute(
                """SELECT x.path,x.sha256,a.upstream_status,a.delivery_status
                FROM artifact x JOIN attempt a ON a.attempt_id=x.attempt_id
                JOIN revision r ON r.task_id=a.task_id AND r.revision=a.revision
                JOIN prepared_execution p ON p.task_id=r.task_id AND p.revision=r.revision
                WHERE x.artifact_id=? AND a.attempt_id=? AND a.task_id=? AND a.revision=? AND p.stage=?""",
                (artifact_id, attempt_id, task_id, revision, stage),
            ).fetchone()
            if row is None:
                raise ArtifactReadError("Artifact association mismatch")
            form_source = saved["form_source"] if stage == "mock" else None
            if stage == "mock":
                if not form_source:
                    raise ArtifactReadError("Mock source association missing")
                src = db.execute(
                    """SELECT a.task_id,a.revision,x.sha256,p.stage FROM artifact x
                    JOIN attempt a ON a.attempt_id=x.attempt_id JOIN prepared_execution p ON p.task_id=a.task_id AND p.revision=a.revision WHERE x.artifact_id=?""",
                    (form_source["artifact_id"],),
                ).fetchone()
                if src != (
                    task_id,
                    form_source["form_revision"],
                    form_source["sha256"],
                    "form",
                ):
                    raise ArtifactReadError("Mock source association mismatch")
            review = _review(db, stage, task_id, revision, artifact_id, row[1])
            preview = read_preview(db, attempt_id)
            if preview.get("status") == "preview" and (
                preview.get("task_id"),
                preview.get("revision"),
                preview.get("execution_digest"),
            ) != (task_id, revision, saved["execution_digest"]):
                raise ArtifactReadError("Preview association mismatch")
            origin = None
            if stage == "form" and _table(db, "form_revision_origin"):
                origin = db.execute(
                    "SELECT previous_revision,previous_attempt_id FROM form_revision_origin WHERE task_id=? AND revision=?",
                    (task_id, revision),
                ).fetchone()
            if stage == "mock" and _table(db, "mock_revision_request"):
                origin = db.execute(
                    """SELECT r.revision,a.attempt_id FROM mock_revision_request q JOIN mock_review r ON r.review_id=q.review_id JOIN artifact a ON a.artifact_id=r.artifact_id WHERE r.task_id=? AND q.target_revision=?""",
                    (task_id, revision),
                ).fetchone()
            file_state = _file_state(
                artifact_root,
                Path(row[0]),
                row[1],
                budget,
                expected_mime,
                include_content,
            )
            reference = file_state.pop("reference", None)
            if row[2] != "succeeded" or row[3] != "delivered":
                reference = None
            elif file_state["state"] == "verified":
                file_state["path"] = row[0]
        return {
            "task_id": task_id,
            "stage": stage,
            "revision": revision,
            "attempt_id": attempt_id,
            "artifact_id": artifact_id,
            "digest": row[1],
            "upstream_status": row[2],
            "delivery_status": row[3],
            "file": file_state,
            "artifact_reference": reference,
            "mime": {"detected": file_state["mime"], "historical": "unknown"},
            "review": review,
            "preview": preview,
            "review_applicability": "unverified"
            if file_state["state"] == "verified"
            else "invalid_artifact",
            "form_source": form_source,
            "packaging_versions": [
                {"ref_id": r["ref_id"], "version": r.get("version")}
                for r in (saved["references"] or [])
                if r["category"] == "artwork"
            ]
            if stage == "mock"
            else [],
            "revision_origin": {"revision": origin[0], "attempt_id": origin[1]}
            if origin
            else None,
        }
    except ArtifactReadError:
        raise
    except (sqlite3.Error, ValueError, OSError, MemoryError, KeyError, TypeError):
        raise ArtifactReadError("Unable to safely inspect artifact") from None
