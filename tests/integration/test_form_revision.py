import time
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator import form_revision
from ecommerce_product_image_creator.preparation import prepare_form, read_prepared
from ecommerce_product_image_creator.review import FormReview, save_review
from ecommerce_product_image_creator.storage import ResourceBudget, Store


def fixture_chain(tmp_path: Path):
    refs = []
    for category in ("artwork", "form"):
        path = tmp_path / f"{category}.png"
        Image.new("RGB", (4, 4)).save(path)
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
    form = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "original",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
    )
    store.claim_operation(
        "old-op", form["task_id"], 1, form["execution_digest"], "old-attempt"
    )
    artifact = tmp_path / "artifact.png"
    Image.new("RGB", (4, 4), "blue").save(artifact)
    import hashlib

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded',delivery_status='delivered' WHERE attempt_id='old-attempt'"
        )
        db.execute(
            "INSERT INTO artifact VALUES(?,?,?,?)",
            ("artifact", "old-attempt", str(artifact), digest),
        )
    save_review(
        store,
        FormReview(
            "reject",
            form["task_id"],
            1,
            "artifact",
            digest,
            "reject",
            "fixture",
            time.time(),
            None,
        ),
    )
    return store, budget, form, artifact, digest


def test_unverified_source_does_not_create_revision(tmp_path: Path):
    store, budget, form, _artifact, digest = fixture_chain(tmp_path)
    form_revision.record_revision_request(
        store,
        "req",
        form["task_id"],
        1,
        "old-attempt",
        "artifact",
        digest,
        "reject",
        "adjust",
        "fixture",
        time.time(),
    )
    with pytest.raises(form_revision.FormRevisionError):
        form_revision.prepare_requested_revision(
            store, "req", "new prompt", tmp_path, budget
        )
    assert read_prepared(store, form["task_id"], 1)["prompt"] == "original"


