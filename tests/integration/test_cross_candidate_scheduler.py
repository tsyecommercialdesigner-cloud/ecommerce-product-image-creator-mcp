"""Explicit offline single-step scheduling through the existing submit transaction."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator import (
    candidate_preflight,
    offline_submission,
    retry_scheduler,
)

from .test_attempt002 import prepare_retry
from .test_candidate_preflight import candidates
from .test_cross_candidate_submission import setup_cross
from .test_retry_scheduler import FakeClock


def test_explicit_steps_switch_and_finish_six_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, target, _, _, third, b, sent = setup_cross(tmp_path, monkeypatch)
    clock = FakeClock()
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, root, attempt, c: (
            root == "op"
            and c.candidate_id == "b"
            and db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt,)
            ).fetchone()
            == ("failed",)
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, op, cid: (
            c.snapshot == b.snapshot
            and op.startswith("step-")
            and cid.startswith("step-confirm-")
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda req: (
            sent.append(req)
            or offline_submission.ProviderResult("failed", task_id="fixture-id")
        ),
    )
    scheduler = retry_scheduler.OfflineRetryScheduler(clock)
    previous = third
    for seq in (4, 5, 6):
        if seq > 4:
            service.store.record_retry_wait(
                "op", previous.attempt_id, clock() - 10, None, 500
            )
        confirmation = replace(target[5], confirmation_id=f"step-confirm-{seq}")
        args = (*target[:5], confirmation, f"step-{seq}", target[7])
        service.prepare(*args, expires_at=clock() + 120, candidate_id="b")
        if seq == 4:
            advisory = scheduler.step(
                service, *args, root="op", candidate_id="b", evaluate_only=True
            )
            assert advisory.action == "retry_candidate" and sent == []
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT consumed FROM local_eligibility WHERE operation_id=?",
                    (args[6],),
                ).fetchone() == (0,)
        result = scheduler.step(service, *args, root="op", candidate_id="b")
        assert result.action == "submitted" and result.result is not None
        assert len(sent) == seq - 3
        repeated = scheduler.step(service, *args, root="op", candidate_id="b")
        assert (
            repeated.action == "existing"
            and repeated.result is not None
            and repeated.result.attempt_id == result.result.attempt_id
        )
        previous = result.result
    view = candidate_preflight.budget_view(service.store, "op", "b")
    assert (view.total_used, view.source_used, view.combination_used) == (6, 3, 3)
    assert (
        scheduler.step(service, *args, root="op", candidate_id="missing").action
        == "stop"
    )
    assert len(sent) == 3


def test_missing_candidate_and_unauthorized_never_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, target, _, _, _, _b, sent = setup_cross(tmp_path, monkeypatch)
    scheduler = retry_scheduler.OfflineRetryScheduler(time.time)
    missing = scheduler.step(service, *target, root="op", candidate_id="missing")
    assert (
        missing.action == "stop"
        and missing.reason == "candidate_unavailable_or_invalid"
    )
    monkeypatch.setattr(
        candidate_preflight, "_verify_candidate_capability", lambda *_: False
    )
    assert (
        scheduler.step(service, *target, root="op", candidate_id="b").action == "stop"
    )
    assert sent == []


def test_full_six_attempts_via_explicit_single_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, first, sends = prepare_retry(tmp_path, monkeypatch)
    clock = FakeClock()
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    monkeypatch.setattr(
        retry_scheduler,
        "_load_observation",
        lambda db, root, attempt: (
            retry_scheduler.RetryObservation(
                "fixture-id",
                {"data": {"task_id": "fixture-id", "status": "FAILURE"}},
                500,
                None,
                clock() - 10,
            )
            if db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt,)
            ).fetchone()
            == ("failed",)
            else None
        ),
    )
    scheduler = retry_scheduler.OfflineRetryScheduler(clock)
    second = scheduler.step(service, *args, root="op")
    assert (
        second.action == "submitted" and second.result is not None and len(sends) == 2
    )
    service.store.record_retry_wait(
        "op", second.result.attempt_id, clock() - 10, None, 500
    )
    confirmation = replace(args[5], confirmation_id="form-three")
    third_args = (*args[:5], confirmation, "form-three-op", args[7])
    service.prepare(*third_args, expires_at=clock() + 120)
    with service.store._connect() as db:
        for table in ("fixture_retry_evidence", "fixture_retry_authorization"):
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?)",
                ("op", second.result.attempt_id, "form-three"),
            )
    third = scheduler.step(service, *third_args, root="op")
    assert third.action == "submitted" and third.result is not None and len(sends) == 3
    _, b = candidates(service, args)
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, root, attempt, c: (
            root == "op"
            and c.candidate_id == "b"
            and db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt,)
            ).fetchone()
            == ("failed",)
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_authorization",
        lambda _db, c: c.snapshot == b.snapshot,
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_capability",
        lambda _db, c: c.snapshot == b.snapshot,
    )
    monkeypatch.setattr(
        candidate_preflight, "_verify_business", lambda _db, c: c.snapshot == b.snapshot
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, op, cid: (
            c.snapshot == b.snapshot
            and op.startswith("full-")
            and cid.startswith("full-confirm-")
        ),
    )
    previous = third.result
    for seq in (4, 5, 6):
        service.store.record_retry_wait(
            "op", previous.attempt_id, clock() - 10, None, 500
        )
        confirmation = replace(
            args[5],
            confirmation_id=f"full-confirm-{seq}",
            execution_digest=b.snapshot.execution_digest,
        )
        step = (*args[:4], b.snapshot, confirmation, f"full-{seq}", args[7])
        service.prepare(*step, expires_at=clock() + 120, candidate_id="b")
        result = scheduler.step(service, *step, root="op", candidate_id="b")
        assert (
            result.action == "submitted"
            and result.result is not None
            and len(sends) == seq
        )
        previous = result.result
    confirmation = replace(
        args[5],
        confirmation_id="full-confirm-7",
        execution_digest=b.snapshot.execution_digest,
    )
    step = (*args[:4], b.snapshot, confirmation, "full-7", args[7])
    service.prepare(*step, expires_at=clock() + 120, candidate_id="b")
    assert scheduler.step(service, *step, root="op", candidate_id="b").action == "stop"
    assert len(sends) == 6 and first.attempt_id != previous.attempt_id
    with service.store._connect() as db:
        chain = db.execute(
            "SELECT sequence,execution_digest FROM operation_attempt WHERE operation_id='op' ORDER BY sequence"
        ).fetchall()
        assert chain == [
            (i, args[4].execution_digest if i <= 3 else b.snapshot.execution_digest)
            for i in range(1, 7)
        ]
        assert db.execute(
            "SELECT combination_sequence FROM candidate_attempt WHERE root_operation_id='op' ORDER BY combination_sequence"
        ).fetchall() == [(1,), (2,), (3,)]
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE operation_id='op'"
        ).fetchone() == (6,)


@pytest.mark.parametrize("state", ["unknown", "running"])
def test_scheduler_prefers_query_for_unresolved_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
):
    service, target, _, _, third, _, sent = setup_cross(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status=? WHERE attempt_id=?",
            (state, third.attempt_id),
        )
    result = retry_scheduler.OfflineRetryScheduler(time.time).step(
        service, *target, root="op", candidate_id="b"
    )
    assert result.action == "query_original" and sent == []


def test_competing_steps_never_duplicate_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, target, _, _, third, b, sent = setup_cross(tmp_path, monkeypatch)
    clock = FakeClock()
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, root, attempt, c: (
            root == "op"
            and c.candidate_id == "b"
            and db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt,)
            ).fetchone()
            == ("failed",)
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, _op, _cid: c.snapshot == b.snapshot,
    )
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda req: (
            sent.append(req)
            or offline_submission.ProviderResult("failed", task_id="fixture-id")
        ),
    )
    scheduler = retry_scheduler.OfflineRetryScheduler(clock)
    previous = third
    for seq in (4, 5, 6):
        if seq > 4:
            service.store.record_retry_wait(
                "op", previous.attempt_id, clock() - 10, None, 500
            )
        requests = []
        for suffix in ("a", "b"):
            confirmation = replace(target[5], confirmation_id=f"race-{seq}-{suffix}")
            step = (*target[:5], confirmation, f"race-op-{seq}-{suffix}", target[7])
            service.prepare(*step, expires_at=clock() + 120, candidate_id="b")
            requests.append(step)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda step: scheduler.step(
                        service, *step, root="op", candidate_id="b"
                    ),
                    requests,
                )
            )
        winners = [r for r in results if r.action == "submitted"]
        assert len(winners) == 1 and winners[0].result is not None
        previous = winners[0].result
        with service.store._connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op' AND sequence=?",
                (seq,),
            ).fetchone() == (1,)
    assert len(sent) == 3
