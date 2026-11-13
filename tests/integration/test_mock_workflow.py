import hashlib
import time
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator import mock_workflow, preparation
from ecommerce_product_image_creator.preparation import (
    prepare_form,
    prepare_mock,
    read_prepared,
)
from ecommerce_product_image_creator.review import FormReview, save_review
from ecommerce_product_image_creator.storage import ResourceBudget, Store


def mock_fixture(tmp_path: Path, monkeypatch):
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
                "version": "v1",
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
        "form prompt",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
    )
    store.claim_operation(
        "form-op", form["task_id"], 1, form["execution_digest"], "form-attempt"
    )
    path = tmp_path / "form-artifact.png"
    Image.new("RGB", (5, 5), "blue").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded',delivery_status='delivered' WHERE attempt_id='form-attempt'"
        )
        db.execute(
            "INSERT INTO artifact VALUES(?,?,?,?)",
            ("form-artifact", "form-attempt", str(path), digest),
        )
    save_review(
        store,
        FormReview(
            "form-review",
            form["task_id"],
            1,
            "form-artifact",
            digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    mock = prepare_mock(
        store,
        tmp_path,
        budget,
        form["task_id"],
        "form-artifact",
        "form-review",
        [refs[0]],
        "mock prompt",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
    )
    store.claim_operation(
        "mock-op",
        form["task_id"],
        mock["revision"],
        mock["execution_digest"],
        "mock-attempt",
    )
    mockpath = tmp_path / "mock-artifact.png"
    Image.new("RGB", (5, 5), "green").save(mockpath)
    mockdigest = hashlib.sha256(mockpath.read_bytes()).hexdigest()
    return store, budget, form, mock, mockpath, mockdigest


def test_registration_needs_upstream_success(tmp_path: Path, monkeypatch):
    store, budget, form, mock, path, digest = mock_fixture(tmp_path, monkeypatch)
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.register_mock_artifact(
            store,
            form["task_id"],
            mock["revision"],
            "mock-attempt",
            "mock-artifact",
            tmp_path,
            path,
            "image/png",
            digest,
            budget,
        )


def test_rejection_revision_and_trust_boundary(tmp_path: Path, monkeypatch):
    import subprocess
    import sys

    store, budget, form, mock, path, digest = mock_fixture(tmp_path, monkeypatch)
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded' WHERE attempt_id='mock-attempt'"
        )
    mock_workflow.register_mock_artifact(
        store,
        form["task_id"],
        mock["revision"],
        "mock-attempt",
        "mock-artifact",
        tmp_path,
        path,
        "image/png",
        digest,
        budget,
    )
    review = mock_workflow.MockReview(
        "mock-review",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        "reject",
        "fixture",
        time.time(),
        None,
        view="F",
        feedback="alignment",
    )
    mock_workflow.save_mock_review(store, review)
    mock_workflow.record_mock_revision_request(
        store, "request", review, "fix F", "fixture", time.time()
    )
    assert store.increment_attempt_counter("mock-op", "model", "endpoint") == 1
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.prepare_mock_revision(
            store, "request", "revised", tmp_path, budget
        )
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_revision_source_verified", lambda *_: True)
    new = mock_workflow.prepare_mock_revision(
        store, "request", "revised", tmp_path, budget
    )
    assert new["revision"] == mock["revision"] + 1
    assert (
        read_prepared(store, form["task_id"], mock["revision"])["prompt"]
        == "mock prompt"
    )
    assert read_prepared(store, form["task_id"], new["revision"])["prompt"] == "revised"
    assert (
        read_prepared(store, form["task_id"], new["revision"])["form_source"]
        == read_prepared(store, form["task_id"], mock["revision"])["form_source"]
    )
    store.claim_operation(
        "new-op",
        form["task_id"],
        new["revision"],
        new["execution_digest"],
        "new-attempt",
    )
    assert store.increment_attempt_counter("mock-op", "model", "endpoint") == 2
    assert store.increment_attempt_counter("new-op", "model", "endpoint") == 1
    chain = mock_workflow.read_mock_revision_chain(store, "request")
    assert chain["source_attempt"] == "mock-attempt"
    assert chain["attempts"][0]["attempt_id"] == "new-attempt"
    assert chain["form_source"]["artifact_id"] == "form-artifact"
    assert chain["packaging_versions"] == [{"ref_id": "artwork", "version": "v1"}]
    code = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.mock_workflow import read_mock_revision_chain; c=read_mock_revision_chain(Store(Path(sys.argv[1])),'request'); print(c['source_attempt'],c['target_revision'],c['attempts'][0]['attempt_id'])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(store.path)],
        check=True,
        text=True,
        capture_output=True,
    )
    assert child.stdout.strip() == f"mock-attempt {new['revision']} new-attempt"
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.prepare_mock_revision(
            store, "request", "revised", tmp_path, budget
        )


