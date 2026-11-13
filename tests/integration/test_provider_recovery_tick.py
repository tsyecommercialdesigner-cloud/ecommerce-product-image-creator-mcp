"""Offline automatic recovery selects candidates from persisted evidence."""

import multiprocessing
import os
import time
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from ecommerce_product_image_creator import (
    candidate_preflight,
    offline_submission,
    provider,
    retry_scheduler,
    retry_scope,
)
from ecommerce_product_image_creator.domain import ExecutionSnapshot
from ecommerce_product_image_creator.execution_candidates import (
    create_candidate,
    read_candidate,
    register_root_candidate,
)
from integration.test_attempt002 import prepare_retry
from integration.test_cross_candidate_submission import setup_cross
from integration.test_retry_scopes import make_scopes


def test_tick_selects_source_from_persisted_chain_and_replays(tmp_path, monkeypatch):
    service, _, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
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
    monkeypatch.setattr(
        offline_submission,
        "_verify_retry_evidence",
        lambda db, root, attempt, snap, cid: (
            root == "op" and attempt == first.attempt_id
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_retry_authorization",
        lambda db, root, attempt, snap, cid: (
            root == "op" and attempt == first.attempt_id
        ),
    )
    result = provider.recovery_tick(service, "op", "tick-1", clock=time.time)
    assert result.action == "submitted" and result.candidate_id == "source"
    assert result.attempt_id != first.attempt_id and len(calls) == 2
    repeated = provider.recovery_tick(service, "op", "tick-1", clock=time.time)
    assert repeated.action == "existing" and repeated.attempt_id == result.attempt_id
    assert len(calls) == 2
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (1,)


def test_automatic_six_attempt_chain_and_budget_cap(tmp_path, monkeypatch):
    service, args, first, calls = prepare_retry(tmp_path, monkeypatch)
    source = register_root_candidate(service.store, "op", "source")
    snap = args[4]
    alt_snapshot = ExecutionSnapshot.create(
        args[1],
        args[3],
        snap.stage,
        candidate_preflight.FLARE,
        snap.endpoint,
        snap.group,
        references=snap.references,
        operation_scope=snap.operation_scope,
    )
    alternate = create_candidate(
        service.store, "op", "alternate", "source", alt_snapshot
    )
    grants = tuple(
        retry_scope.Grant(
            c.candidate_id,
            c.snapshot.execution_digest,
            c.snapshot.model,
            c.snapshot.endpoint,
            c.snapshot.group,
            3,
        )
        for c in (source, alternate)
    )
    current = [time.time()]

    def clock():
        return current[0]

    common = (
        "op",
        snap.task_id,
        snap.revision,
        snap.stage,
        snap.input_digest,
        grants,
        6,
        current[0] + 120,
        "fixture",
        "evidence",
    )
    host = retry_scope.HostScope("host-both", *common)
    cost = retry_scope.CostScope("cost-both", *common, "USD", 200, 20, "fixture-price")
    monkeypatch.setattr(
        retry_scope, "_verify_host_source", lambda db, value: value == host
    )
    monkeypatch.setattr(
        retry_scope, "_verify_cost_source", lambda db, value: value == cost
    )
    monkeypatch.setattr(
        retry_scope, "_verify_price_source", lambda db, value: value == cost
    )
    monkeypatch.setattr(
        retry_scope,
        "_verify_candidate_capability",
        lambda db, cid, snapshot: any(
            c.candidate_id == cid and c.snapshot == snapshot
            for c in (source, alternate)
        ),
    )
    retry_scope.record_host_scope(service.store, host)
    retry_scope.record_cost_scope(service.store, cost)
    monkeypatch.setattr(retry_scope.time, "time", clock)
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    monkeypatch.setattr(
        retry_scheduler,
        "_load_observation",
        lambda db, root, attempt: retry_scheduler.RetryObservation(
            "fixture-id",
            {"data": {"task_id": "fixture-id", "status": "FAILURE"}},
            500,
            None,
            clock(),
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_retry_evidence",
        lambda db, root, attempt, snapshot, confirmation: (
            root == "op"
            and bool(
                db.execute(
                    "SELECT 1 FROM operation_attempt WHERE operation_id=? AND attempt_id=?",
                    (root, attempt),
                ).fetchone()
            )
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_retry_authorization",
        lambda db, root, attempt, snapshot, confirmation: (
            root == "op" and confirmation.startswith("auto-confirm-")
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda db, candidate, op, confirmation: (
            candidate.candidate_id == "alternate"
            and confirmation.startswith("auto-confirm-")
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, root, attempt, c: (
            root == "op"
            and c.candidate_id == "alternate"
            and db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt,)
            ).fetchone()
            == ("failed",)
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_authorization",
        lambda db, c: c.candidate_id == "alternate",
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_capability",
        lambda db, c: c.candidate_id == "alternate",
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_business",
        lambda db, c: c.candidate_id == "alternate",
    )
    identities = [first.attempt_id]
    for position in range(2, 7):
        tick_id = f"tick-{position}"
        result = provider.recovery_tick(service, "op", tick_id, clock=clock)
        if result.action == "waiting":
            assert result.deadline == clock() + 5
            current[0] += 5
            result = provider.recovery_tick(service, "op", tick_id, clock=clock)
        assert result.action == "submitted", (position, result)
        assert result.candidate_id == ("source" if position <= 3 else "alternate")
        assert result.attempt_id is not None
        identities.append(result.attempt_id)
        replay = provider.recovery_tick(service, "op", tick_id, clock=clock)
        assert replay.action == "existing" and replay.attempt_id == result.attempt_id
    assert len(set(identities)) == 6 and len(calls) == 6
    assert (
        provider.recovery_tick(service, "op", "tick-7", clock=clock).reason
        == "retry_budget_exhausted"
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT SUM(count) FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (6,)
        assert db.execute(
            "SELECT COUNT(*),SUM(reserved_minor) FROM retry_scope_reservation"
        ).fetchone() == (5, 100)


def test_exhausted_sunburst_does_not_authorize_flare(tmp_path, monkeypatch):
    service, _, _, _, third, _, sent = setup_cross(tmp_path, monkeypatch)
    source = read_candidate(service.store, "a")
    snap = source.snapshot
    grant = retry_scope.Grant(
        "a", snap.execution_digest, snap.model, snap.endpoint, snap.group, 3
    )
    common = (
        "op",
        snap.task_id,
        snap.revision,
        snap.stage,
        snap.input_digest,
        (grant,),
        6,
        time.time() + 120,
        "fixture",
        "evidence",
    )
    host = retry_scope.HostScope("host-source-only", *common)
    cost = retry_scope.CostScope(
        "cost-source-only", *common, "USD", 100, 20, "fixture-price"
    )
    monkeypatch.setattr(
        retry_scope, "_verify_host_source", lambda db, value: value == host
    )
    monkeypatch.setattr(
        retry_scope, "_verify_cost_source", lambda db, value: value == cost
    )
    monkeypatch.setattr(
        retry_scope, "_verify_price_source", lambda db, value: value == cost
    )
    retry_scope.record_host_scope(service.store, host)
    retry_scope.record_cost_scope(service.store, cost)
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
    result = provider.recovery_tick(service, "op", "not-authorized", clock=time.time)
    assert result.action == "stop" and result.reason == "scope_unavailable_or_ambiguous"
    assert result.candidate_id == "b" and result.attempt_id == third.attempt_id
    assert sent == []
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op'"
        ).fetchone() == (3,)


@pytest.mark.parametrize("interference", ["revoked", "position_occupied"])
def test_candidate_selection_is_rechecked_before_reservation(
    tmp_path, monkeypatch, interference
):
    service, args, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
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
    monkeypatch.setattr(retry_scope, "_verify_revocation_source", lambda *_: True)
    reserve = retry_scope.reserve_eligibility

    def interfere(*request, **kwargs):
        if interference == "revoked":
            retry_scope.revoke_scope(service.store, "host", "after-selection")
        else:
            reserve(
                service,
                *args,
                root="op",
                candidate_id="source",
                host_id="host",
                cost_id="cost",
            )
        return reserve(*request, **kwargs)

    monkeypatch.setattr(retry_scope, "reserve_eligibility", interfere)
    result = provider.recovery_tick(service, "op", "selection-race", clock=time.time)
    assert result.action == "stop" and len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == ((0,) if interference == "revoked" else (1,))
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)
    assert first.attempt_id


@pytest.mark.parametrize(
    "condition",
    [
        "no_evidence",
        "invalid_429",
        "scope_revoked",
        "scope_expired",
        "budget",
        "unknown",
        "processing",
        "no_scope",
    ],
)
def test_tick_stops_safely_without_new_send(tmp_path, monkeypatch, condition):
    host = cost = None
    if condition in {"unknown", "processing", "no_scope"}:
        state = {"unknown": "unknown", "processing": "accepted", "no_scope": "failed"}[
            condition
        ]
        service, args, _first, calls = prepare_retry(tmp_path, monkeypatch, state)
        register_root_candidate(service.store, "op", "source")
    else:
        service, args, _first, calls, host, cost = make_scopes(
            tmp_path, monkeypatch, max_minor=20
        )
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    if condition != "no_evidence":
        monkeypatch.setattr(
            retry_scheduler,
            "_load_observation",
            lambda db, root, attempt: retry_scheduler.RetryObservation(
                "fixture-id",
                {"data": {"task_id": "fixture-id", "status": "FAILURE"}},
                429 if condition == "invalid_429" else 500,
                "invalid" if condition == "invalid_429" else None,
                time.time() - 10,
            ),
        )
    if condition == "scope_revoked":
        monkeypatch.setattr(retry_scope, "_verify_revocation_source", lambda *_: True)
        retry_scope.revoke_scope(service.store, "host", "fixture")
    if condition == "scope_expired":
        assert host is not None and cost is not None
        monkeypatch.setattr(
            retry_scope.time, "time", lambda: max(host.expires_at, cost.expires_at) + 1
        )
    if condition == "budget":
        retry_scope.reserve_eligibility(
            service,
            *args,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
        )
    result = provider.recovery_tick(service, "op", "negative", clock=time.time)
    if condition == "processing":
        assert result.action == "query_original"
    else:
        assert result.action == "stop", (condition, result)
    assert len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)


@pytest.mark.parametrize("date_header", [False, True])
@pytest.mark.parametrize("revoked", [False, True])
def test_tick_retry_after_waits_and_rechecks_scope(
    tmp_path, monkeypatch, date_header, revoked
):
    service, _, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    current = [float(int(time.time()))]

    def clock():
        return current[0]

    started = clock()
    header = (
        format_datetime(datetime.fromtimestamp(started + 7, UTC), usegmt=True)
        if date_header
        else "7"
    )
    with service.store._connect() as db:
        db.execute("DELETE FROM retry_wait WHERE operation_id='op'")
    monkeypatch.setattr(retry_scope.time, "time", clock)
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    monkeypatch.setattr(
        retry_scheduler,
        "_load_observation",
        lambda db, root, attempt: retry_scheduler.RetryObservation(
            "fixture-id",
            {"data": {"task_id": "fixture-id", "status": "FAILURE"}},
            429,
            header,
            started,
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_retry_evidence",
        lambda db, root, attempt, snap, cid: (
            root == "op" and attempt == first.attempt_id
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_retry_authorization",
        lambda db, root, attempt, snap, cid: (
            root == "op" and attempt == first.attempt_id
        ),
    )
    result = provider.recovery_tick(service, "op", "retry-after", clock=clock)
    assert result.action == "waiting" and result.deadline == started + 7
    current[0] += 6
    assert (
        provider.recovery_tick(service, "op", "retry-after", clock=clock).action
        == "waiting"
    )
    if revoked:
        monkeypatch.setattr(retry_scope, "_verify_revocation_source", lambda *_: True)
        retry_scope.revoke_scope(service.store, "host", "during-wait")
    current[0] += 1
    result = provider.recovery_tick(service, "op", "retry-after", clock=clock)
    assert result.action == ("stop" if revoked else "submitted")
    assert len(calls) == (1 if revoked else 2)


def _tick_in_process(
    service,
    host,
    cost,
    first_id,
    ready,
    start,
    output,
    crash=False,
    tick_id="race",
    reserve_barrier=None,
    tick_time=None,
):
    retry_scope._verify_host_source = lambda db, value: value == host
    retry_scope._verify_cost_source = lambda db, value: value == cost
    retry_scope._verify_price_source = lambda db, value: value == cost
    retry_scope._verify_candidate_capability = lambda db, cid, snap: (
        cid == "source" and snap.execution_digest == host.grants[0].digest
    )
    offline_submission._verify_authorization = lambda *_: True
    offline_submission._verify_business_choices = lambda *_: True
    offline_submission._verify_retry_evidence = lambda db, root, attempt, snap, cid: (
        root == "op" and attempt == first_id
    )
    offline_submission._verify_retry_authorization = (
        lambda db, root, attempt, snap, cid: root == "op" and attempt == first_id
    )
    retry_scheduler._combination_verified = lambda *_: True
    retry_scheduler._load_observation = lambda db, root, attempt: (
        retry_scheduler.RetryObservation(
            "fixture-id",
            {"data": {"task_id": "fixture-id", "status": "FAILURE"}},
            500,
            None,
            time.time() - 10,
        )
    )
    if reserve_barrier is not None:
        reserve = retry_scope.reserve_eligibility

        def synchronized_reserve(*args, **kwargs):
            reserve_barrier.wait(timeout=30)
            return reserve(*args, **kwargs)

        retry_scope.reserve_eligibility = synchronized_reserve

    def dispatch(_request):
        with (service.store.path.parent / "fixture-auto-sends").open("a") as handle:
            handle.write("1\n")
        if crash == "during":
            os._exit(24)
        return provider.ProviderResult("failed", task_id="fixture-id")

    offline_submission._offline_dispatch = dispatch
    if crash == "before":
        offline_submission._before_offline_dispatch = lambda: os._exit(23)
    ready.put(True)
    if not start.wait(20):
        output.put(("timeout", ""))
        return
    result = provider.recovery_tick(
        service,
        "op",
        tick_id,
        clock=(time.time if tick_time is None else lambda: tick_time),
    )
    output.put((result.action, result.attempt_id))


@pytest.mark.parametrize("same_tick", [True, False])
def test_two_process_ticks_never_double_send(tmp_path, monkeypatch, same_tick):
    service, _, first, calls, host, cost = make_scopes(tmp_path, monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    barrier = ctx.Barrier(2)
    tick_time = time.time()
    processes = [
        ctx.Process(
            target=_tick_in_process,
            args=(
                service,
                host,
                cost,
                first.attempt_id,
                ready,
                start,
                output,
                False,
                "race" if same_tick else f"race-{index}",
                barrier,
                tick_time,
            ),
        )
        for index in range(2)
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
        assert (
            service.store.path.parent / "fixture-auto-sends"
        ).read_text().splitlines() == ["1"]
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op'"
        ).fetchone() == (2,)
    assert len(calls) == 1


@pytest.mark.parametrize("crash_mode", ["before", "during"])
def test_process_crash_after_intent_never_retries_unknown(
    tmp_path, monkeypatch, crash_mode
):
    service, _, first, calls, host, cost = make_scopes(tmp_path, monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    process = ctx.Process(
        target=_tick_in_process,
        args=(service, host, cost, first.attempt_id, ready, start, output, crash_mode),
    )
    process.start()
    try:
        assert ready.get(timeout=30)
        start.set()
        process.join(timeout=45)
        assert process.exitcode == (23 if crash_mode == "before" else 24)
    finally:
        start.set()
        if process.is_alive():
            process.terminate()
            process.join()
    result = provider.recovery_tick(service, "op", "after-crash", clock=time.time)
    assert result.action == "stop" and result.reason == "original_operation_unresolved"
    with service.store._connect() as db:
        marker = service.store.path.parent / "fixture-auto-sends"
        if crash_mode == "before":
            assert not marker.exists()
        else:
            assert marker.read_text().splitlines() == ["1"]
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT a.upstream_status FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id WHERE c.operation_id='op' ORDER BY c.sequence DESC LIMIT 1"
        ).fetchone() == ("submission_uncertain",)
    assert len(calls) == 1


def test_transport_result_write_failure_preserves_unknown_and_budget(
    tmp_path, monkeypatch
):
    service, _, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
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
    monkeypatch.setattr(
        offline_submission,
        "_verify_retry_evidence",
        lambda db, root, attempt, snap, cid: (
            root == "op" and attempt == first.attempt_id
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_retry_authorization",
        lambda db, root, attempt, snap, cid: (
            root == "op" and attempt == first.attempt_id
        ),
    )
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER fixture_fail_result BEFORE UPDATE OF upstream_status ON attempt WHEN NEW.upstream_status='failed' BEGIN SELECT RAISE(ABORT, 'fixture result write failure'); END"
        )
    result = provider.recovery_tick(service, "op", "result-write", clock=time.time)
    assert result.action == "submitted" and result.attempt_id is not None
    assert len(calls) == 2
    repeat = provider.recovery_tick(service, "op", "result-write", clock=time.time)
    assert repeat.action == "existing" and repeat.attempt_id == result.attempt_id
    later = provider.recovery_tick(service, "op", "new-tick", clock=time.time)
    assert later.action == "stop" and later.reason == "original_operation_unresolved"
    assert len(calls) == 2
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (result.attempt_id,),
        ).fetchone() == ("submission_uncertain",)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT COUNT(*),SUM(reserved_minor) FROM retry_scope_reservation"
        ).fetchone() == (1, 20)
