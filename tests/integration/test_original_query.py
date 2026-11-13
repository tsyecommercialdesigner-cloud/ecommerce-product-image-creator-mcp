"""Isolated original-operation query; never calls generation transport."""

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from ecommerce_product_image_creator import offline_submission, original_query
from ecommerce_product_image_creator.execution_candidates import register_root_candidate

from .test_offline_submission import setup_context


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def setup_query(tmp_path, monkeypatch, *, authorized_legacy=True):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    sends = []
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: (
            sends.append(1)
            or offline_submission.ProviderResult("accepted", task_id="fixture-upstream")
        ),
    )
    result = offline_submission.submit_offline(service, *args)
    register_root_candidate(service.store, "op", "a")
    if authorized_legacy:
        from .authorized_query_fixture import install

        install(monkeypatch)
    return service, args, result, sends


def test_query_records_evolution_without_new_attempt_or_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    from ecommerce_product_image_creator import recovery

    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, identity: (
            identity.upstream_id == "fixture-upstream"
            and identity.attempt_id == attempt.attempt_id
        ),
    )
    states = iter(("IN_PROGRESS", "SUCCESS", "IN_PROGRESS", "SUCCESS"))
    calls = []

    def query(identity):
        calls.append(identity.upstream_id)
        return {"data": {"task_id": identity.upstream_id, "status": next(states)}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    one = original_query.query_once(service.store, *identity)
    two = original_query.query_once(service.store, *identity)
    three = original_query.query_once(service.store, *identity)
    four = original_query.query_once(service.store, *identity)
    assert [one.state, two.state, three.state, four.state] == [
        "running",
        "succeeded",
        "conflict",
        "conflict",
    ]
    assert calls == ["fixture-upstream"] * 3 and len(sends) == 1
    history = original_query.read_observations(
        service.store, "op", "a", attempt.attempt_id
    )
    assert [entry.state for entry in history] == [
        "running",
        "succeeded",
        "conflict",
    ]
    assert [entry.previous_id for entry in history] == [
        None,
        *(entry.observation_id for entry in history[:-1]),
    ]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("accepted",)
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        present = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='query_invocation'"
        ).fetchone()
        calls_recorded = (
            db.execute(
                "SELECT invocation_id,observation_id,status FROM query_invocation ORDER BY rowid"
            ).fetchall()
            if present
            else []
        )
        assert len(calls_recorded) == 3
        assert len({item[0] for item in calls_recorded}) == 3
        assert all(
            item[1] is not None and item[2] == "observed" for item in calls_recorded
        )
        assert db.execute("SELECT count(*) FROM recovery_observation").fetchone() == (
            3,
        )
    code = "import sqlite3,sys; d=sqlite3.connect(sys.argv[1]); print(d.execute('SELECT count(*) FROM recovery_observation').fetchone()[0])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "3"


@pytest.mark.parametrize(
    "status,expected",
    [("NOT_START", "pending"), ("FAILURE", "failed"), ("unrecognized", "unknown")],
)
def test_query_classification_never_submits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, expected: str
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda identity: {"data": {"task_id": identity.upstream_id, "status": status}},
    )
    result = original_query.query_once(
        service.store,
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == expected and len(sends) == 1
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)


@pytest.mark.parametrize("window", ["during_query", "after_response"])
def test_crash_before_observation_persistence_never_resubmits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: str
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)

    def crash(*_):
        raise SystemExit("isolated crash")

    if window == "during_query":
        monkeypatch.setattr(original_query, "_query_adapter", crash)
    else:
        monkeypatch.setattr(
            original_query,
            "_query_adapter",
            lambda identity: {
                "data": {"task_id": identity.upstream_id, "status": "SUCCESS"}
            },
        )
        monkeypatch.setattr(original_query, "_after_query_received", crash)
    with pytest.raises(SystemExit):
        original_query.query_once(
            service.store,
            "op",
            "a",
            args[0].task_id,
            1,
            attempt.attempt_id,
            args[4].execution_digest,
        )
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM recovery_observation").fetchone() == (
            0,
        )
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        state = db.execute("SELECT status FROM query_invocation").fetchone()
        assert state == (
            ("entered" if window == "during_query" else "response_received"),
        )
        assert db.execute("SELECT count(*) FROM query_billing_event").fetchone() == (0,)
    from ecommerce_product_image_creator import query_usage

    summary = query_usage.summarize_query_evidence(
        service.store, "op", "a", attempt.attempt_id
    )
    assert summary["cost_status"] == "unknown"
    assert len(sends) == 1


def test_default_and_wrong_identity_never_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        original_query, "_query_adapter", lambda identity: calls.append(identity)
    )
    result = original_query.query_once(
        service.store,
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == "unverified" and calls == []
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    for task, revision, attempt_id, digest in [
        ("other", 1, attempt.attempt_id, args[4].execution_digest),
        (args[0].task_id, 2, attempt.attempt_id, args[4].execution_digest),
        (args[0].task_id, 1, "other", args[4].execution_digest),
        (args[0].task_id, 1, attempt.attempt_id, "wrong"),
    ]:
        assert (
            original_query.query_once(
                service.store, "op", "a", task, revision, attempt_id, digest
            ).state
            == "unverified"
        )
    assert calls == [] and len(sends) == 1


def test_query_failure_preserves_original_and_never_generates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)

    def timeout(_):
        raise TimeoutError("sensitive fixture")

    monkeypatch.setattr(original_query, "_query_adapter", timeout)
    result = original_query.query_once(
        service.store,
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == "query_error" and len(sends) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("accepted",)


def test_mismatched_upstream_response_is_recorded_as_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda _: {"data": {"task_id": "another-task", "status": "SUCCESS"}},
    )
    result = original_query.query_once(
        service.store,
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == "conflict" and len(sends) == 1


def test_missing_upstream_id_never_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def timeout(_):
        raise TimeoutError()

    monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
    attempt = offline_submission.submit_offline(service, *args)
    register_root_candidate(service.store, "op", "a")
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        original_query, "_query_adapter", lambda identity: calls.append(identity)
    )
    result = original_query.query_once(
        service.store,
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == "unverified" and calls == []
