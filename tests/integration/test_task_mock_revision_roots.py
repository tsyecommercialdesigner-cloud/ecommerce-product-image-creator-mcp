"""Mock revisions use separate authorized offline roots."""

import hashlib
import time

import pytest
from PIL import Image

from ecommerce_product_image_creator import (
    mock_workflow,
    offline_submission,
    preparation,
    tasks,
)
from ecommerce_product_image_creator.execution_candidates import register_root_candidate
from ecommerce_product_image_creator.form_workflow import register_form_artifact
from ecommerce_product_image_creator.preparation import prepare_mock, read_prepared
from ecommerce_product_image_creator.review import FormReview, save_review
from integration.test_multiview_completion import prepare_eligibility
from integration.test_offline_submission import setup_context
from integration.test_task_revision_roots import grant_fixture


def test_mock_revision_keeps_independent_execution_roots(tmp_path, monkeypatch):
    service, form_args = setup_context(tmp_path, "form F L R B T Q")
    store, budget = service.store, service.budget
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_revision_source_verified", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda request: (
            calls.append(request)
            or offline_submission.ProviderResult("succeeded", task_id="fixture")
        ),
    )
    grant_fixture(service, form_args, monkeypatch)
    form_attempt = offline_submission.submit_offline(
        service, *form_args, require_first_authorizations=True
    )
    task_id = form_args[0].task_id
    form_file = tmp_path / "form-result.png"
    Image.new("RGB", (5, 5), "blue").save(form_file)
    form_digest = hashlib.sha256(form_file.read_bytes()).hexdigest()
    register_form_artifact(
        store,
        task_id,
        1,
        form_attempt.attempt_id,
        "form-result",
        tmp_path,
        form_file,
        "image/png",
        form_digest,
        budget,
    )
    save_review(
        store,
        FormReview(
            "form-approved",
            task_id,
            1,
            "form-result",
            form_digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    mock = prepare_mock(
        store,
        tmp_path,
        budget,
        task_id,
        "form-result",
        "form-approved",
        [
            {
                "ref_id": "artwork",
                "category": "artwork",
                "path": str(tmp_path / "artwork.png"),
                "mime": "image/png",
            }
        ],
        "mock F L R B T Q",
        form_args[4].model,
        form_args[4].endpoint,
        form_args[4].group,
    )
    m1 = mock["revision"]
    mock_service, mock_args = prepare_eligibility(
        store, tmp_path, budget, task_id, m1, "mock-root-1"
    )
    grant_fixture(mock_service, mock_args, monkeypatch)
    first = offline_submission.submit_offline(
        mock_service, *mock_args, require_first_authorizations=True
    )
    register_root_candidate(store, "mock-root-1", "mock-candidate-1")
    mock_file = tmp_path / "mock-result.png"
    Image.new("RGB", (5, 5), "green").save(mock_file)
    mock_digest = hashlib.sha256(mock_file.read_bytes()).hexdigest()
    mock_workflow.register_mock_artifact(
        store,
        task_id,
        m1,
        first.attempt_id,
        "mock-result",
        tmp_path,
        mock_file,
        "image/png",
        mock_digest,
        budget,
    )
    source = read_prepared(store, task_id, m1)["form_source"]
    review = mock_workflow.MockReview(
        "mock-review-1",
        task_id,
        m1,
        "mock-result",
        mock_digest,
        "reject",
        "fixture",
        time.time(),
        None,
    )
    tasks.record_task_mock_review(
        store,
        review,
        attempt_id=first.attempt_id,
        form_revision=source["form_revision"],
        form_artifact_id=source["artifact_id"],
    )
    old_state = tasks.read_task_execution(service, task_id, m1, "mock", "mock-root-1")
    tasks.request_task_mock_revision(
        store,
        "request-m2",
        review.review_id,
        task_id,
        m1,
        first.attempt_id,
        "mock-result",
        source["form_revision"],
        source["artifact_id"],
        "revise design",
        "fixture",
        time.time(),
    )
    revised = tasks.prepare_task_mock_revision(
        store, "request-m2", "revised mock F L R B T Q", tmp_path, budget
    )
    m2 = revised["revision"]
    assert m2 != m1 and len(calls) == 2
    assert (
        tasks.read_task_execution(service, task_id, m1, "mock", "mock-root-1")[
            "budget"
        ].total_used
        == 1
    )
    assert read_prepared(store, task_id, m2)["form_source"] == source
    next_service, next_args = prepare_eligibility(
        store, tmp_path, budget, task_id, m2, "mock-root-2"
    )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(
            next_service, *next_args, require_first_authorizations=True
        )
    assert len(calls) == 2 and store.get_operation("mock-root-2") is None
    grant_fixture(next_service, next_args, monkeypatch)
    second = offline_submission.submit_offline(
        next_service, *next_args, require_first_authorizations=True
    )
    register_root_candidate(store, "mock-root-2", "mock-candidate-2")
    old_after = tasks.read_task_execution(service, task_id, m1, "mock", "mock-root-1")
    new_state = tasks.read_task_execution(service, task_id, m2, "mock", "mock-root-2")
    assert len(calls) == 3 and first.attempt_id != second.attempt_id
    assert old_state["budget"].total_used == old_after["budget"].total_used == 1
    assert (
        new_state["budget"].total_used == 1
        and new_state["attempts"][0]["attempt_id"] == second.attempt_id
    )
    assert old_after["attempts"][0]["attempt_id"] == first.attempt_id
    assert read_prepared(store, task_id, m1)["prompt"] == "mock F L R B T Q"
    chain = mock_workflow.read_mock_revision_chain(store, "request-m2")
    assert (
        chain["source_attempt"] == first.attempt_id and chain["target_revision"] == m2
    )
    assert chain["attempts"][0]["attempt_id"] == second.attempt_id
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM first_authorization_consumption WHERE operation_id IN ('mock-root-1','mock-root-2')"
        ).fetchone() == (4,)
        assert db.execute(
            "SELECT operation_id,consumed FROM local_eligibility WHERE operation_id IN ('mock-root-1','mock-root-2') ORDER BY operation_id"
        ).fetchall() == [("mock-root-1", 1), ("mock-root-2", 1)]
        assert db.execute(
            "SELECT attempt_id FROM artifact WHERE artifact_id='mock-result'"
        ).fetchone() == (first.attempt_id,)
        assert db.execute(
            "SELECT COUNT(*) FROM mock_review WHERE review_id='mock-review-1'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM mock_review WHERE task_id=? AND revision=?",
            (task_id, m2),
        ).fetchone() == (0,)
