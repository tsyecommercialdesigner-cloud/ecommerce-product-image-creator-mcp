import hashlib
import time
from pathlib import Path

from PIL import Image

from ecommerce_product_image_creator.domain import (
    Confirmation,
    ExecutionSnapshot,
    PromptRevision,
    Reference,
    ReferenceCategory,
    Revision,
    Task,
    TaskStatus,
)
from ecommerce_product_image_creator.preparation import prepare_form, read_prepared
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.tasks import LocalEligibility, OperationScope


def test_preparation_and_domain_share_versioned_digest(tmp_path: Path) -> None:
    refs = []
    for category in ("artwork", "form"):
        file = tmp_path / (category + ".png")
        Image.new("RGB", (3, 3)).save(file)
        refs.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(file),
                "mime": "image/png",
            }
        )
    store = Store(tmp_path / "task.sqlite")
    result = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 1000, 100000),
        refs,
        "request",
        "prompt",
        "model",
        "endpoint",
        "group",
    )
    restored = read_prepared(store, result["task_id"], result["revision"])
    snapshot = ExecutionSnapshot.from_payload(restored["snapshot_payload"])
    assert snapshot.execution_digest == result["execution_digest"]
    assert restored["digest_version"] == 2
    assert snapshot.execution_digest == restored["execution_digest"]
    assert restored["operation_scope"]["output"] == "six-view-composite"
    context_refs = []
    for item in refs:
        path = Path(item["path"])
        data = path.read_bytes()
        context_refs.append(
            Reference(
                item["ref_id"],
                ReferenceCategory(item["category"]),
                path,
                item["mime"],
                len(data),
                hashlib.sha256(data).hexdigest(),
                task_id=result["task_id"],
            )
        )
    store.transition_task(result["task_id"], TaskStatus.AWAITING_CONFIRMATION)
    store.transition_task(result["task_id"], TaskStatus.READY)
    task = Task(result["task_id"], result["input_digest"], "request", TaskStatus.READY)
    revision = Revision(task.task_id, 1, task.input_digest)
    prompt = PromptRevision.from_text(task.task_id, 1, 1, "form", "prompt")
    confirmation = Confirmation(
        task.task_id,
        1,
        task.input_digest,
        prompt.prompt_digest,
        1,
        snapshot.execution_digest,
        confirmation_id="fixture",
    )
    scope = OperationScope("generate", None, "six-view-composite", 3)
    service = LocalEligibility(store, tmp_path, ResourceBudget(100000, 1000, 100000))
    Path(refs[-1]["path"]).write_bytes(b"modified after preparation")
    service.prepare(
        task,
        revision,
        context_refs,
        prompt,
        snapshot,
        confirmation,
        "op",
        scope,
        expires_at=time.time() + 60,
    )
    assert (
        service.consume(
            task, revision, context_refs, prompt, snapshot, confirmation, "op", scope
        ).business_authorized
        is False
    )


def test_digest_binds_order_scope_and_content() -> None:
    revision = Revision("t", 1, "input")
    prompt = PromptRevision.from_text("t", 1, 1, "form", "prompt")
    base = ExecutionSnapshot.create(
        revision,
        prompt,
        "form",
        "model",
        "endpoint",
        "group",
        references=(("a", "a" * 64), ("b", "b" * 64)),
        operation_scope="scope",
    )
    reordered = ExecutionSnapshot.create(
        revision,
        prompt,
        "form",
        "model",
        "endpoint",
        "group",
        references=(("b", "b" * 64), ("a", "a" * 64)),
        operation_scope="scope",
    )
    changed = ExecutionSnapshot.create(
        revision,
        prompt,
        "form",
        "model",
        "endpoint",
        "group",
        references=(("a", "a" * 64), ("b", "b" * 64)),
        operation_scope="other",
    )
    assert (
        len(
            {
                base.execution_digest,
                reordered.execution_digest,
                changed.execution_digest,
            }
        )
        == 3
    )
    for model, endpoint, group in [
        ("new-model", "endpoint", "group"),
        ("model", "new-endpoint", "group"),
        ("model", "endpoint", "new-group"),
    ]:
        candidate = ExecutionSnapshot.create(
            revision,
            prompt,
            "form",
            model,
            endpoint,
            group,
            references=base.references,
            operation_scope="scope",
        )
        assert candidate.execution_digest != base.execution_digest
    revised_prompt = PromptRevision.from_text("t", 1, 2, "form", "changed")
    candidate = ExecutionSnapshot.create(
        revision,
        revised_prompt,
        "form",
        "model",
        "endpoint",
        "group",
        references=base.references,
        operation_scope="scope",
    )
    assert candidate.execution_digest != base.execution_digest


def test_legacy_form_snapshot_remains_readable_without_migration(
    tmp_path: Path,
) -> None:
    import json

    store = Store(tmp_path / "old.sqlite")
    store.create_task("old", "input", "instructions")
    store.create_revision("old", 1, "input")
    image = b"legacy-image"
    digest = hashlib.sha256(image).hexdigest()
    prompt_digest = hashlib.sha256(b"prompt").hexdigest()
    values = [
        "old",
        1,
        "form",
        "input",
        prompt_digest,
        "model",
        "endpoint",
        "group",
        "six-view-composite",
        [("form", digest)],
    ]
    execution = hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with store._connect() as db:
        db.execute(
            "CREATE TABLE prepared_execution (task_id TEXT, revision INTEGER, stage TEXT, prompt TEXT, prompt_digest TEXT, execution_digest TEXT, model TEXT, endpoint TEXT, grouping TEXT, PRIMARY KEY(task_id,revision))"
        )
        db.execute(
            "CREATE TABLE prepared_reference (task_id TEXT, revision INTEGER, position INTEGER, ref_id TEXT, category TEXT, mime TEXT, sha256 TEXT, data BLOB)"
        )
        db.execute(
            "INSERT INTO prepared_execution VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "old",
                1,
                "form",
                "prompt",
                prompt_digest,
                execution,
                "model",
                "endpoint",
                "group",
            ),
        )
        db.execute(
            "INSERT INTO prepared_reference VALUES(?,?,?,?,?,?,?,?)",
            ("old", 1, 0, "form", "form", "image/png", digest, image),
        )
    result = read_prepared(store, "old", 1)
    assert result["digest_version"] == 0 and result["reference_bytes"] == [image]
    inputs = []
    for category in ("artwork", "form"):
        path = tmp_path / (category + ".png")
        Image.new("RGB", (3, 3)).save(path)
        inputs.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(path),
                "mime": "image/png",
            }
        )
    prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 1000, 100000),
        inputs,
        "request",
        "prompt",
        "model",
        "endpoint",
        "group",
    )
    assert read_prepared(store, "old", 1)["reference_bytes"] == [image]
