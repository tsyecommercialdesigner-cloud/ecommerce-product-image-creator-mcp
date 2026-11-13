"""Test-only construction of scope-bound input-revision decisions."""

import json
import time
import uuid
from pathlib import Path

from ecommerce_product_image_creator import input_revision
from ecommerce_product_image_creator.domain import Reference, ReferenceCategory
from ecommerce_product_image_creator.preparation import _input_context, _observations
from ecommerce_product_image_creator.storage import (
    ResourceBudget,
    Store,
    read_reference,
)


def confirm_fixture_revision(
    store: Store,
    root: Path,
    budget: ResourceBudget,
    task_id: str,
    refs: list[dict],
    monkeypatch,
    *,
    artifact_id: str | None = None,
    artifact_root: Path | None = None,
) -> str:
    built = []
    if artifact_id:
        with store._connect() as db:
            row = db.execute(
                "SELECT path FROM artifact WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        path = Path(row[0])
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        value = read_reference(artifact_root or root, path, mime, budget)
        built.append(
            Reference(
                artifact_id,
                ReferenceCategory.FORM,
                path,
                mime,
                len(value.data),
                value.sha256,
                task_id=task_id,
            )
        )
    for item in refs:
        value = read_reference(root, Path(item["path"]), item["mime"], budget)
        built.append(
            Reference(
                item["ref_id"],
                ReferenceCategory(item["category"]),
                Path(item["path"]),
                item["mime"],
                len(value.data),
                value.sha256,
                view=item.get("view"),
                region=item.get("region"),
                version=item.get("version"),
                importance=item.get("importance"),
                purpose=item.get("purpose"),
                observations=_observations(item.get("observations")),
                task_id=task_id,
            )
        )
    with store._connect() as db:
        revision = db.execute(
            "SELECT MAX(revision) FROM revision WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    decision_id = uuid.uuid4().hex
    target = json.loads(_input_context(built, "fixture"))["references"]
    input_revision.record_input_revision(
        store,
        decision_id,
        task_id,
        revision,
        target,
        "isolated test revision",
        "test fixture",
        time.time() + 120,
    )
    monkeypatch.setattr(input_revision, "_source_is_trusted", lambda *_: True)
    return decision_id
