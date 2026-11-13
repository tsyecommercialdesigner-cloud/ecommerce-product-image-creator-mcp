"""Local Team review and scoped Mock input-change declarations."""

import json
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from .artifact_reader import read_artifact
from .config import team_mode_enabled
from .domain import Reference, ReferenceCategory
from .input_revision import record_input_revision
from .preparation import _input_context, _observations, _preserved_inputs
from .review import FormReview, get_review, save_review
from .storage import ResourceBudget, Store, read_reference

_REVIEW_EVIDENCE = "concept_prototype;internal_mock_allowed;unverified=not_specified"


def review_form(
    store: Store,
    state: Path,
    budget: ResourceBudget,
    task_id: str,
    revision: int,
    attempt_id: str,
    artifact_id: str,
    decision: str,
) -> dict[str, Any]:
    if not team_mode_enabled() or decision not in {"accept", "reject"}:
        raise ValueError("Team review unavailable or invalid decision")
    artifact = read_artifact(
        store, task_id, "form", revision, attempt_id, artifact_id, state, budget
    )
    if (
        artifact["upstream_status"] != "succeeded"
        or artifact["delivery_status"] != "delivered"
        or artifact["file"]["state"] != "verified"
    ):
        raise ValueError("Form artifact is unavailable")
    with closing(store._connect()) as db:
        latest = db.execute(
            "SELECT MAX(revision) FROM prepared_execution WHERE task_id=? AND stage='form'",
            (task_id,),
        ).fetchone()
    if not latest or latest[0] != revision:
        raise ValueError("Form revision is stale")
    review_id = "team-review-" + uuid.uuid4().hex
    save_review(
        store,
        FormReview(
            review_id,
            task_id,
            revision,
            artifact_id,
            artifact["digest"],
            decision,
            "team-responsible-user",
            time.time(),
            _REVIEW_EVIDENCE,
        ),
    )
    return {
        "task_id": task_id,
        "revision": revision,
        "artifact_id": artifact_id,
        "review_id": review_id,
        "decision": decision,
        "usage_scope": "concept_prototype",
        "unverified_structure": "not_specified",
        "trust_state": "unverified",
        "mock_permitted": decision == "accept",
    }


def confirm_mock_inputs(
    store: Store,
    root: Path,
    state: Path,
    budget: ResourceBudget,
    task_id: str,
    artifact_id: str,
    review_id: str,
    artwork: list[dict[str, Any]],
) -> dict[str, Any]:
    if not team_mode_enabled() or not 1 <= len(artwork) <= 2:
        raise ValueError("Team input confirmation unavailable")
    review = get_review(store, review_id)
    if (
        review.task_id != task_id
        or review.artifact_id != artifact_id
        or review.decision != "accept"
        or review.source != "team-responsible-user"
        or review.evidence_reference != _REVIEW_EVIDENCE
        or review.invalidated
        or (review.expires_at is not None and review.expires_at <= time.time())
    ):
        raise ValueError("Form review unavailable")
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT a.attempt_id,t.instructions,(SELECT MAX(revision) FROM revision WHERE task_id=t.task_id) "
            "FROM artifact a JOIN attempt x ON x.attempt_id=a.attempt_id "
            "JOIN task t ON t.task_id=x.task_id WHERE a.artifact_id=? AND t.task_id=?",
            (artifact_id, task_id),
        ).fetchone()
    if not row:
        raise ValueError("Form artifact unavailable")
    artifact = read_artifact(
        store, task_id, "form", review.form_revision, row[0], artifact_id, state, budget
    )
    if (
        artifact["delivery_status"] != "delivered"
        or artifact["upstream_status"] != "succeeded"
        or artifact["file"]["state"] != "verified"
        or artifact["digest"] != review.artifact_digest
    ):
        raise ValueError("Form artifact unavailable")
    image = read_reference(
        state, Path(artifact["file"]["path"]), artifact["file"]["mime"], budget
    )
    if image.sha256 != review.artifact_digest:
        raise ValueError("Form artifact digest mismatch")
    refs = [
        Reference(
            artifact_id,
            ReferenceCategory.FORM,
            Path(artifact["file"]["path"]),
            image.mime,
            len(image.data),
            image.sha256,
            task_id=task_id,
        )
    ]
    for item in artwork:
        if item["category"] != "artwork":
            raise ValueError("Invalid artwork category")
        image = read_reference(root, Path(item["path"]), item["mime"], budget)
        refs.append(
            Reference(
                item["ref_id"],
                ReferenceCategory.ARTWORK,
                Path(item["path"]),
                image.mime,
                len(image.data),
                image.sha256,
                task_id=task_id,
                view=item.get("view"),
                region=item.get("region"),
                version=item.get("version"),
                importance=item.get("importance"),
                purpose=item.get("purpose"),
                observations=_observations(item.get("observations")),
            )
        )
    if len({ref.ref_id for ref in refs}) != len(refs):
        raise ValueError("Duplicate reference identifier")
    revision = int(row[2])
    with closing(store._connect()) as db:
        unchanged = _preserved_inputs(db, task_id, revision, refs, {"artwork"})
    if unchanged:
        return {"task_id": task_id, "revision_decision_id": None, "changed": False}
    decision_id = "team-input-" + uuid.uuid4().hex
    record_input_revision(
        store,
        decision_id,
        task_id,
        revision,
        json.loads(_input_context(refs, row[1]))["references"],
        "Explicit Team Mock input change",
        "team-local-input-change",
        time.time() + 7 * 86400,
        "team-explicit-preparation",
    )
    return {"task_id": task_id, "revision_decision_id": decision_id, "changed": True}
