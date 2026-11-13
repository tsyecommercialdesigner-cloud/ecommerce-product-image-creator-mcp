"""Scoped entry exercises the actual scheduler, reservation and submission."""

import sqlite3
import time
from dataclasses import replace

import pytest

from ecommerce_product_image_creator import retry_scheduler, retry_scope
from ecommerce_product_image_creator.execution_candidates import register_root_candidate
from integration.test_attempt002 import prepare_retry
from integration.test_cross_candidate_submission import setup_cross
from integration.test_retry_scopes import make_scopes


def test_scoped_entry_two_attempts_and_replay(tmp_path, monkeypatch):
    service, args, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
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

    def invoke(request):
        return retry_scheduler.scoped_retry_once(
            time.time,
            service,
            *request,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
        )

    second = invoke(args)
    assert second.action == "submitted" and second.result is not None
    assert second.result.attempt_id != first.attempt_id
    replay = invoke(args)
    assert replay.action == "existing" and replay.result is not None
    assert replay.result.attempt_id == second.result.attempt_id
    service.store.record_retry_wait(
        "op", second.result.attempt_id, time.time() - 10, None, 500
    )
    with service.store._connect() as db:
        db.execute(
            "INSERT INTO fixture_retry_evidence VALUES(?,?,?)",
            ("op", second.result.attempt_id, args[5].confirmation_id),
        )
    reused = (*args[:6], "reused-confirmation-op", args[7])
    with pytest.raises(
        sqlite3.IntegrityError, match="local_eligibility.confirmation_id"
    ):
        retry_scope.reserve_eligibility(
            service,
            *reused,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
        )
    assert invoke(reused).action == "stop"
    with service.store._connect() as db:
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id=?", (args[6],)
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation WHERE operation_id='reused-confirmation-op'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)
    assert len(calls) == 2
    confirmation = replace(args[5], confirmation_id="scoped-third")
    third_args = (*args[:5], confirmation, "scoped-third-op", args[7])
    with service.store._connect() as db:
        for table in ("fixture_retry_evidence", "fixture_retry_authorization"):
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?)",
                ("op", second.result.attempt_id, confirmation.confirmation_id),
            )
    third = invoke(third_args)
    assert third.action == "submitted" and third.result is not None
    assert (
        len({first.attempt_id, second.result.attempt_id, third.result.attempt_id}) == 3
    )
    assert len(calls) == 3
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*), SUM(reserved_minor) FROM retry_scope_reservation"
        ).fetchone() == (2, 40)
        assert db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op'"
        ).fetchone() == (3,)
    assert retry_scope.read_reservation(service.store, args[6])["actual_cost"] is None
    fourth = (
        *args[:5],
        replace(args[5], confirmation_id="fourth"),
        "fourth-op",
        args[7],
    )
    assert invoke(fourth).action == "stop"
    assert len(calls) == 3
    monkeypatch.setattr(retry_scope, "_verify_revocation_source", lambda *_: True)
    retry_scope.revoke_scope(service.store, "cost", "revoked-after-use")
    after_revocation = invoke(args)
    assert after_revocation.action == "existing" and after_revocation.result is not None
    assert after_revocation.result.attempt_id == second.result.attempt_id
    assert after_revocation.result.state == second.result.state
    assert len(calls) == 3


def test_entry_rejects_exhausted_risk_even_with_remaining_attempt_budget(
    tmp_path, monkeypatch
):
    service, args, _, calls, _, _ = make_scopes(tmp_path, monkeypatch, max_minor=20)
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

    def invoke(request):
        return retry_scheduler.scoped_retry_once(
            time.time,
            service,
            *request,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
        )

    second = invoke(args)
    assert second.action == "submitted" and second.result is not None
    service.store.record_retry_wait(
        "op", second.result.attempt_id, time.time() - 10, None, 500
    )
    confirmation = replace(args[5], confirmation_id="risk-exhausted")
    third = (*args[:5], confirmation, "risk-exhausted-op", args[7])
    with service.store._connect() as db:
        db.execute(
            "INSERT INTO fixture_retry_evidence VALUES(?,?,?)",
            ("op", second.result.attempt_id, confirmation.confirmation_id),
        )
    assert invoke(third).action == "stop"
    assert len(calls) == 2
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*), SUM(reserved_minor) FROM retry_scope_reservation"
        ).fetchone() == (1, 20)
        assert db.execute(
            "SELECT COUNT(*) FROM local_eligibility WHERE operation_id='risk-exhausted-op'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)


@pytest.mark.parametrize(
    "host_id,cost_id",
    [("missing", "cost"), ("host", "missing"), ("missing", "missing")],
)
def test_missing_independent_scopes_never_dispatch(
    tmp_path, monkeypatch, host_id, cost_id
):
    service, args, _, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    result = retry_scheduler.scoped_retry_once(
        time.time,
        service,
        *args,
        root="op",
        candidate_id="source",
        host_id=host_id,
        cost_id=cost_id,
    )
    assert result.action == "stop" and len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)