def test_revision_consumed_once_and_history_retained(tmp_path: Path, monkeypatch):
    store, budget, form, _artifact, digest = fixture_chain(tmp_path)
    from ecommerce_product_image_creator import preparation

    monkeypatch.setattr(form_revision, "_revision_source_verified", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    form_revision.record_revision_request(
        store,
        "req",
        form["task_id"],
        1,
        "old-attempt",
        "artifact",
        digest,
        "reject",
        "adjust",
        "fixture",
        time.time(),
    )
    result = form_revision.prepare_requested_revision(
        store, "req", "new prompt", tmp_path, budget
    )
    assert result["revision"] == 2
    assert "F/L/R/B/T/Q" in result["form_output_contract"][0]
    assert "top left" in result["form_output_contract"][1]
    assert "orthographic" in result["form_output_contract"][2]
    assert read_prepared(store, form["task_id"], 1)["prompt"] == "original"
    assert read_prepared(store, form["task_id"], 2)["prompt"] == "new prompt"
    with pytest.raises(form_revision.FormRevisionError):
        form_revision.prepare_requested_revision(
            store, "req", "other prompt", tmp_path, budget
        )
    assert (
        form_revision.read_revision_chain(Store(tmp_path / "state.sqlite"), "req")[
            "target_revision"
        ]
        == 2
    )


def test_revision_chain_links_fake_attempt_and_keeps_old_counter(
    tmp_path: Path, monkeypatch
):
    import hashlib
    import subprocess
    import sys

    from ecommerce_product_image_creator import offline_submission, preparation
    from ecommerce_product_image_creator.domain import (
        Confirmation,
        ExecutionSnapshot,
        PromptRevision,
        Reference,
        ReferenceCategory,
        Revision,
        Task,
        TaskStatus,
        UpstreamStatus,
    )
    from ecommerce_product_image_creator.tasks import LocalEligibility, OperationScope

    store, budget, form, artifact, digest = fixture_chain(tmp_path)
    monkeypatch.setattr(form_revision, "_revision_source_verified", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    form_revision.record_revision_request(
        store,
        "req",
        form["task_id"],
        1,
        "old-attempt",
        "artifact",
        digest,
        "reject",
        "adjust",
        "fixture",
        time.time(),
    )
    assert store.increment_attempt_counter("old-op", "model", "endpoint") == 1
    result = form_revision.prepare_requested_revision(
        store, "req", "revised F L R B T Q top left 45 orthographic", tmp_path, budget
    )
    assert store.increment_attempt_counter("old-op", "model", "endpoint") == 2
    saved = read_prepared(store, form["task_id"], 2)
    snapshot = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
    refs = []
    for meta in saved["references"]:
        path = tmp_path / (meta["ref_id"] + ".png")
        data = path.read_bytes()
        refs.append(
            Reference(
                meta["ref_id"],
                ReferenceCategory(meta["category"]),
                path,
                meta["mime"],
                len(data),
                hashlib.sha256(data).hexdigest(),
                task_id=form["task_id"],
            )
        )
    store.transition_task(form["task_id"], TaskStatus.READY)
    task = Task(form["task_id"], saved["input_digest"], "request", TaskStatus.READY)
    revision = Revision(task.task_id, 2, task.input_digest)
    prompt = PromptRevision.from_text(task.task_id, 2, 2, "form", saved["prompt"])
    confirmation = Confirmation(
        task.task_id,
        2,
        task.input_digest,
        prompt.prompt_digest,
        2,
        snapshot.execution_digest,
        confirmation_id="new-fixture",
    )
    service = LocalEligibility(store, tmp_path, budget)
    scope = OperationScope("generate", None, "six-view-composite", 3)
    args = (task, revision, refs, prompt, snapshot, confirmation, "new-op", scope)
    service.prepare(*args, expires_at=time.time() + 120)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []

    def fake(request):
        calls.append(request)
        return offline_submission.ProviderResult("accepted", task_id="fixture")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", fake)
    new = offline_submission.submit_offline(service, *args)
    chain = form_revision.read_revision_chain(store, "req")
    assert (
        chain["source_attempt"] == "old-attempt"
        and chain["attempts"][0]["attempt_id"] == new.attempt_id
    )
    assert (
        chain["trust_state"] == "unverified" and result["authorization"] == "unverified"
    )
    assert (
        calls[0].body.count(b'name="image[]"') == 1
        and artifact.read_bytes() not in calls[0].body
    )
    code = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.form_revision import read_revision_chain; c=read_revision_chain(Store(Path(sys.argv[1])),'req'); print(c['source_attempt'],c['target_revision'],c['attempts'][0]['attempt_id'])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(store.path)],
        check=True,
        text=True,
        capture_output=True,
    )
    assert child.stdout.strip() == f"old-attempt 2 {new.attempt_id}"
    assert read_prepared(store, form["task_id"], 1)["prompt"] == "original"
    from ecommerce_product_image_creator.form_workflow import register_form_artifact

    store.record_upstream(new.attempt_id, UpstreamStatus.SUCCEEDED, "fixture")
    artifact2 = tmp_path / "artifact2.png"
    Image.new("RGB", (5, 5), "green").save(artifact2)
    digest2 = hashlib.sha256(artifact2.read_bytes()).hexdigest()
    register_form_artifact(
        store,
        task.task_id,
        2,
        new.attempt_id,
        "artifact2",
        tmp_path,
        artifact2,
        "image/png",
        digest2,
        budget,
    )
    save_review(
        store,
        FormReview(
            "reject2",
            task.task_id,
            2,
            "artifact2",
            digest2,
            "reject",
            "fixture",
            time.time(),
            None,
        ),
    )
    form_revision.record_revision_request(
        store,
        "req2",
        task.task_id,
        2,
        new.attempt_id,
        "artifact2",
        digest2,
        "reject2",
        "adjust again",
        "fixture",
        time.time(),
    )
    second = form_revision.prepare_requested_revision(
        store, "req2", "second revision", tmp_path, budget
    )
    assert second["revision"] == 3
    assert (
        form_revision.read_revision_chain(store, "req2")["source_attempt"]
        == new.attempt_id
    )
    assert read_prepared(store, task.task_id, 2)["prompt"] == saved["prompt"]


