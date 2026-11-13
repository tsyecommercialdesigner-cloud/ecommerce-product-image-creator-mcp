"""Offline second attempt: fake evidence and fake dispatch only."""

import time
from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator import offline_submission

from .test_offline_submission import setup_context


def prepare_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_state: str = "failed",
    *,
    first_authorizations: bool = False,
):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []

    def fake(_):
        calls.append(1)
        if first_state == "unknown":
            raise TimeoutError("fixture")
        return offline_submission.ProviderResult(first_state, task_id="fixture-id")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", fake)
    if first_authorizations:
        from ecommerce_product_image_creator import first_authorization

        snap = args[4]
        monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
        with service.store._connect() as db:
            for role in ("host", "fee"):
                first_authorization.record(
                    db,
                    first_authorization.FirstAuthorization(
                        f"old-{role}",
                        role,
                        "op",
                        snap.task_id,
                        snap.revision,
                        snap.stage,
                        snap.execution_digest,
                        snap.model,
                        snap.endpoint,
                        snap.group,
                        snap.operation_scope,
                        time.time() + 120,
                        "fixture",
                        f"fixture-{role}",
                    ),
                )
    first = offline_submission.submit_offline(
        service, *args, require_first_authorizations=first_authorizations
    )
    assert first.state == first_state
    if first_state == "failed":
        service.store.record_retry_wait(
            "op", first.attempt_id, time.time() - 10, None, 500
        )
    confirmation = replace(args[5], confirmation_id="retry-confirmation")
    retry_args = (*args[:5], confirmation, "retry-request", args[7])
    service.prepare(*retry_args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        db.execute(
            "CREATE TABLE fixture_retry_evidence (operation_id TEXT, attempt_id TEXT, confirmation_id TEXT)"
        )
        db.execute(
            "CREATE TABLE fixture_retry_authorization (operation_id TEXT, attempt_id TEXT, confirmation_id TEXT)"
        )
        db.execute(
            "INSERT INTO fixture_retry_evidence VALUES(?,?,?)",
            ("op", first.attempt_id, "retry-confirmation"),
        )
        db.execute(
            "INSERT INTO fixture_retry_authorization VALUES(?,?,?)",
            ("op", first.attempt_id, "retry-confirmation"),
        )

    def verify(db, root, attempt, snapshot, confirmation_id):
        return bool(
            db.execute(
                "SELECT 1 FROM fixture_retry_evidence WHERE operation_id=? AND attempt_id=? AND confirmation_id=?",
                (root, attempt, confirmation_id),
            ).fetchone()
        )

    monkeypatch.setattr(offline_submission, "_verify_retry_evidence", verify)

    def authorize(db, root, attempt, snapshot, confirmation_id):
        return bool(
            db.execute(
                "SELECT 1 FROM fixture_retry_authorization WHERE operation_id=? AND attempt_id=? AND confirmation_id=?",
                (root, attempt, confirmation_id),
            ).fetchone()
        )

    monkeypatch.setattr(offline_submission, "_verify_retry_authorization", authorize)
    return service, retry_args, first, calls


def test_second_attempt_is_linked_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, first, calls = prepare_retry(tmp_path, monkeypatch)
    second = offline_submission.submit_offline(service, *args, retry_of="op")
    assert second.attempt_id != first.attempt_id
    assert service.store.attempt_chain_status("op") == "second_recorded"
    assert (
        offline_submission.submit_offline(service, *args, retry_of="op").attempt_id
        == second.attempt_id
    )
    assert len(calls) == 2
    with service.store._connect() as db:
        rows = db.execute(
            "SELECT sequence,attempt_id,previous_attempt_id FROM operation_attempt WHERE operation_id=? ORDER BY sequence",
            ("op",),
        ).fetchall()
        assert rows == [
            (1, first.attempt_id, None),
            (2, second.attempt_id, first.attempt_id),
        ]
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id=?", ("op",)
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id=?",
            ("retry-request",),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (first.attempt_id,),
        ).fetchone() == ("failed",)
        assert db.execute(
            "SELECT attempt_id,attempt_count FROM operation WHERE operation_id=?",
            ("retry-request",),
        ).fetchone() == (second.attempt_id, 1)
        assert db.execute(
            "SELECT attempt_count FROM operation WHERE operation_id=?", ("op",)
        ).fetchone() == (2,)


def test_retry_unverified_evidence_never_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)
    monkeypatch.setattr(offline_submission, "_verify_retry_evidence", lambda *_: False)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(calls) == 1