def test_historical_root_and_wrong_candidate_cannot_use_entry(tmp_path, monkeypatch):
    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)
    register_root_candidate(service.store, "op", "source")
    for candidate in ("source", "missing"):
        result = retry_scheduler.scoped_retry_once(
            time.time,
            service,
            *args,
            root="op",
            candidate_id=candidate,
            host_id="host",
            cost_id="cost",
        )
        assert result.action == "stop"
    with pytest.raises(TypeError):
        retry_scheduler.scoped_retry_once(
            time.time,
            service,
            *args,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
            require_retry_scope=False,  # pyright: ignore[reportCallIssue]
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "scenario",
    ["revoked", "expired", "wait", "wrong_digest", "wrong_revision", "unknown"],
)
def test_entry_fails_closed_before_new_attempt(tmp_path, monkeypatch, scenario):
    service, args, _first, calls, host, cost = make_scopes(
        tmp_path,
        monkeypatch,
        first_state="unknown" if scenario == "unknown" else "failed",
    )
    clock = time.time
    if scenario == "revoked":
        monkeypatch.setattr(retry_scope, "_verify_revocation_source", lambda *_: True)
        retry_scope.revoke_scope(service.store, "host", "fixture-revocation")
    elif scenario == "expired":
        monkeypatch.setattr(
            retry_scope.time, "time", lambda: max(host.expires_at, cost.expires_at) + 1
        )
    elif scenario == "wait":
        with service.store._connect() as db:
            db.execute(
                "UPDATE retry_wait SET deadline=? WHERE operation_id='op'",
                (time.time() + 60,),
            )
    elif scenario == "wrong_digest":
        snap = args[4]
        changed = type(snap).create(
            args[1],
            args[3],
            snap.stage,
            "other-model",
            snap.endpoint,
            snap.group,
            references=snap.references,
            operation_scope=snap.operation_scope,
        )
        args = (*args[:4], changed, *args[5:])
    elif scenario == "wrong_revision":
        args = (args[0], replace(args[1], revision=args[1].revision + 1), *args[2:])
    result = retry_scheduler.scoped_retry_once(
        clock,
        service,
        *args,
        root="op",
        candidate_id="source",
        host_id="host",
        cost_id="cost",
    )
    assert result.action == "stop" and len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op'"
        ).fetchone() == (1,)


def test_scoped_entry_uses_explicit_alternate_candidate(tmp_path, monkeypatch):
    service, args, _, _, _, candidate, sent = setup_cross(tmp_path, monkeypatch)
    snap = candidate.snapshot
    grant = retry_scope.Grant(
        candidate.candidate_id,
        snap.execution_digest,
        snap.model,
        snap.endpoint,
        snap.group,
        3,
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
    host = retry_scope.HostScope("alternate-host", *common)
    cost = retry_scope.CostScope(
        "alternate-cost", *common, "USD", 50, 25, "fixture-price"
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
    monkeypatch.setattr(
        retry_scope,
        "_verify_candidate_capability",
        lambda db, cid, snapshot: cid == candidate.candidate_id and snapshot == snap,
    )
    retry_scope.record_host_scope(service.store, host)
    retry_scope.record_cost_scope(service.store, cost)
    result = retry_scheduler.scoped_retry_once(
        time.time,
        service,
        *args,
        root="op",
        candidate_id=candidate.candidate_id,
        host_id=host.scope_id,
        cost_id=cost.scope_id,
    )
    assert result.action == "submitted" and result.result is not None
    assert len(sent) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT sequence,execution_digest FROM operation_attempt WHERE attempt_id=?",
            (result.result.attempt_id,),
        ).fetchone() == (4, snap.execution_digest)
        assert db.execute(
            "SELECT reserved_minor,attempt_id FROM retry_scope_reservation WHERE operation_id=?",
            (args[6],),
        ).fetchone() == (25, result.result.attempt_id)
