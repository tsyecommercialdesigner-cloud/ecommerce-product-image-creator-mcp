import socket
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ecommerce_product_image_creator import (
    offline_submission,
    original_query,
    recovery,
    risk_reservation,
    send_evidence,
)

from .test_linked_atomic_submission import linked_context  # noqa: F401


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.mark.parametrize("fail_stop_write", [False, True])
def test_stop_before_entered_and_evidence_write_failure(
    linked_context,  # noqa: F811
    monkeypatch,
    fail_stop_write,
):
    service, linked, _, calls = linked_context
    boundary = offline_submission._record_transport_boundary

    def fail_before(service, operation_id, attempt_id, phase, **kwargs):
        if operation_id == "linked-op" and phase == "entered":
            raise OSError("injected")
        return boundary(service, operation_id, attempt_id, phase, **kwargs)

    monkeypatch.setattr(offline_submission, "_record_transport_boundary", fail_before)
    if fail_stop_write:

        def fail_write(*_args):
            raise OSError("injected")

        monkeypatch.setattr(send_evidence, "_record_linked_stop", fail_write)
    result = offline_submission.submit_offline(
        service, *linked, risk_authorization_id="risk-1"
    )
    assert result.state == "unknown" and calls == [1]
    with service.store._connect() as db:
        assert send_evidence.verified_linked_stop(
            db,
            "linked-op",
            result.attempt_id,
            linked[0].task_id,
            1,
            linked[4].execution_digest,
        ) == (None if fail_stop_write else "stopped_before_entered")


