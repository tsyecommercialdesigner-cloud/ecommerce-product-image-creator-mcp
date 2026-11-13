"""Same-combination scheduler with isolated evidence and fake transport."""

import time
from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator import offline_submission

from .test_attempt002 import prepare_retry


class FakeClock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_three_attempts_are_scheduled_without_real_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import socket

    def forbid_network(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    from ecommerce_product_image_creator import retry_scheduler

    service, args, first, calls = prepare_retry(tmp_path, monkeypatch)
    clock = FakeClock()
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    with service.store._connect() as db:
        db.execute("DELETE FROM retry_wait WHERE operation_id=?", ("op",))
        db.execute(
            "CREATE TABLE fixture_observation (attempt_id TEXT, status INTEGER, observed REAL, retry_after TEXT)"
        )
        db.execute(
            "INSERT INTO fixture_observation VALUES(?,?,?,?)",
            (first.attempt_id, 500, clock(), None),
        )

    def load(db, root, attempt):
        row = db.execute(
            "SELECT status,observed,retry_after FROM fixture_observation WHERE attempt_id=?",
            (attempt,),
        ).fetchone()
        if not row:
            return None
        return retry_scheduler.RetryObservation(
            "fixture-id",
            {"data": {"task_id": "fixture-id", "status": "FAILURE"}},
            row[0],
            row[2],
            row[1],
        )

    monkeypatch.setattr(retry_scheduler, "_load_observation", load)
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    scheduler = retry_scheduler.OfflineRetryScheduler(clock)
    assert scheduler.step(service, *args, root="op").action == "waiting"
    assert len(calls) == 1
    clock.advance(5)
    second = scheduler.step(service, *args, root="op")
    assert second.action == "submitted" and second.result is not None
    assert len(calls) == 2
    repeated = scheduler.step(service, *args, root="op")
    assert repeated.result is not None
    assert repeated.result.attempt_id == second.result.attempt_id
    third_confirmation = replace(args[5], confirmation_id="third-confirmation")
    third_args = (*args[:5], third_confirmation, "third-request", args[7])
    service.prepare(*third_args, expires_at=clock() + 120)
    with service.store._connect() as db:
        db.execute(
            "INSERT INTO fixture_observation VALUES(?,?,?,?)",
            (second.result.attempt_id, 500, clock(), None),
        )
        for table in ("fixture_retry_evidence", "fixture_retry_authorization"):
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?)",
                ("op", second.result.attempt_id, "third-confirmation"),
            )
    assert scheduler.step(service, *third_args, root="op").action == "waiting"
    clock.advance(5)
    third = scheduler.step(service, *third_args, root="op")
    assert third.action == "submitted" and third.result is not None
    assert (
        len(calls) == 3 and service.store.attempt_chain_status("op") == "third_recorded"
    )
    fourth_confirmation = replace(args[5], confirmation_id="fourth-confirmation")
    fourth_args = (*args[:5], fourth_confirmation, "fourth-request", args[7])
    service.prepare(*fourth_args, expires_at=clock() + 120)
    assert scheduler.step(service, *fourth_args, root="op").action == "stop"
    assert len(calls) == 3


def test_scheduler_stops_unknown_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ecommerce_product_image_creator import retry_scheduler

    service, args, first, calls = prepare_retry(tmp_path, monkeypatch, "unknown")
    scheduler = retry_scheduler.OfflineRetryScheduler(FakeClock())
    result = scheduler.step(service, *args, root="op")
    assert result.action == "stop" and len(calls) == 1 and first.state == "unknown"


