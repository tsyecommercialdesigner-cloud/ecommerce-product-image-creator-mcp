"""Human revision keeps independent roots through the existing submission path."""

import hashlib
import time

import pytest
from PIL import Image

from ecommerce_product_image_creator import (
    first_authorization,
    form_revision,
    offline_submission,
    preparation,
    tasks,
)
from ecommerce_product_image_creator.execution_candidates import register_root_candidate
from ecommerce_product_image_creator.form_workflow import register_form_artifact
from ecommerce_product_image_creator.preparation import read_prepared
from ecommerce_product_image_creator.review import FormReview, save_review
from integration.test_multiview_completion import prepare_eligibility
from integration.test_offline_submission import setup_context


def grant_fixture(service, args, monkeypatch):
    snapshot, operation = args[4], args[6]
    monkeypatch.setattr(
        first_authorization,
        "_verify_source",
        lambda db, evidence: evidence.source == "isolated-fixture",
    )
    with service.store._connect() as db:
        for kind in ("host", "fee"):
            first_authorization.record(
                db,
                first_authorization.FirstAuthorization(
                    f"{operation}-{kind}",
                    kind,
                    operation,
                    snapshot.task_id,
                    snapshot.revision,
                    snapshot.stage,
                    snapshot.execution_digest,
                    snapshot.model,
                    snapshot.endpoint,
                    snapshot.group,
                    snapshot.operation_scope,
                    time.time() + 120,
                    "isolated-fixture",
                    f"{operation}-{kind}-fixture",
                ),
            )


def test_form_revision_keeps_independent_execution_roots(tmp_path, monkeypatch):
    service, args = setup_context(tmp_path, "original F L R B T Q")
    store, budget = service.store, service.budget
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    monkeypatch.setattr(form_revision, "_revision_source_verified", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda request: (
            calls.append(request)
            or offline_submission.ProviderResult("succeeded", task_id="fixture")
        ),
    )
    grant_fixture(service, args, monkeypatch)
    first = offline_submission.submit_offline(
        service, *args, require_first_authorizations=True
    )
    register_root_candidate(store, "op", "source-f1")
    image = tmp_path / "form-result.png"
    Image.new("RGB", (5, 5), "blue").save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    task_id = args[0].task_id
    register_form_artifact(
        store,
        task_id,
        1,
        first.attempt_id,
        "form-result",
        tmp_path,
        image,
        "image/png",
        digest,
        budget,
    )
    save_review(
        store,
        FormReview(
            "review-f1",
            task_id,
            1,
            "form-result",
            digest,
            "reject",
            "fixture",
            time.time(),
            None,
        ),
    )
    old_state = tasks.read_task_execution(service, task_id, 1, "form", "op")
    form_revision.record_revision_request(
        store,
        "request-f2",
        task_id,
        1,
        first.attempt_id,
        "form-result",
        digest,
        "review-f1",
        "revise shape",
        "fixture",
        time.time(),
    )
    revised = form_revision.prepare_requested_revision(
        store, "request-f2", "revised F L R B T Q", tmp_path, budget
    )
    assert revised["revision"] == 2 and len(calls) == 1
    assert (
        tasks.read_task_execution(service, task_id, 1, "form", "op")[
            "budget"
        ].total_used
        == 1
    )
    next_service, next_args = prepare_eligibility(
        store, tmp_path, budget, task_id, 2, "form-revision-op"
    )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(
            next_service, *next_args, require_first_authorizations=True
        )
    assert len(calls) == 1 and store.get_operation("form-revision-op") is None
    grant_fixture(next_service, next_args, monkeypatch)
    second = offline_submission.submit_offline(
        next_service, *next_args, require_first_authorizations=True
    )
    register_root_candidate(store, "form-revision-op", "source-f2")
    new_state = tasks.read_task_execution(
        service, task_id, 2, "form", "form-revision-op"
    )
    old_after = tasks.read_task_execution(service, task_id, 1, "form", "op")
    assert len(calls) == 2 and first.attempt_id != second.attempt_id
    assert old_state["budget"].total_used == old_after["budget"].total_used == 1
    assert (
        new_state["budget"].total_used == 1
        and new_state["attempts"][0]["attempt_id"] == second.attempt_id
    )
    assert old_after["attempts"][0]["attempt_id"] == first.attempt_id
    assert read_prepared(store, task_id, 1)["prompt"] == args[3].text
    assert (
        form_revision.read_revision_chain(store, "request-f2")["attempts"][0][
            "attempt_id"
        ]
        == second.attempt_id
    )
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM first_authorization_consumption WHERE operation_id IN ('op','form-revision-op')"
        ).fetchone() == (4,)
        assert db.execute(
            "SELECT operation_id,consumed FROM local_eligibility WHERE operation_id IN ('op','form-revision-op') ORDER BY operation_id"
        ).fetchall() == [("form-revision-op", 1), ("op", 1)]
        assert db.execute(
            "SELECT attempt_id FROM artifact WHERE artifact_id='form-result'"
        ).fetchone() == (first.attempt_id,)
        assert db.execute(
            "SELECT COUNT(*) FROM form_review WHERE review_id='review-f1'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM form_review WHERE task_id=? AND form_revision=2",
            (task_id,),
        ).fetchone() == (0,)
