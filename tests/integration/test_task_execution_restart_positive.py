"""Positive scoped retry after the creating process exits, with fake clocks."""

import multiprocessing
import time
from pathlib import Path

import pytest


def _create(path, output):
    from ecommerce_product_image_creator import faults, offline_submission, retry_scope
    from ecommerce_product_image_creator.execution_candidates import (
        register_root_candidate,
    )
    from integration.test_offline_submission import setup_context

    mp = pytest.MonkeyPatch()
    try:
        service, args = setup_context(Path(path))
        mp.setattr(offline_submission, "_verify_authorization", lambda *_: True)
        mp.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
        calls = []
        mp.setattr(offline_submission, "_offline_dispatch", lambda _: calls.append(1))

        def transient(_):
            raise faults.TransientLocalError("isolated transient failure")

        mp.setattr(offline_submission, "_pre_dispatch_validate", transient)
        first = offline_submission.submit_offline(service, *args)
        assert first.state == "unknown" and not calls
        candidate = register_root_candidate(service.store, "op", "source")
        snap = candidate.snapshot
        grant = retry_scope.Grant(
            "source", snap.execution_digest, snap.model, snap.endpoint, snap.group, 3
        )
        common = (
            "op",
            snap.task_id,
            snap.revision,
            snap.stage,
            snap.input_digest,
            (grant,),
            3,
            time.time() + 120,
            "fixture",
            "evidence",
        )
        host = retry_scope.HostScope("host", *common)
        cost = retry_scope.CostScope("cost", *common, "USD", 100, 20, "fixture-price")
        mp.setattr(retry_scope, "_verify_host_source", lambda db, value: value == host)
        mp.setattr(retry_scope, "_verify_cost_source", lambda db, value: value == cost)
        mp.setattr(retry_scope, "_verify_price_source", lambda db, value: value == cost)
        mp.setattr(
            retry_scope,
            "_verify_candidate_capability",
            lambda db, cid, snapshot: cid == "source" and snapshot == snap,
        )
        retry_scope.record_host_scope(service.store, host)
        retry_scope.record_cost_scope(service.store, cost)
        with service.store._connect() as db:
            started = db.execute(
                "SELECT recorded_at FROM attempt_transport_evidence WHERE attempt_id=? AND phase='pre_send_failure'",
                (first.attempt_id,),
            ).fetchone()[0]
            assert db.execute(
                "SELECT category FROM attempt_fault_evidence WHERE attempt_id=?",
                (first.attempt_id,),
            ).fetchone() == ("transient_local",)
        service.store.record_retry_wait("op", first.attempt_id, started, None, 0)
        snap = args[4]
        with service.store._connect() as db:
            started, deadline = db.execute(
                "SELECT started,deadline FROM retry_wait WHERE operation_id='op'"
            ).fetchone()
        output.put(
            (
                service,
                host,
                cost,
                first.attempt_id,
                (snap.task_id, snap.revision, snap.stage, "op"),
                started,
                deadline,
                len(calls),
            )
        )
    finally:
        mp.undo()


def _resume(
    service,
    host,
    cost,
    first,
    identity: tuple[str, int, str, str],
    started,
    deadline,
    output,
):
    from ecommerce_product_image_creator import (
        offline_submission,
        provider,
        retry_scheduler,
        retry_scope,
        tasks,
    )
    from ecommerce_product_image_creator.send_evidence import verified_pre_send
    from ecommerce_product_image_creator.storage import Store

    service.store = Store(service.store.path)
    retry_scope._verify_host_source = lambda db, value: value == host
    retry_scope._verify_cost_source = lambda db, value: value == cost
    retry_scope._verify_price_source = lambda db, value: value == cost
    retry_scope._verify_candidate_capability = lambda db, cid, snap: (
        cid == "source" and snap.execution_digest == host.grants[0].digest
    )
    offline_submission._verify_authorization = lambda *_: True
    offline_submission._verify_business_choices = lambda *_: True
    offline_submission._verify_retry_evidence = lambda db, root, attempt, snap, cid: (
        root == "op"
        and attempt == first
        and verified_pre_send(
            db, root, attempt, snap.task_id, snap.revision, snap.execution_digest
        )
    )
    offline_submission._verify_retry_authorization = (
        lambda db, root, attempt, snap, cid: root == "op" and attempt == first
    )
    retry_scheduler._combination_verified = lambda *_: True
    sends = []
    offline_submission._offline_dispatch = lambda request: (
        sends.append(1) or provider.ProviderResult("failed", task_id="fixture-id")
    )
    before = tasks.read_task_execution(service, *identity)
    waiting = tasks.advance_task_execution(
        service, *identity, "resume", clock=lambda: started + 1
    )
    with service.store._connect() as db:
        reservation_before = db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone()[0]
    retry_scope.time.time = lambda: deadline
    advanced = tasks.advance_task_execution(
        service, *identity, "resume", clock=lambda: deadline
    )
    with service.store._connect() as db:
        counter = db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone()[0]
        reservation = db.execute(
            "SELECT operation_id,attempt_id FROM retry_scope_reservation"
        ).fetchall()
        consumed = (
            db.execute(
                "SELECT operation_id,consumed FROM local_eligibility WHERE operation_id=?",
                (reservation[0][0],),
            ).fetchone()
            if reservation
            else None
        )
        operation = (
            db.execute(
                "SELECT attempt_id FROM operation WHERE operation_id=?",
                (reservation[0][0],),
            ).fetchone()
            if reservation
            else None
        )
    output.put(
        (
            before["budget"].total_used,
            before["wait"]["deadline"],
            waiting["action"],
            len(waiting["state"]["attempts"]),
            reservation_before,
            advanced["action"],
            advanced["reason"],
            advanced["attempt_id"],
            len(advanced["state"]["attempts"]),
            len(sends),
            counter,
            reservation,
            consumed,
            operation,
        )
    )


def test_task_retry_after_two_process_restart(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    creator = ctx.Process(target=_create, args=(str(tmp_path), output))
    creator.start()
    service, host, cost, first, identity, started, deadline, first_sends = output.get(
        timeout=60
    )
    creator.join(timeout=30)
    assert creator.exitcode == 0 and first_sends == 0
    resumed = ctx.Process(
        target=_resume,
        args=(service, host, cost, first, identity, started, deadline, output),
    )
    resumed.start()
    values = output.get(timeout=60)
    resumed.join(timeout=30)
    assert resumed.exitcode == 0
    (
        used,
        saved_deadline,
        waiting,
        old_count,
        before_reservations,
        action,
        reason,
        attempt,
        count,
        sends,
        counter,
        reservations,
        consumed,
        operation,
    ) = values
    assert used == 1 and saved_deadline == deadline
    assert waiting == "waiting" and old_count == 1 and before_reservations == 0
    assert (
        action == "submitted"
        and attempt != first
        and count == 2
        and sends == 1
        and counter == 2
    ), reason
    assert len(reservations) == 1 and reservations[0][1] == attempt
    assert consumed == (reservations[0][0], 1) and operation == (attempt,)