def configured_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, status=500, header=None
):
    from ecommerce_product_image_creator import retry_scheduler

    service, args, first, calls = prepare_retry(tmp_path, monkeypatch)
    clock = FakeClock()
    clock.now = float(int(clock.now))
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    with service.store._connect() as db:
        db.execute("DELETE FROM retry_wait WHERE operation_id=?", ("op",))
        db.execute(
            "CREATE TABLE fixture_scheduler_observation (attempt_id TEXT, status INTEGER, observed REAL, retry_after TEXT)"
        )
        db.execute(
            "INSERT INTO fixture_scheduler_observation VALUES(?,?,?,?)",
            (first.attempt_id, status, clock(), header),
        )

    def load(db, root, attempt):
        row = db.execute(
            "SELECT status,observed,retry_after FROM fixture_scheduler_observation WHERE attempt_id=?",
            (attempt,),
        ).fetchone()
        return (
            None
            if row is None
            else retry_scheduler.RetryObservation(
                "fixture-id",
                {"data": {"task_id": "fixture-id", "status": "FAILURE"}},
                row[0],
                row[2],
                row[1],
            )
        )

    monkeypatch.setattr(retry_scheduler, "_load_observation", load)
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    return (
        retry_scheduler.OfflineRetryScheduler(clock),
        clock,
        service,
        args,
        first,
        calls,
    )


def test_429_seconds_and_http_date_respect_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from datetime import UTC, datetime
    from email.utils import format_datetime

    scheduler, clock, service, args, first, calls = configured_scheduler(
        tmp_path, monkeypatch, status=429, header="7"
    )
    assert scheduler.step(service, *args, root="op").action == "waiting"
    clock.advance(6)
    assert scheduler.step(service, *args, root="op").action == "waiting"
    clock.advance(1)
    assert scheduler.step(service, *args, root="op").action == "submitted"
    assert len(calls) == 2
    assert first.attempt_id
    # The HTTP-date form is parsed against the supplied observation time.
    from ecommerce_product_image_creator.faults import parse_retry_after

    start = datetime.fromtimestamp(clock(), UTC)
    header = format_datetime(datetime.fromtimestamp(clock() + 7, UTC), usegmt=True)
    assert parse_retry_after(header, start) == 7


@pytest.mark.parametrize("header", [None, "invalid"])
def test_429_missing_or_invalid_stops_without_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header
):
    scheduler, _clock, service, args, _first, calls = configured_scheduler(
        tmp_path, monkeypatch, status=429, header=header
    )
    assert scheduler.step(service, *args, root="op").action == "stop"
    assert len(calls) == 1


def test_authorization_and_safe_evidence_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scheduler, _clock, service, args, _first, calls = configured_scheduler(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        offline_submission, "_verify_retry_authorization", lambda *_: False
    )
    assert scheduler.step(service, *args, root="op").action == "stop"
    assert len(calls) == 1


def test_clock_rollback_stops_even_with_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scheduler, clock, service, args, _first, calls = configured_scheduler(
        tmp_path, monkeypatch
    )
    assert scheduler.step(service, *args, root="op").action == "waiting"
    clock.advance(2)
    assert scheduler.step(service, *args, root="op").action == "waiting"
    clock.advance(-1)
    assert scheduler.step(service, *args, root="op").action == "stop"
    assert len(calls) == 1


def test_parallel_scheduler_requests_create_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from concurrent.futures import ThreadPoolExecutor

    scheduler, clock, service, args, _first, calls = configured_scheduler(
        tmp_path, monkeypatch
    )
    assert scheduler.step(service, *args, root="op").action == "waiting"
    clock.advance(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: scheduler.step(service, *args, root="op"), range(2))
        )
    assert all(result.result is not None for result in results)
    assert results[0].result is not None and results[1].result is not None
    assert results[0].result.attempt_id == results[1].result.attempt_id
    assert len(calls) == 2


def test_running_with_known_id_directs_original_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ecommerce_product_image_creator import retry_scheduler

    service, args, _first, calls = prepare_retry(tmp_path, monkeypatch, "running")
    result = retry_scheduler.OfflineRetryScheduler(FakeClock()).step(
        service, *args, root="op"
    )
    assert result.action == "query_original" and len(calls) == 1


def test_429_http_date_is_used_by_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from datetime import UTC, datetime
    from email.utils import format_datetime

    scheduler, clock, service, args, first, calls = configured_scheduler(
        tmp_path, monkeypatch, status=429
    )
    header = format_datetime(datetime.fromtimestamp(clock() + 7, UTC), usegmt=True)
    with service.store._connect() as db:
        db.execute(
            "UPDATE fixture_scheduler_observation SET retry_after=? WHERE attempt_id=?",
            (header, first.attempt_id),
        )
    assert scheduler.step(service, *args, root="op").action == "waiting"
    clock.advance(6)
    assert scheduler.step(service, *args, root="op").action == "waiting"
    clock.advance(1)
    assert scheduler.step(service, *args, root="op").action == "submitted"
    assert len(calls) == 2


