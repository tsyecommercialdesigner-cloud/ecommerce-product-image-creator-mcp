"""Deterministic interleaving for the task execution read boundary."""

import multiprocessing
import time

import pytest

from ecommerce_product_image_creator import candidate_preflight, tasks
from integration.test_attempt002 import prepare_retry
from integration.test_provider_recovery_tick import _tick_in_process
from integration.test_retry_scopes import make_scopes


def _task_tick_process(
    service, host, cost, first_id, ready, start, output, tick_id, barrier, tick_time
):
    from ecommerce_product_image_creator import provider, tasks
    from ecommerce_product_image_creator.execution_candidates import read_candidate

    original = provider.recovery_tick

    def route_to_task(service, root, tick_id, *, clock):
        snap = read_candidate(service.store, "source").snapshot
        provider.recovery_tick = original
        value = tasks.advance_task_execution(
            service, snap.task_id, snap.revision, snap.stage, root, tick_id, clock=clock
        )
        return provider.RecoveryTickResult(
            value["action"],
            value["reason"],
            value["candidate_id"],
            value["attempt_id"],
            value["deadline"],
        )

    provider.recovery_tick = route_to_task
    _tick_in_process(
        service,
        host,
        cost,
        first_id,
        ready,
        start,
        output,
        False,
        tick_id,
        barrier,
        tick_time,
    )


def _read_after_restart(
    service, identity: tuple[str, int, str, str], clock_value, output
):
    from ecommerce_product_image_creator import tasks
    from ecommerce_product_image_creator.storage import Store

    service.store = Store(service.store.path)
    state = tasks.read_task_execution(service, *identity)
    result = tasks.advance_task_execution(
        service, *identity, "after-restart", clock=lambda: clock_value
    )
    output.put(
        (
            state["budget"].total_used,
            state["wait"]["deadline"],
            result["action"],
            result["reason"],
            len(result["state"]["attempts"]),
        )
    )


def test_wait_observation_does_not_mix_committed_snapshots(tmp_path, monkeypatch):
    service, args, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    with service.store._connect() as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        prior = db.execute(
            "SELECT observed FROM retry_wait WHERE operation_id='op'"
        ).fetchone()[0]
    budget_view = candidate_preflight.budget_view
    observed = max(time.time(), prior + 1)

    def interleave(store, root, candidate):
        assert (
            service.store.retry_wait_status(root, observed, attempt_id=first.attempt_id)
            == "elapsed_recheck_required"
        )
        return budget_view(store, root, candidate)

    monkeypatch.setattr(candidate_preflight, "budget_view", interleave)
    snap = args[4]
    result = tasks.read_task_execution(
        service, snap.task_id, snap.revision, snap.stage, "op"
    )
    assert result["wait"]["observed"] == prior
    with service.store._connect() as db:
        assert db.execute(
            "SELECT observed FROM retry_wait WHERE operation_id='op'"
        ).fetchone() == (observed,)
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (0,)
    assert len(calls) == 1


def test_attempt_committed_in_other_process_does_not_mix_snapshot(
    tmp_path, monkeypatch
):
    service, args, first, calls, host, cost = make_scopes(tmp_path, monkeypatch)
    with service.store._connect() as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    ctx = multiprocessing.get_context("spawn")
    ready, start, output = ctx.Queue(), ctx.Event(), ctx.Queue()
    process = ctx.Process(
        target=_tick_in_process,
        args=(service, host, cost, first.attempt_id, ready, start, output),
    )
    process.start()
    budget_view = candidate_preflight.budget_view
    try:
        assert ready.get(timeout=30)

        def interleave(store, root, candidate):
            start.set()
            action, _ = output.get(timeout=45)
            assert action == "submitted"
            return budget_view(store, root, candidate)

        monkeypatch.setattr(candidate_preflight, "budget_view", interleave)
        snap = args[4]
        state = tasks.read_task_execution(
            service, snap.task_id, snap.revision, snap.stage, "op"
        )
        assert state["budget"].total_used == 1 and len(state["attempts"]) == 1
        assert state["combination_counts"][0]["count"] == 1
        with service.store._connect() as db:
            assert db.execute(
                "SELECT count FROM attempt_counter WHERE operation_id='op'"
            ).fetchone() == (2,)
        process.join(timeout=30)
        assert process.exitcode == 0
    finally:
        start.set()
        if process.is_alive():
            process.terminate()
            process.join()
    assert len(calls) == 1


