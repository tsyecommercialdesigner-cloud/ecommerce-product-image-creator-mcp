"""Unknown alternate candidate and independently authorized linked execution."""

import socket
import time
from dataclasses import replace

import pytest

from ecommerce_product_image_creator import offline_submission, risk_reservation

from .test_cross_candidate_submission import setup_cross


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def blocked(*_a, **_kw):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def setup_alternate_unknown(tmp_path, monkeypatch):
    service, args, _, _, _, candidate, calls = setup_cross(tmp_path, monkeypatch)

    def timeout(request):
        calls.append(request)
        raise TimeoutError("isolated provider")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
    source = offline_submission.submit_offline(service, *args, retry_of="op")
    assert source.state == "unknown" and len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
            (source.attempt_id,),
        ).fetchone() == ("unknown", None)
        assert db.execute(
            "SELECT candidate_id,root_operation_id FROM candidate_attempt WHERE attempt_id=?",
            (source.attempt_id,),
        ).fetchone() == ("b", "op")
        assert (
            db.execute(
                "SELECT execution_digest FROM prepared_execution WHERE task_id=? AND revision=?",
                (args[0].task_id, args[1].revision),
            ).fetchone()[0]
            != candidate.snapshot.execution_digest
        )
    digest = candidate.snapshot.execution_digest
    disclosure = risk_reservation.RiskDisclosure(
        "op",
        "b",
        source.attempt_id,
        digest,
        "linked-b",
        digest,
        "unknown",
        True,
        True,
        True,
        "unknown",
        1,
    )
    evidence = risk_reservation.RiskEvidence(
        "risk-b",
        "op",
        "b",
        source.attempt_id,
        digest,
        "linked-b",
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
    monkeypatch.setattr(
        offline_submission,
        "_verify_linked_fee",
        lambda _db, e, s, c: (
            e.authorization_id == "risk-b"
            and s.execution_digest == digest
            and c == "linked-confirm"
        ),
    )
    assert risk_reservation.reserve(service.store, evidence).can_send is False
    confirmation = replace(args[5], confirmation_id="linked-confirm")
    linked = (*args[:5], confirmation, "linked-b", args[7])
    return service, linked, source, candidate, calls


def test_alternate_unknown_can_create_one_linked_attempt(tmp_path, monkeypatch):
    service, linked, source, candidate, calls = setup_alternate_unknown(
        tmp_path, monkeypatch
    )
    service.prepare(*linked, expires_at=time.time() + 120, candidate_id="b")
    with service.store._connect() as db:
        original_counts = db.execute(
            "SELECT model,endpoint,count FROM attempt_counter WHERE operation_id='op' ORDER BY model,endpoint"
        ).fetchall()
        original_chain = db.execute(
            "SELECT * FROM operation_attempt WHERE operation_id='op' ORDER BY sequence"
        ).fetchall()
    result = offline_submission.submit_offline(
        service, *linked, risk_authorization_id="risk-b"
    )
    assert result.attempt_id != source.attempt_id and result.state == "unknown"
    assert len(calls) == 2
    with service.store._connect() as db:
        row = db.execute(
            "SELECT source_root,source_candidate,source_attempt,source_digest,linked_operation,linked_attempt,linked_candidate,execution_digest FROM risk_consumption WHERE authorization_id='risk-b'"
        ).fetchone()
        assert row[:6] == (
            "op",
            "b",
            source.attempt_id,
            candidate.snapshot.execution_digest,
            "linked-b",
            result.attempt_id,
        )
        assert (
            row[6] not in ("a", "b") and row[7] == candidate.snapshot.execution_digest
        )
    replay = offline_submission.submit_offline(
        service, *linked, risk_authorization_id="risk-b"
    )
    assert replay.attempt_id == result.attempt_id and len(calls) == 2
    with pytest.raises(offline_submission.SubmissionError, match="only one attempt"):
        offline_submission.submit_offline(service, *linked, retry_of="linked-b")
    other = (
        *linked[:5],
        replace(linked[5], confirmation_id="other-confirm"),
        "other-id",
        linked[7],
    )
    service.prepare(*other, expires_at=time.time() + 120, candidate_id="b")
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *other)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *other, retry_of="op")
    assert len(calls) == 2
    from ecommerce_product_image_creator import tasks

    source_view = tasks.recover_task_once(
        service.store,
        "op",
        "b",
        linked[0].task_id,
        1,
        linked[4].stage,
        source.attempt_id,
        linked[4].execution_digest,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    target_view = tasks.recover_task_once(
        service.store,
        "linked-b",
        row[6],
        linked[0].task_id,
        1,
        linked[4].stage,
        result.attempt_id,
        linked[4].execution_digest,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert source_view["linked_evidence"][0]["linked_candidate"] == row[6]
    assert target_view["linked_source"]["attempt_id"] == source.attempt_id
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
            (source.attempt_id,),
        ).fetchone() == ("unknown", None)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='linked-b'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op' AND model='gpt-image-2.5-flare'"
        ).fetchone() == (1,)
        assert (
            db.execute(
                "SELECT model,endpoint,count FROM attempt_counter WHERE operation_id='op' ORDER BY model,endpoint"
            ).fetchall()
            == original_counts
        )
        assert (
            db.execute(
                "SELECT * FROM operation_attempt WHERE operation_id='op' ORDER BY sequence"
            ).fetchall()
            == original_chain
        )


