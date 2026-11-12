"""Controlled pre-send failure evidence; no provider network."""

import subprocess
import sys
from pathlib import Path

import pytest

from ecommerce_product_image_creator import faults, offline_submission

from .test_offline_submission import setup_context


def test_pre_send_failure_persists_evidence_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda _: calls.append(1)
    )

    def fail(_):
        raise ValueError("private detail")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", fail)
    result = offline_submission.submit_offline(service, *args)
    assert result.state == "unknown" and calls == []
    with service.store._connect() as db:
        row = db.execute(
            "SELECT operation_id,task_id,revision,execution_digest,phase,reason FROM attempt_transport_evidence WHERE attempt_id=?",
            (result.attempt_id,),
        ).fetchone()
    assert row == (
        "op",
        args[0].task_id,
        1,
        args[4].execution_digest,
        "pre_send_failure",
        "local_validation",
    )
    code = "import sqlite3,sys; d=sqlite3.connect(sys.argv[1]); print(d.execute('SELECT phase FROM attempt_transport_evidence WHERE attempt_id=?',(sys.argv[2],)).fetchone()[0])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path), result.attempt_id],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "pre_send_failure"


@pytest.mark.parametrize("error_type", [TimeoutError, ConnectionError])
def test_entered_transport_timeout_is_never_proven_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def timeout(_):
        raise error_type("private")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
    result = offline_submission.submit_offline(service, *args)
    assert result.state == "unknown"
    with service.store._connect() as db:
        row = db.execute(
            "SELECT phase FROM attempt_transport_evidence WHERE attempt_id=?",
            (result.attempt_id,),
        ).fetchone()
    assert row == ("entered",)


def test_proven_pre_send_failure_retries_only_with_independent_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import time
    from dataclasses import replace

    from ecommerce_product_image_creator import retry_scheduler
    from ecommerce_product_image_creator.send_evidence import verified_pre_send

    from .test_retry_scheduler import FakeClock

    service, args = setup_context(tmp_path)
    clock = FakeClock()
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: (
            calls.append(1)
            or offline_submission.ProviderResult("accepted", task_id="fixture-id")
        ),
    )

    def fail(_):
        raise faults.TransientLocalError("temporary local failure")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", fail)
    first = offline_submission.submit_offline(service, *args)
    assert first.state == "unknown" and calls == []
    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", lambda _: None)
    confirmation = replace(args[5], confirmation_id="pre-send-retry-confirmation")
    retry_args = (*args[:5], confirmation, "retry-pre-send", args[7])
    service.prepare(*retry_args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        db.execute(
            "CREATE TABLE fixture_presend_authorization (operation_id TEXT, confirmation_id TEXT)"
        )
        db.execute(
            "INSERT INTO fixture_presend_authorization VALUES(?,?)",
            ("op", confirmation.confirmation_id),
        )
        assert verified_pre_send(
            db, "op", first.attempt_id, args[0].task_id, 1, args[4].execution_digest
        )
        assert not verified_pre_send(
            db, "op", first.attempt_id, "other-task", 1, args[4].execution_digest
        )
        assert not verified_pre_send(
            db, "op", first.attempt_id, args[0].task_id, 1, "other-digest"
        )

    def evidence(db, root, attempt, snapshot, _confirmation):
        return verified_pre_send(
            db,
            root,
            attempt,
            snapshot.task_id,
            snapshot.revision,
            snapshot.execution_digest,
        )

    def authorization(db, root, _attempt, _snapshot, confirmation_id):
        return bool(
            db.execute(
                "SELECT 1 FROM fixture_presend_authorization WHERE operation_id=? AND confirmation_id=?",
                (root, confirmation_id),
            ).fetchone()
        )

    monkeypatch.setattr(offline_submission, "_verify_retry_evidence", evidence)
    monkeypatch.setattr(
        offline_submission, "_verify_retry_authorization", lambda *_: False
    )
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    scheduler = retry_scheduler.OfflineRetryScheduler(clock)
    assert scheduler.step(service, *retry_args, root="op").action == "stop"
    assert calls == []
    monkeypatch.setattr(
        offline_submission, "_verify_retry_authorization", authorization
    )
    assert scheduler.step(service, *retry_args, root="op").action == "waiting"
    assert calls == []
    clock.advance(4)
    assert scheduler.step(service, *retry_args, root="op").action == "waiting"
    assert calls == []
    clock.advance(1)
    result = scheduler.step(service, *retry_args, root="op")
    assert result.action == "submitted" and result.result is not None
    assert result.result.state == "accepted" and calls == [1]
    assert scheduler.step(service, *retry_args, root="op").action == "existing"
    assert calls == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT previous_attempt_id FROM operation_attempt WHERE operation_id='op' AND sequence=2"
        ).fetchone() == (first.attempt_id,)
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (first.attempt_id,),
        ).fetchone() == ("submission_uncertain",)


