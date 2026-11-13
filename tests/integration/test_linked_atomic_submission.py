import dataclasses
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

from ecommerce_product_image_creator import offline_submission, risk_reservation
from ecommerce_product_image_creator.execution_candidates import register_root_candidate

from .test_offline_submission import setup_context

_DEFAULT_FEE_VERIFIER = offline_submission._verify_linked_fee


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def linked_context(tmp_path, monkeypatch):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []

    def timeout(_request):
        calls.append(1)
        raise TimeoutError()

    monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
    original = offline_submission.submit_offline(service, *args)
    candidate = register_root_candidate(service.store, "op", "source-candidate")
    digest = candidate.snapshot.execution_digest
    disclosure = risk_reservation.RiskDisclosure(
        "op",
        "source-candidate",
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
        "risk-1",
        "op",
        "source-candidate",
        original.attempt_id,
        digest,
        "linked-op",
        candidate.snapshot.to_payload(),
        "notice",
        "acceptance",
        "execution",
        time.time() + 300,
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
    risk_reservation.reserve(service.store, evidence)
    monkeypatch.setattr(
        offline_submission,
        "_verify_linked_fee",
        lambda _db, e, snap, confirm: (
            e.target_operation == "linked-op"
            and e.snapshot_payload == snap.to_payload()
            and confirm == "linked-confirmation"
        ),
    )
    confirmation = dataclasses.replace(args[5], confirmation_id="linked-confirmation")
    linked = (*args[:5], confirmation, "linked-op", args[7])
    service.prepare(*linked, expires_at=time.time() + 120)
    return service, linked, original, calls


def test_linked_submission_consumes_reservation_atomically(linked_context):
    service, raw_linked, original, calls = linked_context
    linked = cast(Any, raw_linked)
    result = offline_submission.submit_offline(
        service, *linked, risk_authorization_id="risk-1"
    )
    assert result.attempt_id != original.attempt_id
    assert len(calls) == 2
    with service.store._connect() as db:
        consumption = db.execute(
            "SELECT source_root,source_attempt,linked_operation,linked_attempt,linked_candidate,execution_digest FROM risk_consumption"
        ).fetchone()
        assert consumption[:4] == (
            "op",
            original.attempt_id,
            "linked-op",
            result.attempt_id,
        )
        assert db.execute(
            "SELECT root_operation_id,execution_digest FROM execution_candidate WHERE candidate_id=?",
            (consumption[4],),
        ).fetchone() == ("linked-op", consumption[5])
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='linked-op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='linked-op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (original.attempt_id,),
        ).fetchone() == ("unknown",)
    assert (
        offline_submission.submit_offline(
            service, *linked, risk_authorization_id="risk-1"
        ).attempt_id
        == result.attempt_id
    )
    assert len(calls) == 2
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('select count(*) from risk_consumption').fetchone()[0])",
            str(service.store.path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "1"


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "risk_unverified",
        "fee_unverified",
        "revoked",
        "expired",
        "business_unverified",
        "original_changed",
        "scope_mismatch",
        "rollback",
    ],
)
def test_linked_rejections_do_not_send(linked_context, monkeypatch, failure):
    service, linked, original, calls = linked_context
    auth = "risk-1"
    if failure == "missing":
        auth = "missing"
    elif failure == "risk_unverified":
        monkeypatch.setattr(
            risk_reservation, "_verify_risk_acceptance", lambda *_: False
        )
    elif failure == "fee_unverified":
        monkeypatch.setattr(offline_submission, "_verify_linked_fee", lambda *_: False)
    elif failure == "expired":
        monkeypatch.setattr(risk_reservation.time, "time", lambda: 99999999999.0)
    elif failure == "business_unverified":
        monkeypatch.setattr(
            offline_submission, "_verify_business_choices", lambda *_: False
        )
    elif failure == "revoked":
        monkeypatch.setattr(
            risk_reservation, "_verify_revocation_source", lambda *_: True
        )
        risk_reservation.revoke(service.store, auth, "fixture")
    elif failure == "original_changed":
        with service.store._connect() as db:
            db.execute(
                "UPDATE attempt SET upstream_status='succeeded' WHERE attempt_id=?",
                (original.attempt_id,),
            )
    elif failure == "scope_mismatch":
        linked = cast(Any, (*linked[:6], "another-operation", linked[7]))
    else:
        with service.store._connect() as db:
            db.execute(
                "CREATE TRIGGER fail_linked_consumption BEFORE INSERT ON risk_consumption BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *linked, risk_authorization_id=auth)
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM risk_consumption").fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='linked-op'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation WHERE operation_id='linked-op'"
        ).fetchone() == (0,)
    assert calls == [1]


