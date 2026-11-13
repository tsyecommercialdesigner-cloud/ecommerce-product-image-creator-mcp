import dataclasses
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ecommerce_product_image_creator import offline_submission
from ecommerce_product_image_creator.execution_candidates import register_root_candidate

from .test_offline_submission import setup_context


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def test_risk_reservation_is_inert_and_scoped(tmp_path: Path, monkeypatch):
    from ecommerce_product_image_creator import risk_reservation

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    sends = []

    def timeout(_):
        sends.append(1)
        raise TimeoutError()

    monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
    first = offline_submission.submit_offline(service, *args)
    candidate = register_root_candidate(service.store, "op", "a")
    evidence = risk_reservation.RiskEvidence(
        authorization_id="risk-1",
        source_root="op",
        source_candidate="a",
        source_attempt=first.attempt_id,
        source_digest=candidate.snapshot.execution_digest,
        target_operation="linked-1",
        snapshot_payload=candidate.snapshot.to_payload(),
        risk_notice_reference="fixture-notice",
        risk_acceptance_reference="fixture-acceptance",
        execution_authorization_reference="fixture-execution",
        expires_at=time.time() + 300,
    )
    with pytest.raises(ValueError, match="source_unverified"):
        risk_reservation.reserve(service.store, evidence)
    from ecommerce_product_image_creator.risk_reservation import RiskDisclosure

    disclosure = RiskDisclosure(
        source_root="op",
        source_candidate="a",
        source_attempt=first.attempt_id,
        source_digest=candidate.snapshot.execution_digest,
        target_operation="linked-1",
        snapshot_digest=candidate.snapshot.execution_digest,
        upstream_status="unknown",
        may_have_sent=True,
        duplicate_generation=True,
        duplicate_cost=True,
        original_cost="unknown",
        allowed_attempts=1,
    )
    evidence = dataclasses.replace(evidence, disclosure=disclosure)
    monkeypatch.setattr(
        risk_reservation,
        "_verify_risk_acceptance",
        lambda db, e: e.risk_acceptance_reference == "fixture-acceptance",
    )
    with pytest.raises(ValueError, match="source_unverified"):
        risk_reservation.reserve(service.store, evidence)
    monkeypatch.setattr(
        risk_reservation,
        "_verify_execution_authorization",
        lambda db, e: e.execution_authorization_reference == "fixture-execution",
    )
    with pytest.raises(ValueError, match="risk_disclosure_mismatch"):
        risk_reservation.reserve(
            service.store,
            dataclasses.replace(
                evidence,
                disclosure=dataclasses.replace(disclosure, duplicate_cost=False),
            ),
        )
    for changed in (
        dataclasses.replace(disclosure, allowed_attempts=2),
        dataclasses.replace(disclosure, source_attempt="other"),
        dataclasses.replace(disclosure, snapshot_digest="other"),
    ):
        with pytest.raises(ValueError, match="risk_disclosure_mismatch"):
            risk_reservation.reserve(
                service.store, dataclasses.replace(evidence, disclosure=changed)
            )
    with service.store._connect() as db:
        before = tuple(
            db.execute("SELECT count(*) FROM " + t).fetchone()[0]
            for t in ("attempt", "operation", "attempt_counter", "local_eligibility")
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: risk_reservation.reserve(service.store, evidence), range(2)
            )
        )
    assert results[0] == results[1]
    result = results[0]
    assert result.operation_id == "linked-1" and result.can_send is False
    assert risk_reservation.reserve(service.store, evidence) == result
    with pytest.raises(ValueError, match="authorization_conflict"):
        risk_reservation.reserve(
            service.store,
            dataclasses.replace(
                evidence,
                target_operation="linked-2",
                disclosure=dataclasses.replace(disclosure, target_operation="linked-2"),
            ),
        )
    with pytest.raises(ValueError, match="reservation_conflict"):
        risk_reservation.reserve(
            service.store,
            dataclasses.replace(
                evidence,
                authorization_id="risk-2",
                target_operation="linked-2",
                disclosure=dataclasses.replace(disclosure, target_operation="linked-2"),
            ),
        )
    with pytest.raises(ValueError, match="invalid_or_expired_evidence"):
        risk_reservation.reserve(
            service.store, dataclasses.replace(evidence, expires_at=1)
        )
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_id=? WHERE attempt_id=?",
            ("queryable", first.attempt_id),
        )
    with pytest.raises(ValueError, match="query_original"):
        risk_reservation.reserve(service.store, evidence)
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_id=NULL WHERE attempt_id=?",
            (first.attempt_id,),
        )
    for status in ("running", "succeeded", "failed"):
        with service.store._connect() as db:
            db.execute(
                "UPDATE attempt SET upstream_status=? WHERE attempt_id=?",
                (status, first.attempt_id),
            )
        expected = {
            "running": "original_processing",
            "succeeded": "original_succeeded",
            "failed": "original_state_unverified",
        }[status]
        with pytest.raises(ValueError, match=expected):
            risk_reservation.reserve(service.store, evidence)
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='unknown' WHERE attempt_id=?",
            (first.attempt_id,),
        )
    with pytest.raises(ValueError, match="source_unverified"):
        risk_reservation.revoke(service.store, "risk-1", "fixture-revocation")
    monkeypatch.setattr(
        risk_reservation,
        "_verify_revocation_source",
        lambda _db, auth, reference: (
            auth == "risk-1" and reference == "fixture-revocation"
        ),
    )
    entered = threading.Event()
    release = threading.Event()

    def pause_in_write_transaction(_db, e):
        entered.set()
        assert release.wait(timeout=10)
        return e.risk_acceptance_reference == "fixture-acceptance"

    monkeypatch.setattr(
        risk_reservation, "_verify_risk_acceptance", pause_in_write_transaction
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        reservation = pool.submit(risk_reservation.reserve, service.store, evidence)
        assert entered.wait(timeout=10)
        revocation = pool.submit(
            risk_reservation.revoke, service.store, "risk-1", "fixture-revocation"
        )
        release.set()
        assert reservation.result(timeout=15).can_send is False
        revocation.result(timeout=15)
    monkeypatch.setattr(
        risk_reservation,
        "_verify_risk_acceptance",
        lambda _db, e: e.risk_acceptance_reference == "fixture-acceptance",
    )
    risk_reservation.revoke(service.store, "risk-1", "fixture-revocation")
    with pytest.raises(ValueError, match="authorization_revoked"):
        risk_reservation.reserve(service.store, evidence)
    with service.store._connect() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM risk_reservation")
    code = "import sqlite3,sys; d=sqlite3.connect(sys.argv[1]); print(d.execute('SELECT count(*) FROM risk_reservation').fetchone()[0],d.execute('SELECT count(*) FROM risk_reservation_revocation').fetchone()[0])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "1 1"
    with service.store._connect() as db:
        assert (
            tuple(
                db.execute("SELECT count(*) FROM " + t).fetchone()[0]
                for t in (
                    "attempt",
                    "operation",
                    "attempt_counter",
                    "local_eligibility",
                )
            )
            == before
        )
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (first.attempt_id,),
        ).fetchone()[0] in ("unknown", "submission_uncertain")
    assert sends == [1]


