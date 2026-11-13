"""Candidate-bound local eligibility remains distinct from authorization."""

import time
from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator import offline_submission
from ecommerce_product_image_creator.tasks import EligibilityError

from .test_attempt003 import prepare_third
from .test_candidate_preflight import candidates


def test_candidate_eligibility_is_distinct_and_cannot_bypass_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, _, calls = prepare_third(tmp_path, monkeypatch)
    offline_submission.submit_offline(service, *args, retry_of="op")
    _, candidate = candidates(service, args)
    confirmation = replace(
        args[5],
        confirmation_id="flare-local",
        execution_digest=candidate.snapshot.execution_digest,
    )
    local = (*args[:4], candidate.snapshot, confirmation, "flare-operation", args[7])
    service.prepare(*local, expires_at=time.time() + 120, candidate_id="b")
    with service.store._connect() as db:
        assert db.execute(
            "SELECT candidate_id,root_operation_id FROM candidate_eligibility WHERE operation_id=?",
            ("flare-operation",),
        ).fetchone() == ("b", "op")
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id=?",
            ("flare-operation",),
        ).fetchone() == (0,)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *local, retry_of="op")
    assert len(calls) == 3


def test_candidate_eligibility_rejects_old_confirmation_and_wrong_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, _, _ = prepare_third(tmp_path, monkeypatch)
    offline_submission.submit_offline(service, *args, retry_of="op")
    _, candidate = candidates(service, args)
    local = (*args[:4], candidate.snapshot, args[5], "bad-operation", args[7])
    with pytest.raises(EligibilityError):
        service.prepare(*local, expires_at=time.time() + 120, candidate_id="b")
    confirmation = replace(
        args[5],
        confirmation_id="new",
        execution_digest=candidate.snapshot.execution_digest,
    )
    local = (*args[:4], candidate.snapshot, confirmation, "bad-operation", args[7])
    with pytest.raises(EligibilityError):
        service.prepare(*local, expires_at=time.time() + 120, candidate_id="a")
