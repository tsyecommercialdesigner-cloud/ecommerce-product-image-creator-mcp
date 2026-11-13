import hashlib
import time
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator import preparation
from ecommerce_product_image_creator.preparation import (
    PreparationError,
    prepare_form,
    prepare_mock,
    read_prepared,
)
from ecommerce_product_image_creator.review import FormReview, save_review
from ecommerce_product_image_creator.storage import ResourceBudget, Store


@pytest.fixture
def fixture(tmp_path: Path):
    refs = []
    for category in ("artwork", "form"):
        path = tmp_path / (category + ".png")
        Image.new("RGB", (3, 3), "blue").save(path)
        refs.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(path),
                "mime": "image/png",
            }
        )
    store = Store(tmp_path / "state.sqlite")
    budget = ResourceBudget(100000, 10000, 100000)
    result = prepare_form(
        store, tmp_path, budget, refs, "request", "form", "model", "endpoint", "group"
    )
    store.claim_operation("op", result["task_id"], 1, "digest", "attempt")
    artifact = tmp_path / "artifact.png"
    Image.new("RGB", (3, 3), "green").save(artifact)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded',delivery_status='delivered' WHERE attempt_id='attempt'"
        )
        db.execute(
            "INSERT INTO artifact VALUES(?,?,?,?)",
            ("artifact", "attempt", str(artifact), digest),
        )
    save_review(
        store,
        FormReview(
            "review",
            result["task_id"],
            1,
            "artifact",
            digest,
            "accept",
            "test-source",
            time.time(),
            None,
        ),
    )
    return store, budget, result, refs, artifact


def test_mock_accepts_checked_fixture_but_never_asserts_authorization(
    fixture, monkeypatch
):
    store, budget, result, refs, _ = fixture
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    mock = prepare_mock(
        store,
        Path(refs[0]["path"]).parent,
        budget,
        result["task_id"],
        "artifact",
        "review",
        [refs[0]],
        "mock",
        "model",
        "endpoint",
        "group",
    )
    assert mock["stage"] == "mock" and mock["authorization"] == "unverified"
    assert (mock["model"], mock["endpoint"], mock["group"]) == (
        "model",
        "endpoint",
        "group",
    )
    assert mock["output"] == "six-view-composite"
    saved = read_prepared(store, result["task_id"], mock["revision"])
    assert saved["stage_references"] == ["artifact", "artwork"]


def test_mock_rejects_untrusted_or_missing_review(fixture):
    store, budget, result, refs, _ = fixture
    with pytest.raises(PreparationError):
        prepare_mock(
            store,
            Path(refs[0]["path"]).parent,
            budget,
            result["task_id"],
            "artifact",
            "review",
            [refs[0]],
            "mock",
            "model",
            "endpoint",
            "group",
        )
    with pytest.raises(PreparationError):
        prepare_mock(
            store,
            Path(refs[0]["path"]).parent,
            budget,
            result["task_id"],
            "artifact",
            "missing",
            [refs[0]],
            "mock",
            "model",
            "endpoint",
            "group",
        )


def test_mock_rejects_tampered_artifact(fixture, monkeypatch):
    store, budget, result, refs, artifact = fixture
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    artifact.write_bytes(b"changed")
    with pytest.raises(PreparationError):
        prepare_mock(
            store,
            Path(refs[0]["path"]).parent,
            budget,
            result["task_id"],
            "artifact",
            "review",
            [refs[0]],
            "mock",
            "model",
            "endpoint",
            "group",
        )


def test_newer_rejection_supersedes_older_acceptance(fixture, monkeypatch):
    store, budget, result, refs, artifact = fixture
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    save_review(
        store,
        FormReview(
            "new-review",
            result["task_id"],
            1,
            "artifact",
            digest,
            "reject",
            "test-source",
            time.time() + 1,
            None,
        ),
    )
    with pytest.raises(PreparationError):
        prepare_mock(
            store,
            Path(refs[0]["path"]).parent,
            budget,
            result["task_id"],
            "artifact",
            "review",
            [refs[0]],
            "mock",
            "model",
            "endpoint",
            "group",
        )