def test_competing_reservations_and_rollback(tmp_path: Path, monkeypatch):
    from ecommerce_product_image_creator import risk_reservation

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def timeout(_):
        raise TimeoutError()

    monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
    first = offline_submission.submit_offline(service, *args)
    candidate = register_root_candidate(service.store, "op", "a")
    evidence = risk_reservation.RiskEvidence(
        authorization_id="risk-a",
        source_root="op",
        source_candidate="a",
        source_attempt=first.attempt_id,
        source_digest=candidate.snapshot.execution_digest,
        target_operation="linked-a",
        snapshot_payload=candidate.snapshot.to_payload(),
        risk_notice_reference="notice",
        risk_acceptance_reference="acceptance",
        execution_authorization_reference="execution",
        expires_at=time.time() + 300,
    )
    evidence = dataclasses.replace(
        evidence,
        disclosure=risk_reservation.RiskDisclosure(
            source_root="op",
            source_candidate="a",
            source_attempt=first.attempt_id,
            source_digest=candidate.snapshot.execution_digest,
            target_operation="linked-a",
            snapshot_digest=candidate.snapshot.execution_digest,
            upstream_status="unknown",
            may_have_sent=True,
            duplicate_generation=True,
            duplicate_cost=True,
            original_cost="unknown",
            allowed_attempts=1,
        ),
    )
    monkeypatch.setattr(
        risk_reservation,
        "_verify_risk_acceptance",
        lambda _db, e: (
            e.risk_notice_reference == "notice"
            and e.risk_acceptance_reference == "acceptance"
        ),
    )
    monkeypatch.setattr(
        risk_reservation,
        "_verify_execution_authorization",
        lambda _db, e: e.execution_authorization_reference == "execution",
    )
    with service.store._connect() as db:
        risk_reservation._schema(db)
        db.execute(
            "CREATE TRIGGER inject_reservation_failure BEFORE INSERT ON risk_reservation WHEN NEW.authorization_id='failure' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        risk_reservation.reserve(
            service.store, dataclasses.replace(evidence, authorization_id="failure")
        )
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM risk_reservation").fetchone() == (0,)
    assert evidence.disclosure is not None
    competing = [
        evidence,
        dataclasses.replace(
            evidence,
            authorization_id="risk-b",
            target_operation="linked-b",
            disclosure=dataclasses.replace(
                evidence.disclosure, target_operation="linked-b"
            ),
        ),
    ]

    def run(item):
        try:
            return risk_reservation.reserve(service.store, item).operation_id
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, competing))
    assert sum(value in {"linked-a", "linked-b"} for value in outcomes) == 1
    assert outcomes.count("reservation_conflict") == 1
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM risk_reservation").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert db.execute("SELECT sum(count) FROM attempt_counter").fetchone() == (1,)