def test_missing_boundary_record_cannot_prove_pre_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ecommerce_product_image_creator.send_evidence import verified_pre_send

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def crash():
        raise RuntimeError("crash before boundary")

    monkeypatch.setattr(offline_submission, "_after_intent_recorded", crash)
    result = offline_submission.submit_offline(service, *args)
    assert result.state == "unknown"
    with service.store._connect() as db:
        assert (
            db.execute(
                "SELECT phase FROM attempt_transport_evidence WHERE attempt_id=?",
                (result.attempt_id,),
            ).fetchone()
            is None
        )
        assert not verified_pre_send(
            db, "op", result.attempt_id, args[0].task_id, 1, args[4].execution_digest
        )


def test_crash_after_boundary_is_conservatively_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ecommerce_product_image_creator.send_evidence import verified_pre_send

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def crash(_):
        raise SystemExit("fixture crash")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", crash)
    with pytest.raises(SystemExit):
        offline_submission.submit_offline(service, *args)
    with service.store._connect() as db:
        attempt = db.execute(
            "SELECT attempt_id FROM operation WHERE operation_id='op'"
        ).fetchone()[0]
        assert db.execute(
            "SELECT phase FROM attempt_transport_evidence WHERE attempt_id=?",
            (attempt,),
        ).fetchone() == ("entered",)
        assert not verified_pre_send(
            db, "op", attempt, args[0].task_id, 1, args[4].execution_digest
        )
    assert offline_submission.submit_offline(service, *args).state == "unknown"


def test_conflicting_remote_evidence_cannot_be_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ecommerce_product_image_creator.send_evidence import verified_pre_send

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def fail(_):
        raise ValueError("local")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", fail)
    result = offline_submission.submit_offline(service, *args)
    with service.store._connect() as db:
        assert verified_pre_send(
            db, "op", result.attempt_id, args[0].task_id, 1, args[4].execution_digest
        )
        db.execute(
            "UPDATE attempt SET upstream_status='failed',upstream_id='conflicting-id' WHERE attempt_id=?",
            (result.attempt_id,),
        )
        assert not verified_pre_send(
            db, "op", result.attempt_id, args[0].task_id, 1, args[4].execution_digest
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission._record_transport_boundary(
            service, "op", result.attempt_id, "entered"
        )


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_failure_never_creates_pre_send_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
):
    from ecommerce_product_image_creator import provider
    from ecommerce_product_image_creator.send_evidence import verified_pre_send

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: provider.parse_submission(status, {}),
    )
    result = offline_submission.submit_offline(service, *args)
    assert result.state == "unknown"
    with service.store._connect() as db:
        assert db.execute(
            "SELECT phase FROM attempt_transport_evidence WHERE attempt_id=?",
            (result.attempt_id,),
        ).fetchone() == ("entered",)
        assert not verified_pre_send(
            db, "op", result.attempt_id, args[0].task_id, 1, args[4].execution_digest
        )


def test_scheduler_rejects_conflicting_send_and_upstream_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from .test_retry_scheduler import configured_scheduler

    scheduler, _clock, service, args, first, calls = configured_scheduler(
        tmp_path, monkeypatch
    )
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt_transport_evidence SET phase='pre_send_failure',reason='local_validation' WHERE attempt_id=?",
            (first.attempt_id,),
        )
    result = scheduler.step(service, *args, root="op")
    assert result.action == "stop" and result.reason == "conflicting_transport_evidence"
    assert len(calls) == 1