@pytest.mark.parametrize(
    "condition",
    [
        "other_task",
        "missing_artifact",
        "invalidated",
        "stale",
        "rejected",
        "pending",
        "expired",
    ],
)
def test_mock_rejects_wrong_ownership_and_stale_review(fixture, monkeypatch, condition):
    store, budget, result, refs, _ = fixture
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    task_id = result["task_id"]
    artifact_id = "artifact"
    if condition == "other_task":
        store.create_task("other-task", "other-digest", "other instructions")
        store.create_revision("other-task", 1, "other-digest")
        task_id = "other-task"
    elif condition == "missing_artifact":
        artifact_id = "missing"
    elif condition == "invalidated":
        with store._connect() as db:
            db.execute("INSERT INTO form_review_invalidation VALUES(?)", ("review",))
    elif condition in {"rejected", "pending"}:
        with store._connect() as db:
            db.execute(
                "UPDATE form_review_detail SET decision=? WHERE review_id='review'",
                ("reject" if condition == "rejected" else "pending",),
            )
    elif condition == "expired":
        with store._connect() as db:
            db.execute(
                "UPDATE form_review_detail SET expires_at=? WHERE review_id='review'",
                (time.time() - 1,),
            )
    else:
        prepare_form(
            store,
            Path(refs[0]["path"]).parent,
            budget,
            refs,
            "request",
            "new form",
            "model",
            "endpoint",
            "group",
            task_id=task_id,
        )
    with pytest.raises(PreparationError):
        prepare_mock(
            store,
            Path(refs[0]["path"]).parent,
            budget,
            task_id,
            artifact_id,
            "review",
            [refs[0]],
            "mock",
            "model",
            "endpoint",
            "group",
        )


def test_conflicting_active_decisions_require_clarification(fixture, monkeypatch):
    store, budget, result, refs, artifact = fixture
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    save_review(
        store,
        FormReview(
            "reject-review",
            result["task_id"],
            1,
            "artifact",
            digest,
            "reject",
            "test-source",
            time.time(),
            None,
        ),
    )
    with pytest.raises(PreparationError, match="conflicting"):
        prepare_mock(
            store,
            Path(refs[0]["path"]).parent,
            budget,
            result["task_id"],
            "artifact",
            "review",
            [refs[0]],
            "mock",
            "model",
            "endpoint",
            "group",
        )


def test_prepared_mock_review_rechecked_after_invalidation(fixture, monkeypatch):
    from ecommerce_product_image_creator.review import invalidate_review

    store, budget, result, refs, _ = fixture
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    mock = prepare_mock(
        store,
        Path(refs[0]["path"]).parent,
        budget,
        result["task_id"],
        "artifact",
        "review",
        [refs[0]],
        "mock",
        "model",
        "endpoint",
        "group",
    )
    with store._connect() as db:
        preparation.check_mock_review_current(db, result["task_id"], mock["revision"])
    invalidate_review(store, "review", "review withdrawn")
    with store._connect() as db, pytest.raises(PreparationError):
        preparation.check_mock_review_current(db, result["task_id"], mock["revision"])
    assert read_prepared(store, result["task_id"], mock["revision"])["prompt"] == "mock"


