"""Offline third attempt with per-attempt wait; no provider access."""

import time
from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator import offline_submission

from .test_attempt002 import prepare_retry


def prepare_third(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, args, first, calls = prepare_retry(tmp_path, monkeypatch)
    second = offline_submission.submit_offline(service, *args, retry_of="op")
    assert second.state == "failed"
    service.store.record_retry_wait(
        "op", second.attempt_id, time.time() - 10, None, 500
    )
    confirmation = replace(args[5], confirmation_id="third-confirmation")
    third_args = (*args[:5], confirmation, "third-request", args[7])
    service.prepare(*third_args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        for table in ("fixture_retry_evidence", "fixture_retry_authorization"):
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?)",
                ("op", second.attempt_id, "third-confirmation"),
            )
    return service, third_args, first, second, calls


def test_third_attempt_and_wait_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, first, second, calls = prepare_third(tmp_path, monkeypatch)
    third = offline_submission.submit_offline(service, *args, retry_of="op")
    assert third.attempt_id not in (first.attempt_id, second.attempt_id)
    assert (
        offline_submission.submit_offline(service, *args, retry_of="op").attempt_id
        == third.attempt_id
    )
    assert len(calls) == 3
    with service.store._connect() as db:
        assert db.execute(
            "SELECT sequence,attempt_id,previous_attempt_id FROM operation_attempt WHERE operation_id=? ORDER BY sequence",
            ("op",),
        ).fetchall() == [
            (1, first.attempt_id, None),
            (2, second.attempt_id, first.attempt_id),
            (3, third.attempt_id, second.attempt_id),
        ]
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id=?", ("op",)
        ).fetchone() == (3,)
        assert db.execute(
            "SELECT attempt_id FROM retry_wait WHERE operation_id=?", ("op",)
        ).fetchone() == (first.attempt_id,)
        assert db.execute(
            "SELECT attempt_id FROM retry_wait_attempt WHERE operation_id=?", ("op",)
        ).fetchone() == (second.attempt_id,)


def test_third_unknown_prior_never_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _first, calls = prepare_retry(tmp_path, monkeypatch)

    def timeout(_):
        calls.append(1)
        raise TimeoutError("fixture")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
    second = offline_submission.submit_offline(service, *args, retry_of="op")
    assert second.state == "unknown"
    with pytest.raises(ValueError):
        service.store.record_retry_wait(
            "op", second.attempt_id, time.time() - 10, None, 500
        )
    confirmation = replace(args[5], confirmation_id="third-unknown-confirmation")
    third_args = (*args[:5], confirmation, "third-unknown-request", args[7])
    service.prepare(*third_args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        for table in ("fixture_retry_evidence", "fixture_retry_authorization"):
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?)",
                ("op", second.attempt_id, confirmation.confirmation_id),
            )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *third_args, retry_of="op")
    assert len(calls) == 2


def test_fourth_attempt_cannot_reset_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, _, calls = prepare_third(tmp_path, monkeypatch)
    third = offline_submission.submit_offline(service, *args, retry_of="op")
    assert service.store.attempt_chain_status("op") == "third_recorded"
    fourth_confirmation = replace(args[5], confirmation_id="fourth-confirmation")
    fourth_args = (*args[:5], fourth_confirmation, "fourth-request", args[7])
    service.prepare(*fourth_args, expires_at=time.time() + 120)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *fourth_args, retry_of="op")
    assert len(calls) == 3 and third.attempt_id