@pytest.mark.parametrize(
    "window",
    [
        "transaction",
        "intent",
        "entered",
        "final_check",
        "dispatch",
        "persisted",
        "observation",
    ],
)
def test_subprocess_crash_preserves_single_commit(tmp_path, window):
    script = r"""
import os, sys, socket, pickle
from pathlib import Path
import pytest
from tests.integration.test_linked_atomic_submission import linked_context
from tests.integration.authorized_query_fixture import install
from ecommerce_product_image_creator import offline_submission, original_query, recovery
root = Path(sys.argv[1]); root.mkdir()
window = sys.argv[2]
patch = pytest.MonkeyPatch()
install(patch)
def denied(*args, **kwargs):
    raise AssertionError("network forbidden")
patch.setattr(socket.socket, "connect", denied)
patch.setattr(socket, "create_connection", denied)
service, args, original, calls = linked_context.__wrapped__(root, patch)
with (root / "args.pkl").open("wb") as file:
    pickle.dump(args, file)
fee = offline_submission._verify_linked_fee
def fee_check(*args):
    fee_check.calls += 1
    return fee(*args)
fee_check.calls = 0
offline_submission._verify_linked_fee = fee_check
if window == "transaction":
    offline_submission._before_intent_commit = lambda: os._exit(71)
if window == "intent":
    offline_submission._after_intent_recorded = lambda: os._exit(71)
if window == "final_check":
    offline_submission._before_offline_dispatch = lambda: os._exit(71)
if window == "observation":
    original_query._verify_original_binding = lambda db, identity: identity.upstream_id == "fixture-id" and identity.attempt_id == original.attempt_id
    recovery._verify_observation_source = lambda *args: True
    original_query._query_adapter = lambda identity: {"data": {"task_id": identity.upstream_id, "status": "SUCCESS"}}
    def observe_then_crash():
        with service.store._connect() as db:
            db.execute("UPDATE attempt SET upstream_id='fixture-id' WHERE attempt_id=?", (original.attempt_id,))
        result = recovery.recover_once(service.store, "op", "source-candidate", args[0].task_id, 1, "form", original.attempt_id, args[4].execution_digest)
        if result.state != "succeeded":
            os._exit(72)
        os._exit(71)
    offline_submission._after_intent_recorded = observe_then_crash
if window == "entered":
    boundary = offline_submission._record_transport_boundary
    def crash_boundary(*args, **kwargs):
        boundary(*args, **kwargs)
        if args[1] == "linked-op" and args[3] == "entered":
            os._exit(71)
    offline_submission._record_transport_boundary = crash_boundary
def dispatch(_request):
    (root / "fake-called").write_text("called")
    if window == "dispatch":
        os._exit(71)
    return offline_submission.ProviderResult("accepted", task_id="fake-id")
offline_submission._offline_dispatch = dispatch
offline_submission.submit_offline(service, *args, risk_authorization_id="risk-1")
os._exit(0)
"""
    root = tmp_path / window
    child = subprocess.run(
        [sys.executable, "-c", script, str(root), window],
        check=False,
        capture_output=True,
        text=True,
    )
    assert child.returncode == (0 if window == "persisted" else 71), child.stderr
    with sqlite3.connect(root / "state.sqlite") as db:
        count = db.execute("SELECT count(*) FROM risk_consumption").fetchone()[0]
        assert count == (0 if window == "transaction" else 1)
        assert (
            db.execute(
                "SELECT count(*) FROM operation WHERE operation_id='linked-op'"
            ).fetchone()[0]
            == count
        )
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='linked-op'"
        ).fetchone() == (count,)
        linked_attempt = db.execute(
            "SELECT linked_attempt FROM risk_consumption WHERE authorization_id='risk-1'"
        ).fetchone()
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)
        if count:
            row = db.execute(
                "SELECT a.upstream_status,e.phase FROM risk_consumption r JOIN attempt a ON a.attempt_id=r.linked_attempt LEFT JOIN attempt_transport_evidence e ON e.attempt_id=a.attempt_id"
            ).fetchone()
            assert row == (
                ("accepted", "entered")
                if window == "persisted"
                else (
                    "submission_uncertain",
                    None if window in {"intent", "observation"} else "entered",
                )
            )
        if window == "observation":
            assert db.execute(
                "SELECT remote_state FROM recovery_observation"
            ).fetchone() == ("succeeded",)
    assert (root / "fake-called").exists() == (window in {"dispatch", "persisted"})
    if count:
        replay_script = r"""
import pickle, sys, socket
from pathlib import Path
from ecommerce_product_image_creator.offline_submission import submit_offline
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.tasks import LocalEligibility
root = Path(sys.argv[1])
def denied(*args, **kwargs):
    raise AssertionError("network forbidden")
socket.socket.connect = denied
socket.create_connection = denied
with (root / "args.pkl").open("rb") as file:
    args = pickle.load(file)
service = LocalEligibility(Store(root / "state.sqlite"), root, ResourceBudget(100000, 10000, 100000))
result = submit_offline(service, *args, risk_authorization_id="risk-1")
print(result.attempt_id)
"""
        replay = subprocess.run(
            [sys.executable, "-c", replay_script, str(root)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert replay.returncode == 0, replay.stderr
        assert replay.stdout.strip() == linked_attempt[0]


def test_original_success_observation_stops_pending_linked_send(
    linked_context,  # noqa: F811
    monkeypatch,
):
    service, linked, original, calls = linked_context
    from .authorized_query_fixture import install

    install(monkeypatch)
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, identity: (
            identity.upstream_id == "fixture-id"
            and identity.attempt_id == original.attempt_id
        ),
    )
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda identity: {
            "data": {"task_id": identity.upstream_id, "status": "SUCCESS"}
        },
    )
    resolutions = []

    def original_recovers():
        # Isolated fixture simulates the later arrival of an upstream ID; no
        # production mechanism for attesting this handoff exists yet.
        with service.store._connect() as db:
            db.execute(
                "UPDATE attempt SET upstream_id=? WHERE attempt_id=?",
                ("fixture-id", original.attempt_id),
            )
        resolutions.append(
            recovery.recover_once(
                service.store,
                "op",
                "source-candidate",
                linked[0].task_id,
                1,
                "form",
                original.attempt_id,
                linked[4].execution_digest,
            )
        )

    monkeypatch.setattr(offline_submission, "_after_intent_recorded", original_recovers)
    result = offline_submission.submit_offline(
        service, *linked, risk_authorization_id="risk-1"
    )
    assert resolutions[0].state == "succeeded"
    assert result.state == "unknown" and calls == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (original.attempt_id,),
        ).fetchone() == ("unknown",)
        assert db.execute(
            "SELECT remote_state FROM recovery_observation WHERE attempt_id=?",
            (original.attempt_id,),
        ).fetchone() == ("succeeded",)
        assert (
            send_evidence.verified_linked_stop(
                db,
                "linked-op",
                result.attempt_id,
                linked[0].task_id,
                1,
                linked[4].execution_digest,
            )
            == "entered_then_stopped"
        )
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)