def test_retry_wait_and_authorization_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute(
            "UPDATE retry_wait SET deadline=? WHERE operation_id=?",
            (time.time() + 100, "op"),
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    with service.store._connect() as db:
        db.execute(
            "UPDATE retry_wait SET deadline=? WHERE operation_id=?",
            (time.time() - 1, "op"),
        )
    monkeypatch.setattr(
        offline_submission, "_verify_retry_authorization", lambda *_: False
    )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(calls) == 1


def test_unknown_first_is_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, first, calls = prepare_retry(tmp_path, monkeypatch, "unknown")
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (first.attempt_id,),
        ).fetchone() == ("unknown",)
    assert len(calls) == 1


def test_retry_chain_insert_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_second BEFORE INSERT ON operation_attempt WHEN NEW.sequence=2 BEGIN SELECT RAISE(ABORT, 'fixture'); END"
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id=?", ("op",)
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id=?",
            ("retry-request",),
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation WHERE operation_id=?", ("retry-request",)
        ).fetchone() == (0,)
    assert len(calls) == 1


def test_retry_same_request_parallel_is_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from concurrent.futures import ThreadPoolExecutor

    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _: offline_submission.submit_offline(
                    service, *args, retry_of="op"
                ),
                range(2),
            )
        )
    assert outcomes[0].attempt_id == outcomes[1].attempt_id
    assert len(calls) == 2


def test_retry_production_verifiers_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)
    monkeypatch.undo()
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(calls) == 1


def test_other_request_id_cannot_create_third_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)
    second = offline_submission.submit_offline(service, *args, retry_of="op")
    third_confirmation = replace(args[5], confirmation_id="other-confirmation")
    third_args = (*args[:5], third_confirmation, "other-request", args[7])
    service.prepare(*third_args, expires_at=time.time() + 120)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *third_args, retry_of="op")
    assert len(calls) == 2
    assert second.attempt_id


def test_legacy_missing_budget_is_not_inferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute("DELETE FROM attempt_counter WHERE operation_id=?", ("op",))
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(calls) == 1


def test_second_chain_survives_process_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import subprocess
    import sys

    service, args, first, _ = prepare_retry(tmp_path, monkeypatch)
    second = offline_submission.submit_offline(service, *args, retry_of="op")
    code = "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(db.execute(\"SELECT group_concat(sequence, ',') FROM (SELECT sequence FROM operation_attempt WHERE operation_id='op' ORDER BY sequence)\").fetchone()[0]); print(db.execute(\"SELECT count FROM attempt_counter WHERE operation_id='op'\").fetchone()[0])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.splitlines() == ["1,2", "2"]
    assert second.attempt_id != first.attempt_id


def test_revoked_after_intent_never_reaches_fake_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)

    def revoke():
        with service.store._connect() as db:
            db.execute("DELETE FROM fixture_retry_authorization")

    monkeypatch.setattr(offline_submission, "_after_intent_recorded", revoke)
    second = offline_submission.submit_offline(service, *args, retry_of="op")
    assert second.state == "unknown"
    assert len(calls) == 1
    assert (
        offline_submission.submit_offline(service, *args, retry_of="op").attempt_id
        == second.attempt_id
    )
    assert len(calls) == 1


def test_two_distinct_retry_requests_compete_for_one_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from concurrent.futures import ThreadPoolExecutor

    service, args, first, calls = prepare_retry(tmp_path, monkeypatch)
    other_confirmation = replace(args[5], confirmation_id="other-confirmation")
    other_args = (*args[:5], other_confirmation, "other-request", args[7])
    service.prepare(*other_args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        db.execute(
            "UPDATE local_eligibility SET invalidated=0 WHERE operation_id=?",
            ("retry-request",),
        )
        for table in ("fixture_retry_evidence", "fixture_retry_authorization"):
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?)",
                ("op", first.attempt_id, "other-confirmation"),
            )

    def submit(item):
        try:
            return offline_submission.submit_offline(
                service, *item, retry_of="op"
            ).attempt_id
        except offline_submission.SubmissionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, [args, other_args]))
    assert sum(value is not None for value in outcomes) == 1
    assert len(calls) == 2
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id=?", ("op",)
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE operation_id=?", ("op",)
        ).fetchone() == (2,)


def test_running_original_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, first, calls = prepare_retry(tmp_path, monkeypatch, "running")
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
            (first.attempt_id,),
        ).fetchone() == ("running", "fixture-id")
    assert len(calls) == 1


def test_missing_legacy_chain_is_not_inferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute("DELETE FROM operation_attempt WHERE operation_id=?", ("op",))
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(calls) == 1
