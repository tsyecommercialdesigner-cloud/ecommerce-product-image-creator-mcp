"""Offline submission authorization and immutable-scope contract."""

from pathlib import Path

import pytest
from integration.test_offline_submission import setup_context

from ecommerce_product_image_creator import offline_submission
from ecommerce_product_image_creator.domain import PromptRevision
from ecommerce_product_image_creator.tasks import OperationScope


@pytest.mark.parametrize(
    "reason",
    ["no_host_evidence", "scope_mismatch", "prompt_change", "unresolved_conflict"],
)
def test_denial_never_dispatches(tmp_path: Path, monkeypatch, reason: str):
    service, args = setup_context(tmp_path)
    sends = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda request: sends.append(request)
    )
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_verify_authorization",
        lambda *_: reason != "no_host_evidence",
    )
    if reason == "scope_mismatch":
        args = (*args[:-1], OperationScope("generate", None, "six-view-composite", 2))
    elif reason == "prompt_change":
        args = (
            *args[:3],
            PromptRevision.from_text(args[0].task_id, 1, 1, "form", "changed"),
            *args[4:],
        )
    elif reason == "unresolved_conflict":
        with service.store._connect() as db:
            db.execute(
                "UPDATE prepared_conflicts SET reported=? WHERE task_id=? AND revision=1",
                ('["uncertain"]', args[0].task_id),
            )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    assert sends == []
    assert service.store.get_operation("op") is None


def test_ordinary_claims_and_api_key_do_not_authorize(tmp_path: Path, monkeypatch):
    service, args = setup_context(tmp_path)
    sent = []
    monkeypatch.setenv("ZHENZHEN_GPTIMAGEGEN_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda request: sent.append(request)
    )
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    assert args[5].confirmation_id == "fixture-confirmation"
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    assert not sent and service.store.get_operation("op") is None
