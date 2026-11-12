import hashlib
import time
from dataclasses import replace

import pytest
from PIL import Image

from ecommerce_product_image_creator import mock_workflow, offline_submission, tasks
from ecommerce_product_image_creator.preparation import read_prepared
from integration.test_mock_workflow import _registered_fixture


def test_task_mock_review_and_two_revision_history(tmp_path, monkeypatch):
    store, budget, form, mock, _, digest = _registered_fixture(tmp_path, monkeypatch)
    task_id, rev = form["task_id"], mock["revision"]
    source = read_prepared(store, task_id, rev)["form_source"]
    sends = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda request: sends.append(request)
    )
    with store._connect() as db:
        before_attempts = db.execute("SELECT count(*) FROM attempt").fetchone()
        before_budget = db.execute("SELECT * FROM attempt_counter").fetchall()
        form_reviews = db.execute("SELECT * FROM form_review ORDER BY rowid").fetchall()
    first = mock_workflow.MockReview(
        "task-review-1",
        task_id,
        rev,
        "mock-artifact",
        digest,
        "reject",
        "fixture",
        time.time(),
        None,
    )
    with pytest.raises(ValueError, match="mock_identity_mismatch"):
        tasks.record_task_mock_review(
            store,
            first,
            attempt_id="wrong",
            form_revision=source["form_revision"],
            form_artifact_id=source["artifact_id"],
        )
    result = tasks.record_task_mock_review(
        store,
        first,
        attempt_id="mock-attempt",
        form_revision=source["form_revision"],
        form_artifact_id=source["artifact_id"],
    )
    assert result["status"] == "recorded" and result["source_trust"] == "unverified"
    assert (
        tasks.read_task_mock_review(
            store,
            first.review_id,
            task_id,
            rev,
            "mock-attempt",
            "mock-artifact",
            source["form_revision"],
            source["artifact_id"],
        )["status"]
        == "reject"
    )
    with pytest.raises(ValueError):
        tasks.prepare_task_mock_revision(
            store, "missing-request", "next", tmp_path, budget
        )
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_revision_source_verified", lambda *_: True)
    tasks.request_task_mock_revision(
        store,
        "task-request-1",
        first.review_id,
        task_id,
        rev,
        "mock-attempt",
        "mock-artifact",
        source["form_revision"],
        source["artifact_id"],
        "fix shape",
        "fixture",
        time.time(),
    )
    second = tasks.prepare_task_mock_revision(
        store, "task-request-1", "second prompt", tmp_path, budget
    )
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == before_attempts
        assert db.execute("SELECT * FROM attempt_counter").fetchall() == before_budget
    assert second["revision"] == rev + 1
    assert read_prepared(store, task_id, rev)["prompt"] != "second prompt"
    assert read_prepared(store, task_id, second["revision"])["form_source"] == source
    store.claim_operation(
        "mock-second-op",
        task_id,
        second["revision"],
        second["execution_digest"],
        "mock-second-attempt",
    )
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded' WHERE attempt_id='mock-second-attempt'"
        )
    path = tmp_path / "mock-second.png"
    Image.new("RGB", (5, 5), "blue").save(path)
    digest2 = hashlib.sha256(path.read_bytes()).hexdigest()
    mock_workflow.register_mock_artifact(
        store,
        task_id,
        second["revision"],
        "mock-second-attempt",
        "mock-second-artifact",
        tmp_path,
        path,
        "image/png",
        digest2,
        budget,
    )
    second_review = mock_workflow.MockReview(
        "task-review-2",
        task_id,
        second["revision"],
        "mock-second-artifact",
        digest2,
        "reject",
        "fixture",
        time.time(),
        None,
    )
    tasks.record_task_mock_review(
        store,
        second_review,
        attempt_id="mock-second-attempt",
        form_revision=source["form_revision"],
        form_artifact_id=source["artifact_id"],
    )
    tasks.request_task_mock_revision(
        store,
        "task-request-2",
        second_review.review_id,
        task_id,
        second["revision"],
        "mock-second-attempt",
        "mock-second-artifact",
        source["form_revision"],
        source["artifact_id"],
        "fix again",
        "fixture",
        time.time(),
    )
    with store._connect() as db:
        before_third_attempts = db.execute("SELECT count(*) FROM attempt").fetchone()
        before_third_budget = db.execute("SELECT * FROM attempt_counter").fetchall()
    third = tasks.prepare_task_mock_revision(
        store, "task-request-2", "third prompt", tmp_path, budget
    )
    assert third["revision"] == second["revision"] + 1
    assert (
        read_prepared(store, task_id, second["revision"])["prompt"] == "second prompt"
    )
    assert read_prepared(store, task_id, third["revision"])["prompt"] == "third prompt"
    assert sends == []
    with pytest.raises(ValueError, match="mock_approval_unverified_or_invalid"):
        tasks.check_task_mock_approval(
            store,
            first.review_id,
            task_id,
            rev,
            "mock-attempt",
            "mock-artifact",
            digest,
            source["form_revision"],
            source["artifact_id"],
            tmp_path,
            budget,
        )
    with store._connect() as db:
        assert (
            db.execute("SELECT count(*) FROM attempt").fetchone()
            == before_third_attempts
        )
        assert (
            db.execute("SELECT * FROM attempt_counter").fetchall()
            == before_third_budget
        )
        assert (
            db.execute("SELECT * FROM form_review ORDER BY rowid").fetchall()
            == form_reviews
        )
        assert db.execute("SELECT count(*) FROM mock_review").fetchone() == (2,)
        assert db.execute(
            "SELECT count(*) FROM artifact WHERE artifact_id IN ('mock-artifact','mock-second-artifact')"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT count(*) FROM operation WHERE operation_id='mock-second-op'"
        ).fetchone() == (1,)


