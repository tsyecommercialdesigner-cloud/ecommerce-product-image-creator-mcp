"""Task-level execution uses the existing scoped retry path."""

import time

import pytest

from ecommerce_product_image_creator import (
    offline_submission,
    provider,
    retry_scheduler,
    tasks,
)
from ecommerce_product_image_creator.execution_candidates import read_candidate
from integration.test_provider_recovery_tick import (
    test_automatic_six_attempt_chain_and_budget_cap as _exercise_six_attempt_chain,
)
from integration.test_retry_scopes import make_scopes


def test_task_execution_reads_and_delegates_scoped_retry(tmp_path, monkeypatch):
    service, args, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    snapshot = args[4]
    identity = (snapshot.task_id, snapshot.revision, snapshot.stage, "op")
    before = tasks.read_task_execution(service, *identity)
    assert [a["attempt_id"] for a in before["attempts"]] == [first.attempt_id]
    assert before["budget"].total_used == 1
    assert before["combination_counts"][0]["count"] == 1
    assert len(calls) == 1
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    monkeypatch.setattr(
        retry_scheduler,
        "_load_observation",
        lambda db, root, attempt: retry_scheduler.RetryObservation(
            "fixture-id",
            {"data": {"task_id": "fixture-id", "status": "FAILURE"}},
            500,
            None,
            time.time() - 10,
        ),
    )
    monkeypatch.setattr(offline_submission, "_verify_retry_evidence", lambda *_: True)
    monkeypatch.setattr(
        offline_submission, "_verify_retry_authorization", lambda *_: True
    )
    result = tasks.advance_task_execution(service, *identity, "tick-1", clock=time.time)
    assert result["action"] == "submitted"
    assert len(result["state"]["attempts"]) == 2
    assert result["state"]["budget"].total_used == 2
    replay = tasks.advance_task_execution(service, *identity, "tick-1", clock=time.time)
    assert replay["action"] == "existing"
    assert len(calls) == 2
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (1,)


def test_task_execution_unknown_never_retries(tmp_path, monkeypatch):
    service, args, first, calls, _, _ = make_scopes(
        tmp_path, monkeypatch, first_state="unknown"
    )
    snap = args[4]
    identity = (snap.task_id, snap.revision, snap.stage, "op")
    before = tasks.read_task_execution(service, *identity)
    assert before["attempts"][0]["upstream"]["historical_status"] == "unknown"
    result = tasks.advance_task_execution(
        service, *identity, "tick-unknown", clock=time.time
    )
    assert result["action"] != "submitted"
    assert result["state"]["attempts"][0]["attempt_id"] == first.attempt_id
    assert result["state"]["budget"].total_used == 1
    assert len(calls) == 1


def test_task_execution_rejects_wrong_identity_without_sending(tmp_path, monkeypatch):
    service, args, _, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    snapshot = args[4]
    for identity in (
        ("wrong", snapshot.revision, snapshot.stage, "op"),
        (snapshot.task_id, snapshot.revision + 1, snapshot.stage, "op"),
        (snapshot.task_id, snapshot.revision, "mock", "op"),
    ):
        with pytest.raises(ValueError):
            tasks.advance_task_execution(service, *identity, "tick", clock=time.time)
    assert len(calls) == 1


def test_task_execution_wait_is_advisory_and_read_only(tmp_path, monkeypatch):
    service, args, _, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    snapshot = args[4]
    with service.store._connect() as db:
        saved = db.execute(
            "SELECT started,deadline,observed FROM retry_wait WHERE operation_id='op'"
        ).fetchone()
    assert saved is not None
    identity = (snapshot.task_id, snapshot.revision, snapshot.stage, "op")
    before = tasks.read_task_execution(service, *identity)
    assert before["wait"]["deadline"] == saved[1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT observed FROM retry_wait WHERE operation_id='op'"
        ).fetchone() == (saved[2],)
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (0,)
    assert len(calls) == 1


def test_task_execution_six_attempts_and_budget_cap(tmp_path, monkeypatch):
    original = provider.recovery_tick

    def task_tick(service, root, tick_id, *, clock):
        with service.store._connect() as db:
            row = db.execute(
                "SELECT candidate_id FROM execution_candidate WHERE root_operation_id=? AND source_candidate_id IS NULL",
                (root,),
            ).fetchone()
        assert row is not None
        snapshot = read_candidate(service.store, row[0]).snapshot
        with monkeypatch.context() as scoped:
            scoped.setattr(provider, "recovery_tick", original)
            result = tasks.advance_task_execution(
                service,
                snapshot.task_id,
                snapshot.revision,
                snapshot.stage,
                root,
                tick_id,
                clock=clock,
            )
        return provider.RecoveryTickResult(
            result["action"],
            result["reason"],
            result["candidate_id"],
            result["attempt_id"],
            result["deadline"],
        )

    monkeypatch.setattr(provider, "recovery_tick", task_tick)
    _exercise_six_attempt_chain(tmp_path, monkeypatch)