def test_upstream_success_never_regenerates_for_local_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ecommerce_product_image_creator import retry_scheduler

    service, args, first, calls = prepare_retry(tmp_path, monkeypatch, "succeeded")
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET delivery_status='download_failed' WHERE attempt_id=?",
            (first.attempt_id,),
        )
    result = retry_scheduler.OfflineRetryScheduler(FakeClock()).step(
        service, *args, root="op"
    )
    assert result.action == "stop" and len(calls) == 1


def test_unverified_failure_or_combination_never_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ecommerce_product_image_creator import retry_scheduler

    scheduler, _clock, service, args, _first, calls = configured_scheduler(
        tmp_path, monkeypatch
    )
    fixture_evidence = offline_submission._verify_retry_evidence
    monkeypatch.setattr(offline_submission, "_verify_retry_evidence", lambda *_: False)
    assert scheduler.step(service, *args, root="op").action == "stop"
    assert len(calls) == 1
    monkeypatch.setattr(offline_submission, "_verify_retry_evidence", fixture_evidence)
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: False)
    assert scheduler.step(service, *args, root="op").action == "stop"
    assert len(calls) == 1


def test_production_evidence_defaults_never_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ecommerce_product_image_creator import retry_scheduler

    service, args, _first, calls = prepare_retry(tmp_path, monkeypatch)
    monkeypatch.undo()
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda _: calls.append(1)
    )
    result = retry_scheduler.OfflineRetryScheduler(FakeClock()).step(
        service, *args, root="op"
    )
    assert result.action == "stop" and len(calls) == 1


def test_unknown_result_after_intent_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scheduler, clock, service, args, _first, calls = configured_scheduler(
        tmp_path, monkeypatch
    )
    assert scheduler.step(service, *args, root="op").action == "waiting"
    clock.advance(5)

    def timeout(_):
        calls.append(1)
        raise TimeoutError("fixture")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
    result = scheduler.step(service, *args, root="op")
    assert result.result is not None and result.result.state == "unknown"
    again = scheduler.step(service, *args, root="op")
    assert again.action == "existing" and again.result is not None
    assert again.result.attempt_id == result.result.attempt_id and len(calls) == 2


def test_wait_persists_across_process_and_scheduler_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import subprocess
    import sys

    from ecommerce_product_image_creator import retry_scheduler
    from ecommerce_product_image_creator.storage import Store
    from ecommerce_product_image_creator.tasks import LocalEligibility

    scheduler, clock, service, args, first, calls = configured_scheduler(
        tmp_path, monkeypatch
    )
    assert scheduler.step(service, *args, root="op").action == "waiting"
    code = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; print(Store(Path(sys.argv[1])).retry_wait_status('op',float(sys.argv[2]),attempt_id=sys.argv[3]))"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.store.path),
            str(clock()),
            first.attempt_id,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "waiting"
    restarted = LocalEligibility(
        Store(service.store.path), service.input_root, service.budget
    )
    scheduler = retry_scheduler.OfflineRetryScheduler(clock)
    assert scheduler.step(restarted, *args, root="op").action == "waiting"
    clock.advance(5)
    assert scheduler.step(restarted, *args, root="op").action == "submitted"
    assert len(calls) == 2


def test_scheduler_intent_failure_rolls_back_budget_and_never_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scheduler, clock, service, args, _first, calls = configured_scheduler(
        tmp_path, monkeypatch
    )
    assert scheduler.step(service, *args, root="op").action == "waiting"
    clock.advance(5)
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_scheduler_attempt BEFORE INSERT ON operation_attempt WHEN NEW.sequence=2 BEGIN SELECT RAISE(ABORT, 'fixture'); END"
        )
    assert scheduler.step(service, *args, root="op").action == "stop"
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='retry-request'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation WHERE operation_id='retry-request'"
        ).fetchone() == (0,)
    assert len(calls) == 1