def test_task_mock_identity_trust_and_delegation(tmp_path, monkeypatch):
    store, budget, form, mock, _, digest = _registered_fixture(tmp_path, monkeypatch)
    task_id, rev = form["task_id"], mock["revision"]
    form_source = read_prepared(store, task_id, rev)["form_source"]
    review = mock_workflow.MockReview(
        "candidate-accept",
        task_id,
        rev,
        "mock-artifact",
        digest,
        "accept",
        "fixture",
        time.time(),
        None,
    )
    with store._connect() as db:
        before_attempts = db.execute("SELECT count(*) FROM attempt").fetchone()
        before_budget = db.execute("SELECT * FROM attempt_counter").fetchall()
    calls = []
    original = mock_workflow.save_mock_review

    def spy(store_arg, review_arg):
        calls.append(review_arg.review_id)
        return original(store_arg, review_arg)

    monkeypatch.setattr(mock_workflow, "save_mock_review", spy)
    for changed in (
        {"attempt_id": "wrong"},
        {"form_revision": form_source["form_revision"] + 1},
        {"form_artifact_id": "wrong"},
    ):
        identity = {
            "attempt_id": "mock-attempt",
            "form_revision": form_source["form_revision"],
            "form_artifact_id": form_source["artifact_id"],
        }
        with pytest.raises(ValueError, match="mock_identity_mismatch"):
            tasks.record_task_mock_review(store, review, **(identity | changed))
    for altered in (
        replace(review, artifact_id="missing-artifact"),
        replace(review, revision=rev + 1),
        replace(review, task_id="wrong-task"),
    ):
        with pytest.raises(ValueError, match="mock_identity_mismatch"):
            tasks.record_task_mock_review(
                store,
                altered,
                attempt_id="mock-attempt",
                form_revision=form_source["form_revision"],
                form_artifact_id=form_source["artifact_id"],
            )
    assert calls == []
    tasks.record_task_mock_review(
        store,
        review,
        attempt_id="mock-attempt",
        form_revision=form_source["form_revision"],
        form_artifact_id=form_source["artifact_id"],
    )
    assert calls == ["candidate-accept"]
    with pytest.raises(ValueError, match="mock_approval_unverified_or_invalid"):
        tasks.check_task_mock_approval(
            store,
            review.review_id,
            task_id,
            rev,
            "mock-attempt",
            "mock-artifact",
            digest,
            form_source["form_revision"],
            form_source["artifact_id"],
            tmp_path,
            budget,
        )
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    checked = tasks.check_task_mock_approval(
        store,
        review.review_id,
        task_id,
        rev,
        "mock-attempt",
        "mock-artifact",
        digest,
        form_source["form_revision"],
        form_source["artifact_id"],
        tmp_path,
        budget,
    )
    assert checked == {"status": "applicable", "source_trust": "verifier_accepted"}
    with pytest.raises(ValueError, match="mock_identity_mismatch"):
        tasks.read_task_mock_review(
            store,
            review.review_id,
            task_id,
            rev + 1,
            "mock-attempt",
            "mock-artifact",
            form_source["form_revision"],
            form_source["artifact_id"],
        )
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == before_attempts
        assert db.execute("SELECT * FROM attempt_counter").fetchall() == before_budget