def _registered_fixture(tmp_path, monkeypatch):
    store, budget, form, mock, path, digest = mock_fixture(tmp_path, monkeypatch)
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded' WHERE attempt_id='mock-attempt'"
        )
    mock_workflow.register_mock_artifact(
        store,
        form["task_id"],
        mock["revision"],
        "mock-attempt",
        "mock-artifact",
        tmp_path,
        path,
        "image/png",
        digest,
        budget,
    )
    return store, budget, form, mock, path, digest


def test_artifact_identity_and_single_output(tmp_path, monkeypatch):
    store, budget, form, mock, path, digest = _registered_fixture(tmp_path, monkeypatch)
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.register_mock_artifact(
            store,
            form["task_id"],
            mock["revision"],
            "mock-attempt",
            "other",
            tmp_path,
            path,
            "image/png",
            digest,
            budget,
        )
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM artifact WHERE attempt_id='mock-attempt'"
        ).fetchone() == (1,)
    saved = read_prepared(store, form["task_id"], mock["revision"])
    assert saved["form_source"]["artifact_id"] == "form-artifact"
    candidate = mock_workflow.read_mock_candidate(
        store, form["task_id"], mock["revision"], "mock-attempt"
    )
    assert (
        candidate["review_status"] == "pending"
        and candidate["file_state"] == "unverified"
    )
    assert candidate["form_source"]["artifact_id"] == "form-artifact"
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.read_mock_candidate(
            store, "other", mock["revision"], "mock-attempt"
        )
    assert [
        r["version"] for r in saved["references"] if r["category"] == "artwork"
    ] == ["v1"]


def test_mock_review_accept_conflict_invalidation_and_view_feedback(
    tmp_path, monkeypatch
):
    from contextlib import closing

    store, _budget, form, mock, _path, digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    accepted = mock_workflow.MockReview(
        "accept",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        "accept",
        "fixture",
        time.time(),
        None,
        view="Q",
        feedback="angle",
    )
    mock_workflow.save_mock_review(store, accepted)
    state = mock_workflow.read_mock_review_state(store, "accept")
    assert state["status"] == "accept" and state["source_trust"] == "unverified"
    assert state["review"].view == "Q" and state["review"].feedback == "angle"
    with (
        closing(store._connect()) as db,
        pytest.raises(mock_workflow.MockWorkflowError),
    ):
        mock_workflow._check_review(db, "accept", "accept")
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    with closing(store._connect()) as db:
        assert mock_workflow._check_review(db, "accept", "accept") == accepted
    rejected = mock_workflow.MockReview(
        "reject",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        "reject",
        "fixture",
        time.time(),
        None,
    )
    mock_workflow.save_mock_review(store, rejected)
    assert mock_workflow.read_mock_review_state(store, "accept")["status"] == "conflict"
    with (
        closing(store._connect()) as db,
        pytest.raises(mock_workflow.MockWorkflowError, match="Conflicting"),
    ):
        mock_workflow._check_review(db, "reject", "reject")
    mock_workflow.invalidate_mock_review(store, "reject", "withdrawn")
    assert (
        mock_workflow.read_mock_review_state(
            Store(tmp_path / "state.sqlite"), "reject"
        )["status"]
        == "invalidated"
    )


def test_expired_review_or_changed_artifact_blocks_revision(tmp_path, monkeypatch):
    store, budget, form, mock, _path, digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    now = time.time()
    review = mock_workflow.MockReview(
        "review",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        "reject",
        "fixture",
        now - 20,
        None,
        expires_at=now - 1,
    )
    mock_workflow.save_mock_review(store, review)
    assert mock_workflow.read_mock_review_state(store, "review")["status"] == "expired"
    mock_workflow.record_mock_revision_request(
        store, "request", review, "fix", "fixture", now
    )
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_revision_source_verified", lambda *_: True)
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.prepare_mock_revision(
            store, "request", "revised", tmp_path, budget
        )


def test_concurrent_request_has_one_winner(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    store, budget, form, mock, _path, digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    review = mock_workflow.MockReview(
        "reject",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        "reject",
        "fixture",
        time.time(),
        None,
    )
    mock_workflow.save_mock_review(store, review)
    mock_workflow.record_mock_revision_request(
        store, "request", review, "fix", "fixture", time.time()
    )
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_revision_source_verified", lambda *_: True)

    def consume(_):
        try:
            mock_workflow.prepare_mock_revision(
                Store(tmp_path / "state.sqlite"), "request", "revised", tmp_path, budget
            )
            return True
        except mock_workflow.MockWorkflowError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(consume, range(2)))
    assert sorted(outcomes) == [False, True]
    assert (
        mock_workflow.read_mock_revision_chain(store, "request")["target_revision"]
        == mock["revision"] + 1
    )