def test_legacy_missing_candidate_is_auditable_but_cannot_advance(
    tmp_path, monkeypatch
):
    service, args, first, calls = prepare_retry(tmp_path, monkeypatch)
    snap = args[4]
    identity = (snap.task_id, snap.revision, snap.stage, "op")
    state = tasks.read_task_execution(service, *identity)
    assert state["attempts"][0]["attempt_id"] == first.attempt_id
    assert state["candidate_id"] is None and state["budget"] is None
    with pytest.raises(ValueError, match="execution_candidate_unavailable"):
        tasks.advance_task_execution(service, *identity, "legacy", clock=time.time)
    assert len(calls) == 1


def test_new_process_recovers_budget_and_wait_without_sending(tmp_path, monkeypatch):
    service, args, _, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    snap = args[4]
    identity = (snap.task_id, snap.revision, snap.stage, "op")
    before = tasks.read_task_execution(service, *identity)
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    process = ctx.Process(
        target=_read_after_restart,
        args=(service, identity, before["wait"]["started"] + 1, output),
    )
    process.start()
    try:
        used, deadline, action, reason, count = output.get(timeout=45)
        process.join(timeout=30)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
    assert used == 1 and deadline == before["wait"]["deadline"]
    assert action == "stop" and reason == "failure_unverified"
    assert count == 1 and len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (0,)


def test_two_process_task_ticks_compete_for_one_position(tmp_path, monkeypatch):
    service, _, first, calls, host, cost = make_scopes(tmp_path, monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    ready, output, start, barrier = (
        ctx.Queue(),
        ctx.Queue(),
        ctx.Event(),
        ctx.Barrier(2),
    )
    tick_time = time.time()
    processes = [
        ctx.Process(
            target=_task_tick_process,
            args=(
                service,
                host,
                cost,
                first.attempt_id,
                ready,
                start,
                output,
                f"task-{i}",
                barrier,
                tick_time,
            ),
        )
        for i in range(2)
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=30) and ready.get(timeout=30)
        start.set()
        results = [output.get(timeout=45) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
    assert any(action == "submitted" for action, _ in results), results
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (1,)
    assert (
        service.store.path.parent / "fixture-auto-sends"
    ).read_text().splitlines() == ["1"]
    assert len(calls) == 1


def test_task_entry_preserves_unknown_after_intent_process_crash(tmp_path, monkeypatch):
    service, args, first, calls, host, cost = make_scopes(tmp_path, monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    ready, start, output = ctx.Queue(), ctx.Event(), ctx.Queue()
    process = ctx.Process(
        target=_tick_in_process,
        args=(service, host, cost, first.attempt_id, ready, start, output, "before"),
    )
    process.start()
    try:
        assert ready.get(timeout=30)
        start.set()
        process.join(timeout=45)
        assert process.exitcode == 23
    finally:
        start.set()
        if process.is_alive():
            process.terminate()
            process.join()
    snap = args[4]
    identity = (snap.task_id, snap.revision, snap.stage, "op")
    state = tasks.read_task_execution(service, *identity)
    assert (
        state["attempts"][-1]["upstream"]["historical_status"] == "submission_uncertain"
    )
    result = tasks.advance_task_execution(
        service, *identity, "after-crash", clock=time.time
    )
    assert (
        result["action"] == "stop"
        and result["reason"] == "original_operation_unresolved"
    )
    assert result["state"]["budget"].total_used == 2 and len(calls) == 1