def test_revocation_after_intent_prevents_fake_dispatch(linked_context, monkeypatch):
    service, linked, _, calls = linked_context
    monkeypatch.setattr(risk_reservation, "_verify_revocation_source", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_after_intent_recorded",
        lambda: risk_reservation.revoke(service.store, "risk-1", "revoked"),
    )
    result = offline_submission.submit_offline(
        service, *linked, risk_authorization_id="risk-1"
    )
    assert result.state == "unknown"
    assert calls == [1]
    from ecommerce_product_image_creator import send_evidence

    with service.store._connect() as db:
        assert (
            send_evidence.verified_linked_stop(
                db,
                "linked-op",
                result.attempt_id,
                linked[0].task_id,
                1,
                linked[4].execution_digest,
            )
            == "entered_then_stopped"
        )


def test_reservation_alone_cannot_bypass_same_digest(linked_context):
    service, linked, _, calls = linked_context
    with pytest.raises(offline_submission.SubmissionError, match="already has"):
        offline_submission.submit_offline(service, *linked)
    assert calls == [1]


def test_linked_root_cannot_be_source_for_another_reservation(linked_context):
    service, linked, _, calls = linked_context
    result = offline_submission.submit_offline(
        service, *linked, risk_authorization_id="risk-1"
    )
    with service.store._connect() as db:
        candidate_id = db.execute(
            "SELECT linked_candidate FROM risk_consumption WHERE authorization_id='risk-1'"
        ).fetchone()[0]
    snapshot = linked[4]
    digest = snapshot.execution_digest
    disclosure = risk_reservation.RiskDisclosure(
        "linked-op",
        candidate_id,
        result.attempt_id,
        digest,
        "nested-op",
        digest,
        "unknown",
        True,
        True,
        True,
        "unknown",
        1,
    )
    evidence = risk_reservation.RiskEvidence(
        "nested",
        "linked-op",
        candidate_id,
        result.attempt_id,
        digest,
        "nested-op",
        snapshot.to_payload(),
        "notice",
        "acceptance",
        "execution",
        time.time() + 300,
        disclosure,
    )
    with pytest.raises(ValueError, match="risk_lineage_unverified"):
        risk_reservation.reserve(service.store, evidence)
    assert len(calls) == 2


def test_production_fee_verifier_is_fail_closed(linked_context, monkeypatch):
    service, linked, _, calls = linked_context
    monkeypatch.setattr(offline_submission, "_verify_linked_fee", _DEFAULT_FEE_VERIFIER)
    with pytest.raises(offline_submission.SubmissionError, match="fee authorization"):
        offline_submission.submit_offline(
            service, *linked, risk_authorization_id="risk-1"
        )
    assert calls == [1]


def test_parallel_replay_and_one_attempt_limit(linked_context):
    from ecommerce_product_image_creator.retry_scheduler import OfflineRetryScheduler

    service, raw_linked, _, calls = linked_context
    linked = cast(Any, raw_linked)

    def run(_):
        return offline_submission.submit_offline(
            service, *linked, risk_authorization_id="risk-1"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results[0].attempt_id == results[1].attempt_id
    assert len(calls) == 2
    alternate = cast(Any, (*linked[:6], "retry-linked", linked[7]))
    with pytest.raises(offline_submission.SubmissionError, match="only one attempt"):
        offline_submission.submit_offline(service, *alternate, retry_of="linked-op")
    scheduler = OfflineRetryScheduler(time.time)
    result = scheduler.step(service, *alternate, root="linked-op")
    assert (result.action, result.reason) == ("stop", "linked_risk_one_attempt_limit")
    candidate_result = scheduler.step(
        service,
        *alternate,
        root="linked-op",
        candidate_id="another-candidate",
        evaluate_only=True,
    )
    assert (candidate_result.action, candidate_result.reason) == (
        "stop",
        "linked_risk_one_attempt_limit",
    )
    assert len(calls) == 2
