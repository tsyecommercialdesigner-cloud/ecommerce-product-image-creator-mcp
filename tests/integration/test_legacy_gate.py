from pathlib import Path

import pytest

from ecommerce_product_image_creator.domain import (
    Confirmation,
    ExecutionSnapshot,
    PromptRevision,
    Revision,
    Task,
    TaskStatus,
)
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.tasks import (
    EligibilityError,
    LocalEligibility,
    OperationScope,
)


def test_legacy_snapshot_cannot_create_or_consume_new_eligibility(tmp_path: Path):
    store = Store(tmp_path / "state.sqlite")
    task = Task("task", "input", "request", TaskStatus.READY)
    revision = Revision("task", 1, "input")
    prompt = PromptRevision.from_text("task", 1, 1, "form", "prompt")
    snapshot = ExecutionSnapshot.create(
        revision, prompt, "form", "model", "endpoint", "group"
    )
    confirmation = Confirmation(
        "task",
        1,
        "input",
        prompt.prompt_digest,
        1,
        snapshot.execution_digest,
        confirmation_id="legacy",
    )
    service = LocalEligibility(store, tmp_path, ResourceBudget(10000, 1000, 10000))
    args = (
        task,
        revision,
        [],
        prompt,
        snapshot,
        confirmation,
        "op",
        OperationScope("generate", None, "single-image", 3),
    )
    with pytest.raises(EligibilityError, match="version"):
        service.prepare(*args, expires_at=9999999999)
    with pytest.raises(EligibilityError, match="version"):
        service.consume(*args)


def test_existing_v1_eligibility_is_retained_but_cannot_be_consumed(tmp_path: Path):
    store = Store(tmp_path / "old.sqlite")
    store.create_task("task", "input", "request")
    store.create_revision("task", 1, "input")
    service = LocalEligibility(store, tmp_path, ResourceBudget(10000, 1000, 10000))
    task = Task("task", "input", "request", TaskStatus.READY)
    revision = Revision("task", 1, "input")
    prompt = PromptRevision.from_text("task", 1, 1, "form", "prompt")
    snapshot = ExecutionSnapshot.create(
        revision, prompt, "form", "model", "endpoint", "group"
    )
    confirmation = Confirmation(
        "task",
        1,
        "input",
        prompt.prompt_digest,
        1,
        snapshot.execution_digest,
        confirmation_id="legacy",
    )
    with store._connect() as db:
        db.execute(
            "INSERT INTO local_eligibility(operation_id,confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op",
                "legacy",
                "task",
                1,
                "input",
                1,
                prompt.prompt_digest,
                snapshot.execution_digest,
                "scope",
                9999999999,
            ),
        )
    with pytest.raises(EligibilityError, match="version"):
        service.consume(
            task,
            revision,
            [],
            prompt,
            snapshot,
            confirmation,
            "op",
            OperationScope("generate", None, "single-image", 3),
        )
    with store._connect() as db:
        assert db.execute(
            "SELECT consumed,invalidated FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (0, 1)


def test_v1_still_constructible_for_audit():
    revision = Revision("task", 1, "input")
    prompt = PromptRevision.from_text("task", 1, 1, "form", "prompt")
    assert (
        ExecutionSnapshot.create(
            revision, prompt, "form", "model", "endpoint", "group"
        ).digest_version
        == 1
    )
