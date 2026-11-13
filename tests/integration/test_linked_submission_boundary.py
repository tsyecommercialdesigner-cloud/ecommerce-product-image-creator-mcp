"""A risk reservation alone never authorizes a new submission."""

import dataclasses
import socket
import time
from pathlib import Path

import pytest

from ecommerce_product_image_creator import offline_submission, risk_reservation
from ecommerce_product_image_creator.execution_candidates import register_root_candidate

from .test_offline_submission import setup_context


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def test_reservation_does_not_bypass_submit_or_consume_new_eligibility(
    tmp_path: Path, monkeypatch
):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    sends = []

    def original_timeout(_):
        sends.append("original")
        raise TimeoutError()

    monkeypatch.setattr(offline_submission, "_offline_dispatch", original_timeout)
    original = offline_submission.submit_offline(service, *args)
    candidate = register_root_candidate(service.store, "op", "candidate-a")
    digest = candidate.snapshot.execution_digest
    disclosure = risk_reservation.RiskDisclosure(
        "op",
        "candidate-a",
        original.attempt_id,
        digest,
        "linked-op",
        digest,
        "unknown",
        True,
        True,
        True,
        "unknown",
        1,
    )
    evidence = risk_reservation.RiskEvidence(
        "risk-auth",
        "op",
        "candidate-a",
        original.attempt_id,
        digest,
        "linked-op",
        candidate.snapshot.to_payload(),
        "notice",
        "acceptance",
        "execution",
        time.time() + 120,
        disclosure,
    )
    monkeypatch.setattr(
        risk_reservation,
        "_verify_risk_acceptance",
        lambda _db, e: e.risk_acceptance_reference == "acceptance",
    )
    monkeypatch.setattr(
        risk_reservation,
        "_verify_execution_authorization",
        lambda _db, e: e.execution_authorization_reference == "execution",
    )
    assert risk_reservation.reserve(service.store, evidence).can_send is False
    confirmation = dataclasses.replace(args[5], confirmation_id="linked-confirmation")
    linked_args = (*args[:5], confirmation, "linked-op", args[7])
    service.prepare(*linked_args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        before = {
            table: db.execute("SELECT * FROM " + table).fetchall()
            for table in (
                "attempt",
                "operation",
                "operation_attempt",
                "attempt_counter",
                "local_eligibility",
                "risk_reservation",
            )
        }
    with pytest.raises(
        offline_submission.SubmissionError,
        match="Execution already has a logical operation",
    ):
        offline_submission.submit_offline(service, *linked_args)
    with service.store._connect() as db:
        assert {
            table: db.execute("SELECT * FROM " + table).fetchall() for table in before
        } == before
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='linked-op'"
        ).fetchone() == (0,)
        assert (
            db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?",
                (original.attempt_id,),
            ).fetchone()[0]
            == "unknown"
        )
    assert sends == ["original"]