def test_pre_risk_schema_upgrade_preserves_history(tmp_path: Path, monkeypatch):
    from ecommerce_product_image_creator import risk_reservation
    from ecommerce_product_image_creator.storage import Store

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def timeout(_):
        raise TimeoutError()

    monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
    first = offline_submission.submit_offline(service, *args)
    register_root_candidate(service.store, "op", "a")
    path = tmp_path / "legacy.sqlite"
    with service.store._connect() as source, sqlite3.connect(path) as destination:
        assert (
            source.execute(
                "SELECT 1 FROM sqlite_master WHERE name='risk_reservation'"
            ).fetchone()
            is None
        )
        source.backup(destination)
    legacy = Store(path)
    names = (
        "task",
        "revision",
        "attempt",
        "operation",
        "operation_attempt",
        "attempt_counter",
        "execution_candidate",
        "artifact",
        "recovery_observation",
    )
    with legacy._connect() as db:
        before = {
            name: db.execute("SELECT * FROM " + name).fetchall() for name in names
        }
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (first.attempt_id,),
        ).fetchone()[0] in ("unknown", "submission_uncertain")
        risk_reservation._schema(db)
        risk_reservation._schema(db)
        assert {
            name: db.execute("SELECT * FROM " + name).fetchall() for name in names
        } == before
        assert db.execute("SELECT count(*) FROM risk_reservation").fetchone() == (0,)


def test_mock_source_review_is_rechecked(tmp_path: Path, monkeypatch):
    from ecommerce_product_image_creator import preparation, risk_reservation
    from ecommerce_product_image_creator.domain import ExecutionSnapshot
    from ecommerce_product_image_creator.preparation import read_prepared
    from ecommerce_product_image_creator.tasks import EligibilityError

    from .test_mock_workflow import mock_fixture

    store, _, form, mock, _, _ = mock_fixture(tmp_path, monkeypatch)
    snap = ExecutionSnapshot.from_payload(
        read_prepared(store, form["task_id"], mock["revision"])["snapshot_payload"]
    )
    with store._connect() as db:
        db.execute("UPDATE task SET status='ready' WHERE task_id=?", (form["task_id"],))
        db.execute(
            "INSERT INTO operation_attempt VALUES(?,?,?,?,?,?,?)",
            (
                "mock-op",
                1,
                "mock-attempt",
                None,
                snap.model,
                snap.endpoint,
                snap.execution_digest,
            ),
        )
        db.execute(
            "INSERT INTO attempt_counter VALUES(?,?,?,?)",
            ("mock-op", snap.model, snap.endpoint, 1),
        )
        db.execute(
            "INSERT INTO attempt_transport_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "mock-attempt",
                "mock-op",
                "mock-op",
                form["task_id"],
                mock["revision"],
                snap.execution_digest,
                "entered",
                "dispatch_boundary",
                "server_pre_dispatch",
                time.time(),
            ),
        )
    candidate = register_root_candidate(store, "mock-op", "mock-candidate")
    disclosure = risk_reservation.RiskDisclosure(
        "mock-op",
        "mock-candidate",
        "mock-attempt",
        snap.execution_digest,
        "mock-reservation",
        snap.execution_digest,
        "unknown",
        True,
        True,
        True,
        "unknown",
        1,
    )
    evidence = risk_reservation.RiskEvidence(
        "risk-mock",
        "mock-op",
        "mock-candidate",
        "mock-attempt",
        snap.execution_digest,
        "mock-reservation",
        candidate.snapshot.to_payload(),
        "notice",
        "acceptance",
        "execution",
        time.time() + 300,
        disclosure,
    )
    monkeypatch.setattr(risk_reservation, "_verify_risk_acceptance", lambda *_: True)
    monkeypatch.setattr(
        risk_reservation, "_verify_execution_authorization", lambda *_: True
    )
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: False)
    with pytest.raises(EligibilityError, match="review"):
        risk_reservation.reserve(store, evidence)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    assert risk_reservation.reserve(store, evidence).can_send is False
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='mock-op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT decision FROM form_review_detail WHERE review_id='form-review'"
        ).fetchone() == ("accept",)