@pytest.mark.parametrize(
    "failure",
    ["fee", "capability", "business", "candidate", "risk_retry", "production"],
)
def test_alternate_risk_denials_never_dispatch(tmp_path, monkeypatch, failure):
    from ecommerce_product_image_creator import candidate_preflight

    service, linked, _source, _candidate, calls = setup_alternate_unknown(
        tmp_path, monkeypatch
    )
    service.prepare(*linked, expires_at=time.time() + 120, candidate_id="b")
    if failure == "fee":
        monkeypatch.setattr(offline_submission, "_verify_linked_fee", lambda *_: False)
    elif failure == "capability":
        monkeypatch.setattr(
            candidate_preflight, "_verify_candidate_capability", lambda *_: False
        )
    elif failure == "business":
        monkeypatch.setattr(candidate_preflight, "_verify_business", lambda *_: False)
    elif failure == "candidate":
        with service.store._connect() as db:
            db.execute(
                "UPDATE candidate_eligibility SET candidate_id='a' WHERE operation_id='linked-b'"
            )
    elif failure == "production":
        monkeypatch.setattr(
            risk_reservation, "_verify_execution_authorization", lambda *_: False
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(
            service,
            *linked,
            risk_authorization_id="risk-b",
            retry_of="op" if failure == "risk_retry" else None,
        )
    assert len(calls) == 1
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM risk_consumption").fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='linked-b'"
        ).fetchone() == (0,)


def test_alternate_risk_transaction_rolls_back(tmp_path, monkeypatch):
    service, linked, _, _, calls = setup_alternate_unknown(tmp_path, monkeypatch)
    service.prepare(*linked, expires_at=time.time() + 120, candidate_id="b")
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER abort_risk BEFORE INSERT ON risk_consumption BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(
            service, *linked, risk_authorization_id="risk-b"
        )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM operation WHERE operation_id='linked-b'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='linked-b'"
        ).fetchone() == (0,)
    assert len(calls) == 1


def test_alternate_risk_concurrent_replay_and_restart(tmp_path, monkeypatch):
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor

    service, linked, source, candidate, calls = setup_alternate_unknown(
        tmp_path, monkeypatch
    )
    service.prepare(*linked, expires_at=time.time() + 120, candidate_id="b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: offline_submission.submit_offline(
                    service, *linked, risk_authorization_id="risk-b"
                ),
                range(2),
            )
        )
    assert results[0].attempt_id == results[1].attempt_id and len(calls) == 2
    with service.store._connect() as db:
        linked_candidate = db.execute(
            "SELECT linked_candidate FROM risk_consumption WHERE authorization_id='risk-b'"
        ).fetchone()[0]
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE operation_id='linked-b'"
        ).fetchone() == (1,)
    code = "import sys; from pathlib import Path; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.execution_candidates import read_candidate; c=read_candidate(Store(Path(sys.argv[1])),sys.argv[2]); print(c.root_operation_id,c.snapshot.execution_digest)"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path), linked_candidate],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == f"linked-b {candidate.snapshot.execution_digest}"
    assert source.attempt_id != results[0].attempt_id


def test_alternate_risk_source_attempt_candidate_mismatch_is_rejected(
    tmp_path, monkeypatch
):
    service, linked, source, _, calls = setup_alternate_unknown(tmp_path, monkeypatch)
    service.prepare(*linked, expires_at=time.time() + 120, candidate_id="b")
    with service.store._connect() as db:
        db.execute(
            "UPDATE candidate_attempt SET candidate_id='a' WHERE attempt_id=?",
            (source.attempt_id,),
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(
            service, *linked, risk_authorization_id="risk-b"
        )
    assert len(calls) == 1
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM risk_consumption").fetchone() == (0,)


def test_linked_candidate_recovery_rejects_changed_source_association(
    tmp_path, monkeypatch
):
    from ecommerce_product_image_creator.execution_candidates import (
        CandidateError,
        read_candidate,
    )

    service, linked, source, _, _calls = setup_alternate_unknown(tmp_path, monkeypatch)
    service.prepare(*linked, expires_at=time.time() + 120, candidate_id="b")
    offline_submission.submit_offline(service, *linked, risk_authorization_id="risk-b")
    with service.store._connect() as db:
        candidate_id = db.execute(
            "SELECT linked_candidate FROM risk_consumption WHERE authorization_id='risk-b'"
        ).fetchone()[0]
        db.execute(
            "UPDATE candidate_attempt SET candidate_id='a' WHERE attempt_id=?",
            (source.attempt_id,),
        )
    with pytest.raises(CandidateError):
        read_candidate(service.store, candidate_id)