def test_original_success_after_dispatch_preserves_both_histories(
    linked_context,  # noqa: F811
    monkeypatch,
):
    service, linked, original, calls = linked_context
    from .authorized_query_fixture import install

    install(monkeypatch)
    linked_result = offline_submission.submit_offline(
        service, *linked, risk_authorization_id="risk-1"
    )
    assert calls == [1, 1] and linked_result.state == "unknown"
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_id='fixture-id' WHERE attempt_id=?",
            (original.attempt_id,),
        )
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, identity: (
            identity.upstream_id == "fixture-id"
            and identity.attempt_id == original.attempt_id
        ),
    )
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda identity: {
            "data": {"task_id": identity.upstream_id, "status": "SUCCESS"}
        },
    )
    resolution = recovery.recover_once(
        service.store,
        "op",
        "source-candidate",
        linked[0].task_id,
        1,
        "form",
        original.attempt_id,
        linked[4].execution_digest,
    )
    assert resolution.state == "succeeded" and calls == [1, 1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (original.attempt_id,),
        ).fetchone() == ("unknown",)
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (linked_result.attempt_id,),
        ).fetchone() == ("unknown",)
        assert db.execute("SELECT count(*) FROM risk_consumption").fetchone() == (1,)
        assert (
            send_evidence.verified_linked_stop(
                db,
                "linked-op",
                linked_result.attempt_id,
                linked[0].task_id,
                1,
                linked[4].execution_digest,
            )
            is None
        )


def test_revocation_competes_with_consumption_in_defined_order(
    linked_context,  # noqa: F811
    monkeypatch,
):
    service, linked, _, calls = linked_context
    inside = threading.Event()
    release = threading.Event()
    revoked = threading.Event()
    fee = offline_submission._verify_linked_fee
    monkeypatch.setattr(risk_reservation, "_verify_revocation_source", lambda *_: True)

    def checked_fee(*args):
        if not inside.is_set():
            inside.set()
            assert release.wait(10)
        return fee(*args)

    def after_intent():
        assert revoked.wait(10)

    def revoke():
        try:
            risk_reservation.revoke(service.store, "risk-1", "concurrent-revocation")
        finally:
            revoked.set()

    monkeypatch.setattr(offline_submission, "_verify_linked_fee", checked_fee)
    monkeypatch.setattr(offline_submission, "_after_intent_recorded", after_intent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        submission = pool.submit(
            offline_submission.submit_offline,
            service,
            *linked,
            risk_authorization_id="risk-1",
        )
        assert inside.wait(10)
        revocation = pool.submit(revoke)
        release.set()
        result = submission.result(timeout=15)
        revocation.result(timeout=15)
    assert result.state == "unknown" and calls == [1]
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM risk_consumption").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM risk_reservation_revocation"
        ).fetchone() == (1,)
        assert (
            send_evidence.verified_linked_stop(
                db,
                "linked-op",
                result.attempt_id,
                linked[0].task_id,
                1,
                linked[4].execution_digest,
            )
            == "entered_then_stopped"
        )


def test_revocation_after_transport_boundary_does_not_claim_cancellation(
    linked_context,  # noqa: F811
    monkeypatch,
):
    service, linked, _, calls = linked_context
    invoked = threading.Event()
    monkeypatch.setattr(risk_reservation, "_verify_revocation_source", lambda *_: True)

    def dispatch(_request):
        calls.append(1)
        invoked.set()
        raise TimeoutError("uncertain")

    def revoke_after_entering_transport():
        assert invoked.wait(10)
        risk_reservation.revoke(service.store, "risk-1", "after-transport")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", dispatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        revocation = pool.submit(revoke_after_entering_transport)
        result = offline_submission.submit_offline(
            service, *linked, risk_authorization_id="risk-1"
        )
        revocation.result(timeout=15)
    assert result.state == "unknown" and calls == [1, 1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM risk_reservation_revocation"
        ).fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM risk_consumption").fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='linked-op'"
        ).fetchone() == (1,)
        assert (
            send_evidence.verified_linked_stop(
                db,
                "linked-op",
                result.attempt_id,
                linked[0].task_id,
                1,
                linked[4].execution_digest,
            )
            is None
        )