def test_mismatched_review_or_corrupt_artifact_rejected(tmp_path, monkeypatch):
    from dataclasses import replace

    store, budget, form, mock, path, digest = _registered_fixture(tmp_path, monkeypatch)
    review = mock_workflow.MockReview(
        "reject",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        "reject",
        "fixture",
        time.time(),
        None,
    )
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.save_mock_review(store, replace(review, task_id="other"))
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.save_mock_review(store, replace(review, digest="a" * 64))
    mock_workflow.save_mock_review(store, review)
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.record_mock_revision_request(
            store,
            "bad",
            replace(review, artifact_id="other"),
            "fix",
            "fixture",
            time.time(),
        )
    mock_workflow.record_mock_revision_request(
        store, "request", review, "fix", "fixture", time.time()
    )
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_revision_source_verified", lambda *_: True)
    path.write_bytes(b"changed")
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.prepare_mock_revision(
            store, "request", "revised", tmp_path, budget
        )
    assert (
        mock_workflow.read_mock_revision_chain(store, "request")["target_revision"]
        is None
    )


def test_unknown_mock_attempt_stays_unknown_without_new_attempt(tmp_path, monkeypatch):
    from ecommerce_product_image_creator.domain import UpstreamStatus

    store, _, form, mock, _, _ = mock_fixture(tmp_path, monkeypatch)
    store.record_upstream("mock-attempt", UpstreamStatus.UNKNOWN, "upstream")
    candidate = mock_workflow.read_mock_candidate(
        store, form["task_id"], mock["revision"], "mock-attempt"
    )
    assert (
        candidate["upstream"] == "unknown" and candidate["delivery"] == "not_available"
    )
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM attempt WHERE task_id=? AND revision=?",
            (form["task_id"], mock["revision"]),
        ).fetchone() == (1,)


def test_revision_fails_if_original_form_review_was_invalidated(tmp_path, monkeypatch):
    from ecommerce_product_image_creator.review import invalidate_review

    store, budget, form, mock, _path, digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    review = mock_workflow.MockReview(
        "reject",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        "reject",
        "fixture",
        time.time(),
        None,
    )
    mock_workflow.save_mock_review(store, review)
    mock_workflow.record_mock_revision_request(
        store, "request", review, "fix", "fixture", time.time()
    )
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_revision_source_verified", lambda *_: True)
    invalidate_review(store, "form-review", "withdrawn")
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.prepare_mock_revision(
            store, "request", "revised", tmp_path, budget
        )
    assert (
        mock_workflow.read_mock_revision_chain(store, "request")["target_revision"]
        is None
    )


def test_acceptance_is_bound_to_current_mock_revision(tmp_path, monkeypatch):
    store, budget, form, mock, _path, digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    review = mock_workflow.MockReview(
        "accept",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        "accept",
        "fixture",
        time.time(),
        None,
    )
    mock_workflow.save_mock_review(store, review)
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.check_mock_approval(
            store,
            "accept",
            form["task_id"],
            mock["revision"],
            "mock-artifact",
            digest,
            tmp_path,
            budget,
        )

    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    mock_workflow.check_mock_approval(
        store,
        "accept",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        tmp_path,
        budget,
    )
    art = {
        "ref_id": "artwork",
        "category": "artwork",
        "path": str(tmp_path / "artwork.png"),
        "mime": "image/png",
        "version": "v1",
    }
    prepare_mock(
        store,
        tmp_path,
        budget,
        form["task_id"],
        "form-artifact",
        "form-review",
        [art],
        "new prompt",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
    )
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.check_mock_approval(
            store,
            "accept",
            form["task_id"],
            mock["revision"],
            "mock-artifact",
            digest,
            tmp_path,
            budget,
        )


def test_revision_rejects_changed_form_artifact(tmp_path, monkeypatch):
    store, budget, form, mock, _path, digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    review = mock_workflow.MockReview(
        "reject",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        "reject",
        "fixture",
        time.time(),
        None,
    )
    mock_workflow.save_mock_review(store, review)
    mock_workflow.record_mock_revision_request(
        store, "request", review, "fix", "fixture", time.time()
    )
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_revision_source_verified", lambda *_: True)
    (tmp_path / "form-artifact.png").write_bytes(b"changed")
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.prepare_mock_revision(
            store, "request", "revised", tmp_path, budget
        )
    assert (
        mock_workflow.read_mock_revision_chain(store, "request")["target_revision"]
        is None
    )
