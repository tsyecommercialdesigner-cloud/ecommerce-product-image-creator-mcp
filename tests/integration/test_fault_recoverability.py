"""Controlled local failure evidence is distinct from side-effect safety."""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator import faults, offline_submission, retry_scheduler
from ecommerce_product_image_creator.send_evidence import (
    verified_pre_send,
    verified_recoverability,
)

from .test_offline_submission import setup_context
from .test_retry_scheduler import FakeClock


@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (faults.DeterministicInputError, "deterministic_input"),
        (faults.DeterministicConfigurationError, "deterministic_configuration"),
        (faults.AuthorizationGateError, "authorization_gate"),
        (ValueError, "unknown"),
    ],
)
def test_pre_send_nonrecoverable_fault_never_waits_or_sends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    category: str,
):
    service, args = setup_context(tmp_path)
    clock = FakeClock()
    calls: list[int] = []
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda _: calls.append(1)
    )

    def fail(_):
        raise error_type("fixture, not used for classification")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", fail)
    first = offline_submission.submit_offline(service, *args)
    confirmation = replace(args[5], confirmation_id="new-authorization")
    retry = (*args[:5], confirmation, "new-operation", args[7])
    service.prepare(*retry, expires_at=clock() + 120)
    monkeypatch.setattr(offline_submission, "_verify_retry_evidence", lambda *_: True)
    monkeypatch.setattr(
        offline_submission, "_verify_retry_authorization", lambda *_: True
    )
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    result = retry_scheduler.OfflineRetryScheduler(clock).step(
        service, *retry, root="op"
    )
    assert result.action == "stop" and calls == []
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *retry, retry_of="op")
    assert calls == []
    with service.store._connect() as db:
        values = ("op", first.attempt_id, args[0].task_id, 1, args[4].execution_digest)
        assert verified_pre_send(db, *values)
        assert verified_recoverability(db, *values) == category
        assert db.execute("SELECT count(*) FROM retry_wait").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert (
            verified_recoverability(
                db, "op", first.attempt_id, "other", 1, args[4].execution_digest
            )
            == "unknown"
        )
        assert (
            verified_recoverability(
                db, "op", "other", args[0].task_id, 1, args[4].execution_digest
            )
            == "unknown"
        )


def test_missing_and_conflicting_fault_evidence_fail_closed_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def fail(_):
        raise faults.TransientLocalError("fixture")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", fail)
    first = offline_submission.submit_offline(service, *args)
    values = ("op", first.attempt_id, args[0].task_id, 1, args[4].execution_digest)
    code = (
        "import sqlite3,sys; from ecommerce_product_image_creator.send_evidence import verified_recoverability; "
        "db=sqlite3.connect(sys.argv[1]); print(verified_recoverability(db,sys.argv[2],sys.argv[3],sys.argv[4],int(sys.argv[5]),sys.argv[6]))"
    )
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path), *map(str, values)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "transient_local"
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt_fault_evidence SET execution_digest='conflict' WHERE attempt_id=?",
            (first.attempt_id,),
        )
        assert verified_recoverability(db, *values) == "unknown"
        db.execute(
            "DELETE FROM attempt_fault_evidence WHERE attempt_id=?", (first.attempt_id,)
        )
        assert verified_pre_send(db, *values)
        assert verified_recoverability(db, *values) == "unknown"


def test_advisory_candidate_needs_independent_recoverability():
    classification = faults.classify_transport("not_sent", evidence="local_guard")
    facts = faults.RetryFacts(
        "op", "gpt-image-2.5-sunburst", "/v1/images/edits", 1, 1, True, True, True, True
    )
    assert faults.evaluate_retry_candidate(classification, facts).outcome == "stop"
    assert not faults.evaluate_retry_candidate(classification, facts).can_send
    with_retry_after = replace(classification, http_status=429, retry_after_seconds=7)
    assert faults.evaluate_retry_candidate(with_retry_after, facts).outcome == "stop"


def test_legacy_schema_adds_empty_fault_table_without_rewriting_history(tmp_path: Path):
    from ecommerce_product_image_creator.storage import Store

    path = tmp_path / "old.sqlite"
    store = Store(path)
    store.create_task("task", "input", "request")
    store.create_revision("task", 1, "input")
    store.claim_operation("op", "task", 1, "digest", "attempt")
    with store._connect() as db:
        db.execute("DROP TABLE attempt_fault_evidence")
    reopened = Store(path)
    assert reopened.get_operation("op") is not None
    with reopened._connect() as db:
        assert db.execute("SELECT count(*) FROM attempt_fault_evidence").fetchone() == (
            0,
        )
        assert (
            verified_recoverability(db, "op", "attempt", "task", 1, "digest")
            == "unknown"
        )
