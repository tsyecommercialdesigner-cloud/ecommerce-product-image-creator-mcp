"""Deterministic replay observations at the local intent boundary."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from ecommerce_product_image_creator import first_authorization, offline_submission
from integration.test_cross_candidate_submission import setup_cross
from integration.test_offline_submission import setup_context


def _state(store, operation_id, root):
    with store._connect() as db:
        row = db.execute(
            "SELECT o.attempt_id,a.upstream_status FROM operation o JOIN attempt a ON a.attempt_id=o.attempt_id WHERE o.operation_id=?",
            (operation_id,),
        ).fetchone()
        eligibility = db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        chain = db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id=? AND attempt_id=?",
            (root, row[0]),
        ).fetchone()[0]
        budget = db.execute(
            "SELECT SUM(count) FROM attempt_counter WHERE operation_id=?", (root,)
        ).fetchone()[0]
        return row, eligibility, chain, budget


def _interleave(service, args, monkeypatch, *, retry_of=None):
    entered = Event()
    release = Event()
    calls = []

    def pause_after_intent():
        entered.set()
        if not release.wait(20):
            raise AssertionError("replay did not reach the barrier")

    def dispatch(request):
        calls.append(request)
        return offline_submission.ProviderResult("failed", task_id="fixture-upstream")

    monkeypatch.setattr(
        offline_submission, "_after_intent_recorded", pause_after_intent
    )
    monkeypatch.setattr(offline_submission, "_offline_dispatch", dispatch)
    operation_id = args[6]
    root = retry_of or operation_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(
            offline_submission.submit_offline,
            service,
            *args,
            retry_of=retry_of,
            require_first_authorizations=retry_of is None,
        )
        try:
            assert entered.wait(20)
            before = _state(service.store, operation_id, root)
            replay = pool.submit(
                offline_submission.submit_offline,
                service,
                *args,
                retry_of=retry_of,
                require_first_authorizations=retry_of is None,
            ).result(timeout=20)
            assert replay.replayed and replay.state == "unknown"
            assert before[0] == (replay.attempt_id, "submission_uncertain")
            assert before[1:3] == ((1,), 1)
            assert calls == []
        finally:
            release.set()
        completed = original.result(timeout=20)
    assert completed.attempt_id == replay.attempt_id and completed.state == "failed"
    assert len(calls) == 1
    after = _state(service.store, operation_id, root)
    assert after[0] == (completed.attempt_id, "failed")
    assert after[1:] == before[1:]
    again = offline_submission.submit_offline(
        service, *args, retry_of=retry_of, require_first_authorizations=retry_of is None
    )
    assert (
        again.replayed
        and again.attempt_id == completed.attempt_id
        and again.state == "failed"
    )
    assert _state(service.store, operation_id, root) == after
    assert len(calls) == 1
    return completed


def test_first_attempt_replay_preserves_independent_authorization(
    tmp_path, monkeypatch
):
    service, args = setup_context(tmp_path)
    snapshot = args[4]
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        first_authorization, "_verify_source", lambda _db, e: e.source == "fixture"
    )
    with service.store._connect() as db:
        for kind in ("host", "fee"):
            first_authorization.record(
                db,
                first_authorization.FirstAuthorization(
                    kind,
                    kind,
                    "op",
                    snapshot.task_id,
                    snapshot.revision,
                    snapshot.stage,
                    snapshot.execution_digest,
                    snapshot.model,
                    snapshot.endpoint,
                    snapshot.group,
                    snapshot.operation_scope,
                    time.time() + 120,
                    "fixture",
                    kind,
                ),
            )
    result = _interleave(service, args, monkeypatch)
    with service.store._connect() as db:
        rows = db.execute(
            "SELECT authorization_id,operation_id,attempt_id FROM first_authorization_consumption ORDER BY authorization_id"
        ).fetchall()
        assert rows == [
            ("fee", "op", result.attempt_id),
            ("host", "op", result.attempt_id),
        ]
        assert db.execute("SELECT COUNT(*) FROM attempt").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM operation").fetchone() == (1,)


def test_flare_replay_before_and_after_terminal_write(tmp_path, monkeypatch):
    service, target, _, _, _, _, _ = setup_cross(tmp_path, monkeypatch)
    result = _interleave(service, target, monkeypatch, retry_of="op")
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM attempt WHERE attempt_id=?", (result.attempt_id,)
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM operation WHERE operation_id='flare-request'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM candidate_attempt WHERE attempt_id=?",
            (result.attempt_id,),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT SUM(count) FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (4,)