def test_bad_ownership_and_duplicate_request_rejected(tmp_path: Path):
    store, _, form, _, digest = fixture_chain(tmp_path)
    for task, artifact in [("other", "artifact"), (form["task_id"], "other")]:
        with pytest.raises(form_revision.FormRevisionError):
            form_revision.record_revision_request(
                store,
                "bad",
                task,
                1,
                "old-attempt",
                artifact,
                digest,
                "reject",
                "adjust",
                "fixture",
                time.time(),
            )
    form_revision.record_revision_request(
        store,
        "req",
        form["task_id"],
        1,
        "old-attempt",
        "artifact",
        digest,
        "reject",
        "adjust",
        "fixture",
        time.time(),
    )
    with pytest.raises(form_revision.FormRevisionError):
        form_revision.record_revision_request(
            store,
            "req",
            form["task_id"],
            1,
            "old-attempt",
            "artifact",
            digest,
            "reject",
            "different",
            "fixture",
            time.time(),
        )


def test_tampered_artifact_cannot_authorize_revision(tmp_path: Path, monkeypatch):
    from ecommerce_product_image_creator import preparation

    store, budget, form, artifact, digest = fixture_chain(tmp_path)
    form_revision.record_revision_request(
        store,
        "req",
        form["task_id"],
        1,
        "old-attempt",
        "artifact",
        digest,
        "reject",
        "adjust",
        "fixture",
        time.time(),
    )
    monkeypatch.setattr(form_revision, "_revision_source_verified", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    artifact.write_bytes(b"changed")
    with pytest.raises(form_revision.FormRevisionError):
        form_revision.prepare_requested_revision(
            store, "req", "new prompt", tmp_path, budget
        )
    assert form_revision.read_revision_chain(store, "req")["target_revision"] is None


def test_frozen_reference_survives_source_change_and_old_review_is_stale(
    tmp_path: Path, monkeypatch
):
    from ecommerce_product_image_creator import preparation
    from ecommerce_product_image_creator.preparation import (
        PreparationError,
        prepare_mock,
    )

    store, budget, form, _, digest = fixture_chain(tmp_path)
    before = read_prepared(store, form["task_id"], 1)
    form_revision.record_revision_request(
        store,
        "req",
        form["task_id"],
        1,
        "old-attempt",
        "artifact",
        digest,
        "reject",
        "adjust",
        "fixture",
        time.time(),
    )
    monkeypatch.setattr(form_revision, "_revision_source_verified", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    (tmp_path / "form.png").write_bytes(b"changed original")
    form_revision.prepare_requested_revision(store, "req", "revised", tmp_path, budget)
    after = read_prepared(store, form["task_id"], 2)
    assert after["reference_bytes"] == before["reference_bytes"]
    save_review(
        store,
        FormReview(
            "old-accept",
            form["task_id"],
            1,
            "artifact",
            digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    artwork = {
        "ref_id": "artwork",
        "category": "artwork",
        "path": str(tmp_path / "artwork.png"),
        "mime": "image/png",
    }
    with pytest.raises(PreparationError):
        prepare_mock(
            store,
            tmp_path,
            budget,
            form["task_id"],
            "artifact",
            "old-accept",
            [artwork],
            "mock",
            "model",
            "endpoint",
            "group",
        )


def test_same_revision_request_concurrent_consumption_has_one_winner(
    tmp_path: Path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from ecommerce_product_image_creator import preparation

    store, budget, form, _, digest = fixture_chain(tmp_path)
    form_revision.record_revision_request(
        store,
        "req",
        form["task_id"],
        1,
        "old-attempt",
        "artifact",
        digest,
        "reject",
        "adjust",
        "fixture",
        time.time(),
    )
    monkeypatch.setattr(form_revision, "_revision_source_verified", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)

    def invoke(_):
        try:
            form_revision.prepare_requested_revision(
                Store(tmp_path / "state.sqlite"), "req", "new", tmp_path, budget
            )
            return True
        except form_revision.FormRevisionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(invoke, range(2)))
    assert sorted(outcomes) == [False, True]