def test_mock_eligibility_rechecks_review_at_consumption(fixture, monkeypatch):
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
    from ecommerce_product_image_creator.review import invalidate_review
    from ecommerce_product_image_creator.tasks import (
        EligibilityError,
        LocalEligibility,
        OperationScope,
    )

    store, budget, result, refs, artifact = fixture
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    mock = prepare_mock(
        store,
        Path(refs[0]["path"]).parent,
        budget,
        result["task_id"],
        "artifact",
        "review",
        [refs[0]],
        "mock",
        "model",
        "endpoint",
        "group",
    )
    saved = read_prepared(store, result["task_id"], mock["revision"])
    snapshot = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
    context_refs = []
    for ref_id, category, path in [
        ("artifact", "form", artifact),
        ("artwork", "artwork", Path(refs[0]["path"])),
    ]:
        data = path.read_bytes()
        context_refs.append(
            Reference(
                ref_id,
                ReferenceCategory(category),
                path,
                "image/png",
                len(data),
                hashlib.sha256(data).hexdigest(),
                task_id=result["task_id"],
            )
        )
    store.transition_task(result["task_id"], TaskStatus.READY)
    task = Task(result["task_id"], mock["input_digest"], "request", TaskStatus.READY)
    revision = Revision(task.task_id, mock["revision"], task.input_digest)
    prompt = PromptRevision.from_text(
        task.task_id, revision.revision, revision.revision, "mock", "mock"
    )
    confirmation = Confirmation(
        task.task_id,
        revision.revision,
        task.input_digest,
        prompt.prompt_digest,
        prompt.prompt_revision,
        snapshot.execution_digest,
        confirmation_id="confirm",
    )
    service = LocalEligibility(store, Path(refs[0]["path"]).parent, budget)
    scope = OperationScope("generate", None, "six-view-composite", 3)
    service.prepare(
        task,
        revision,
        context_refs,
        prompt,
        snapshot,
        confirmation,
        "new-op",
        scope,
        expires_at=time.time() + 60,
    )
    invalidate_review(store, "review", "withdrawn")
    with pytest.raises(EligibilityError, match="review"):
        service.consume(
            task,
            revision,
            context_refs,
            prompt,
            snapshot,
            confirmation,
            "new-op",
            scope,
        )


def test_mock_prompt_context_recovers_checked_source(fixture, monkeypatch):
    from ecommerce_product_image_creator.tasks import (
        read_prompt_context,
        validate_prompt_context,
    )

    store, budget, result, refs, artifact = fixture
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    mock = prepare_mock(
        store,
        Path(refs[0]["path"]).parent,
        budget,
        result["task_id"],
        "artifact",
        "review",
        [refs[0]],
        "mock prompt",
        "model",
        "endpoint",
        "group",
    )
    context = read_prompt_context(store, result["task_id"], mock["revision"])
    assert context.prompt.stage == "mock"
    assert context.prompt.text == "mock prompt"
    assert context.form_source == (
        1,
        "artifact",
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
    )
    assert [name for name, _ in context.snapshot.references] == ["artifact", "artwork"]
    assert context.authorization_status == "unverified"
    assert validate_prompt_context(store, context.prompt) == context
    revised = prepare_mock(
        store,
        Path(refs[0]["path"]).parent,
        budget,
        result["task_id"],
        "artifact",
        "review",
        [refs[0]],
        "revised mock prompt",
        "model",
        "endpoint",
        "group",
    )
    new_context = read_prompt_context(store, result["task_id"], revised["revision"])
    assert new_context.form_source == context.form_source
    assert new_context.snapshot.execution_digest != context.snapshot.execution_digest
    assert read_prompt_context(store, result["task_id"], mock["revision"]) == context