def test_third_wait_and_independent_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, second, calls = prepare_third(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute(
            "UPDATE retry_wait_attempt SET deadline=? WHERE operation_id=? AND attempt_id=?",
            (time.time() + 60, "op", second.attempt_id),
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    with service.store._connect() as db:
        db.execute(
            "UPDATE retry_wait_attempt SET deadline=? WHERE operation_id=? AND attempt_id=?",
            (time.time() - 1, "op", second.attempt_id),
        )
        db.execute(
            "DELETE FROM fixture_retry_authorization WHERE confirmation_id=?",
            ("third-confirmation",),
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(calls) == 2


def test_third_transaction_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, _, calls = prepare_third(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_third BEFORE INSERT ON operation_attempt WHEN NEW.sequence=3 BEGIN SELECT RAISE(ABORT, 'fixture'); END"
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id=?", ("op",)
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id=?",
            ("third-request",),
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation WHERE operation_id=?", ("third-request",)
        ).fetchone() == (0,)
    assert len(calls) == 2


def test_third_chain_and_wait_survive_new_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import subprocess
    import sys

    service, args, _, second, _ = prepare_third(tmp_path, monkeypatch)
    third = offline_submission.submit_offline(service, *args, retry_of="op")
    code = "from pathlib import Path; import sys,time; from ecommerce_product_image_creator.storage import Store; s=Store(Path(sys.argv[1])); print(s.attempt_chain_status('op')); print(s.retry_wait_status('op',time.time(),attempt_id=sys.argv[2]))"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path), second.attempt_id],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.splitlines() == ["third_recorded", "elapsed_recheck_required"]
    assert third.attempt_id


def test_two_distinct_third_requests_compete_for_one_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from concurrent.futures import ThreadPoolExecutor

    service, args, _, second, calls = prepare_third(tmp_path, monkeypatch)
    other_confirmation = replace(args[5], confirmation_id="other-third-confirmation")
    other_args = (*args[:5], other_confirmation, "other-third-request", args[7])
    service.prepare(*other_args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        db.execute(
            "UPDATE local_eligibility SET invalidated=0 WHERE operation_id=?",
            ("third-request",),
        )
        for table in ("fixture_retry_evidence", "fixture_retry_authorization"):
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?)",
                ("op", second.attempt_id, "other-third-confirmation"),
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
    assert len(calls) == 3
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id=?", ("op",)
        ).fetchone() == (3,)


def test_missing_second_wait_and_missing_budget_are_conservative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, second, calls = prepare_third(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute(
            "DELETE FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
            ("op", second.attempt_id),
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    with service.store._connect() as db:
        db.execute("DELETE FROM attempt_counter WHERE operation_id=?", ("op",))
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(calls) == 2


def test_legacy_first_wait_remains_bound_and_clock_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, _args, first, second, _calls = prepare_third(tmp_path, monkeypatch)
    now = time.time()
    assert (
        service.store.retry_wait_status("op", now, attempt_id=first.attempt_id)
        == "elapsed_recheck_required"
    )
    assert (
        service.store.retry_wait_status("op", now, attempt_id=second.attempt_id)
        == "elapsed_recheck_required"
    )
    assert (
        service.store.retry_wait_status("op", now - 1, attempt_id=second.attempt_id)
        == "clock_invalid"
    )
    assert service.store.retry_wait_status("op", now, attempt_id="missing") == "unknown"


def test_legacy_wait_schema_upgrades_without_rewriting_old_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ecommerce_product_image_creator.storage import Store

    service, _args, first, _calls = prepare_retry(tmp_path, monkeypatch)
    with service.store._connect() as db:
        old = db.execute(
            "SELECT * FROM retry_wait WHERE operation_id=?", ("op",)
        ).fetchone()
        db.execute("DROP TABLE retry_wait_attempt")
    reopened = Store(service.store.path)
    with reopened._connect() as db:
        assert (
            db.execute(
                "SELECT * FROM retry_wait WHERE operation_id=?", ("op",)
            ).fetchone()
            == old
        )
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='retry_wait_attempt'"
        ).fetchone() == ("retry_wait_attempt",)
    assert (
        reopened.retry_wait_status("op", time.time(), attempt_id=first.attempt_id)
        == "elapsed_recheck_required"
    )


def test_incomplete_second_link_and_conflicting_wait_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, second, calls = prepare_third(tmp_path, monkeypatch)
    with service.store._connect() as db:
        started = db.execute(
            "SELECT started FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
            ("op", second.attempt_id),
        ).fetchone()[0]
    with pytest.raises(ValueError):
        service.store.record_retry_wait("op", second.attempt_id, started + 1, None, 500)
    with service.store._connect() as db:
        db.execute(
            "DELETE FROM operation_attempt WHERE operation_id=? AND sequence=2", ("op",)
        )
    assert service.store.attempt_chain_status("op") == "unknown"
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(calls) == 2