def test_pre_send_proof_cannot_replace_missing_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import time
    from dataclasses import replace

    from ecommerce_product_image_creator import retry_scheduler

    from .test_retry_scheduler import FakeClock

    service, args = setup_context(tmp_path)
    clock = FakeClock()
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_retry_evidence", lambda *_: True)
    monkeypatch.setattr(
        offline_submission, "_verify_retry_authorization", lambda *_: True
    )
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda _: calls.append(1)
    )

    def fail(_):
        raise ValueError("fixture")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", fail)
    first = offline_submission.submit_offline(service, *args)
    confirmation = replace(args[5], confirmation_id="budget-confirmation")
    retry_args = (*args[:5], confirmation, "budget-request", args[7])
    service.prepare(*retry_args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        db.execute("DELETE FROM attempt_counter WHERE operation_id=?", ("op",))
    assert (
        retry_scheduler.OfflineRetryScheduler(clock)
        .step(service, *retry_args, root="op")
        .action
        == "stop"
    )
    assert first.state == "unknown" and calls == []


def test_legacy_database_adds_evidence_table_without_inventing_proof(tmp_path: Path):
    from ecommerce_product_image_creator.send_evidence import verified_pre_send
    from ecommerce_product_image_creator.storage import Store

    path = tmp_path / "legacy.sqlite"
    store = Store(path)
    store.create_task("legacy-task", "input", "request")
    store.create_revision("legacy-task", 1, "input")
    store.claim_operation("legacy-op", "legacy-task", 1, "digest", "legacy-attempt")
    with store._connect() as db:
        db.execute("DROP TABLE attempt_transport_evidence")
    reopened = Store(path)
    operation = reopened.get_operation("legacy-op")
    assert operation is not None
    assert operation.upstream_status == "submission_uncertain"
    with reopened._connect() as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='attempt_transport_evidence'"
        ).fetchone() == ("attempt_transport_evidence",)
        assert not verified_pre_send(
            db, "legacy-op", "legacy-attempt", "legacy-task", 1, "digest"
        )


def test_second_attempt_presend_proof_is_bound_to_root_and_allows_third(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import time
    from dataclasses import replace

    from ecommerce_product_image_creator import retry_scheduler
    from ecommerce_product_image_creator.send_evidence import verified_pre_send

    from .test_attempt002 import prepare_retry
    from .test_retry_scheduler import FakeClock

    service, args, first, calls = prepare_retry(tmp_path, monkeypatch)
    clock = FakeClock()
    monkeypatch.setattr(offline_submission, "_clock_now", clock)

    def fail(_):
        raise faults.TransientLocalError("temporary local failure")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", fail)
    second = offline_submission.submit_offline(service, *args, retry_of="op")
    assert second.state == "unknown" and len(calls) == 1
    with service.store._connect() as db:
        assert verified_pre_send(
            db, "op", second.attempt_id, args[0].task_id, 1, args[4].execution_digest
        )
        assert not verified_pre_send(
            db,
            "retry-request",
            second.attempt_id,
            args[0].task_id,
            1,
            args[4].execution_digest,
        )
    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", lambda _: None)
    confirmation = replace(args[5], confirmation_id="third-presend-confirmation")
    third_args = (*args[:5], confirmation, "third-presend-request", args[7])
    service.prepare(*third_args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        for table in ("fixture_retry_evidence", "fixture_retry_authorization"):
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?)",
                ("op", second.attempt_id, confirmation.confirmation_id),
            )
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    scheduler = retry_scheduler.OfflineRetryScheduler(clock)
    assert scheduler.step(service, *third_args, root="op").action == "waiting"
    clock.advance(5)
    third = scheduler.step(service, *third_args, root="op")
    assert third.action == "submitted" and third.result is not None and len(calls) == 2
    with service.store._connect() as db:
        assert db.execute(
            "SELECT previous_attempt_id FROM operation_attempt WHERE operation_id=? AND sequence=3",
            ("op",),
        ).fetchone() == (second.attempt_id,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id=?", ("op",)
        ).fetchone() == (3,)
    assert first.attempt_id != second.attempt_id