def test_mock_offline_submission_requires_review_and_separate_authorization(
    fixture, monkeypatch
):
    from ecommerce_product_image_creator import offline_submission
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
    from ecommerce_product_image_creator.tasks import LocalEligibility, OperationScope

    store, budget, result, refs, artifact = fixture
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    mock = prepare_mock(
        store,
        Path(refs[0]["path"]).parent,
        budget,
        result["task_id"],
        "artifact",
        "review",
        [refs[0]],
        "mock prompt",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
    )
    saved = read_prepared(store, result["task_id"], mock["revision"])
    snapshot = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
    context_refs = []
    for ref_id, category, path in [
        ("artifact", "form", artifact),
        ("artwork", "artwork", Path(refs[0]["path"])),
    ]:
        data = path.read_bytes()
        context_refs.append(
            Reference(
                ref_id,
                ReferenceCategory(category),
                path,
                "image/png",
                len(data),
                hashlib.sha256(data).hexdigest(),
                task_id=result["task_id"],
            )
        )
    store.transition_task(result["task_id"], TaskStatus.READY)
    task = Task(result["task_id"], mock["input_digest"], "request", TaskStatus.READY)
    revision = Revision(task.task_id, mock["revision"], task.input_digest)
    prompt = PromptRevision.from_text(
        task.task_id, revision.revision, revision.revision, "mock", "mock prompt"
    )
    confirmation = Confirmation(
        task.task_id,
        revision.revision,
        task.input_digest,
        prompt.prompt_digest,
        prompt.prompt_revision,
        snapshot.execution_digest,
        confirmation_id="mock-confirm",
    )
    scope = OperationScope("generate", None, "six-view-composite", 3)
    service = LocalEligibility(store, Path(refs[0]["path"]).parent, budget)
    args = (
        task,
        revision,
        context_refs,
        prompt,
        snapshot,
        confirmation,
        "mock-op",
        scope,
    )
    service.prepare(*args, expires_at=time.time() + 120)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    seen = []

    def fake(request):
        seen.append(request)
        return offline_submission.ProviderResult("accepted", task_id="test-id")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", fake)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: False)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    assert not seen
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    assert offline_submission.submit_offline(service, *args).state == "accepted"
    assert len(seen) == 1 and b"mock prompt" in seen[0].body
    assert artifact.read_bytes() in seen[0].body
    assert Path(refs[0]["path"]).read_bytes() in seen[0].body
    assert seen[0].body.count(b'name="image[]"') == 2


def test_mock_subset_checked_apply_and_plain_prepare_bypass(fixture, monkeypatch):
    from unit.revision_fixtures import confirm_fixture_revision

    from ecommerce_product_image_creator import business_choices

    store, budget, result, refs, _ = fixture
    root = Path(refs[0]["path"]).parent
    second = root / "artwork2.png"
    Image.new("RGB", (3, 3), "red").save(second)
    a2 = {
        "ref_id": "artwork2",
        "category": "artwork",
        "path": str(second),
        "mime": "image/png",
    }
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    decision = confirm_fixture_revision(
        store,
        root,
        budget,
        result["task_id"],
        [refs[0], a2],
        monkeypatch,
        artifact_id="artifact",
    )
    mock = prepare_mock(
        store,
        root,
        budget,
        result["task_id"],
        "artifact",
        "review",
        [refs[0], a2],
        "mock",
        "model",
        "endpoint",
        "group",
        _revision_decision_id=decision,
    )
    business_choices.record_subset_proposal(
        store,
        "proposal",
        result["task_id"],
        mock["revision"],
        ["artifact", "artwork"],
        "fixture",
        "cost",
    )
    with pytest.raises(PreparationError, match="Subset"):
        prepare_mock(
            store,
            root,
            budget,
            result["task_id"],
            "artifact",
            "review",
            [refs[0]],
            "mock",
            "model",
            "endpoint",
            "group",
        )
    business_choices.record_subset_decision(
        store, "proposal", "decision", "accepted", "fixture", time.time() + 120
    )
    monkeypatch.setattr(business_choices, "_subset_source_is_trusted", lambda *_: True)
    newer = business_choices.apply_subset_decision(store, "proposal")
    saved = read_prepared(store, result["task_id"], newer["revision"])
    assert saved["stage_references"] == ["artifact", "artwork"]
    assert saved["form_source"]["artifact_id"] == "artifact"
    assert read_prepared(store, result["task_id"], mock["revision"])[
        "stage_references"
    ] == ["artifact", "artwork", "artwork2"]
    assert newer["authorization"] == "unverified"
