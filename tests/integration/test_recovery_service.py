"""Recovery resolution uses only isolated recorded observations."""

import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ecommerce_product_image_creator import original_query, recovery
from ecommerce_product_image_creator.recovery import recover_once
from ecommerce_product_image_creator.storage import Store

from .test_cross_candidate_submission import setup_cross
from .test_original_query import setup_query


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["IN_PROGRESS", "IN_PROGRESS"], "running"),
        (["IN_PROGRESS", "SUCCESS"], "succeeded"),
        (["IN_PROGRESS", "FAILURE"], "failed"),
        (["SUCCESS", "IN_PROGRESS"], "conflict"),
        (["SUCCESS", "FAILURE"], "conflict"),
        (["FAILURE", "SUCCESS"], "conflict"),
    ],
)
def test_recovery_observations_reduce_without_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statuses, expected
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    pending = iter(statuses)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda identity: {
            "data": {"task_id": identity.upstream_id, "status": next(pending)}
        },
    )
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    recover_once(service.store, *identity)
    result = recover_once(service.store, *identity)
    assert result.state == expected
    assert result.attempt_id == attempt.attempt_id
    assert result.observation_id is not None
    assert len(sends) == 1
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)


def test_error_keeps_prior_success_and_reports_query_error(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert recover_once(service.store, *identity).state == "succeeded"

    def fail(_):
        raise TimeoutError()

    monkeypatch.setattr(original_query, "_query_adapter", fail)
    result = recover_once(service.store, *identity)
    assert result.state == "succeeded" and result.query_state == "query_error"
    assert len(sends) == 1


def test_unverified_identity_never_queries(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    queries = []
    monkeypatch.setattr(original_query, "_query_adapter", lambda i: queries.append(i))
    result = recover_once(
        service.store,
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == "unverified" and result.risk_of_duplicate_cost
    assert not queries and len(sends) == 1


def test_response_hash_alone_does_not_verify_history(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: False)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    result = recover_once(
        service.store,
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == "unverified"
    assert result.reason == "observation_source_unverified" and len(sends) == 1


def test_new_process_recovers_recorded_terminal(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert recover_once(service.store, *identity).state == "succeeded"
    code = """
import socket,sys
from pathlib import Path
import pytest
from ecommerce_product_image_creator import original_query, recovery
from ecommerce_product_image_creator.storage import Store
from tests.integration.authorized_query_fixture import install
def blocked(*a,**k): raise RuntimeError('network denied')
socket.socket.connect=blocked
socket.create_connection=blocked
original_query._verify_original_binding=lambda *a: True
recovery._verify_observation_source=lambda *a: True
def unavailable(i): raise TimeoutError()
original_query._query_adapter=unavailable
install(pytest.MonkeyPatch())
r=recovery.recover_once(Store(Path(sys.argv[1])), 'op','a',sys.argv[2],1,sys.argv[3],sys.argv[4],sys.argv[5])
print(r.state,r.query_state)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.store.path),
            args[0].task_id,
            args[4].stage,
            attempt.attempt_id,
            args[4].execution_digest,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "succeeded query_error"
    assert len(sends) == 1


def test_cross_candidate_and_stage_rejected(tmp_path, monkeypatch):
    service, target, first, _, _, b, sent = setup_cross(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    queries = []
    monkeypatch.setattr(original_query, "_query_adapter", lambda i: queries.append(i))
    snapshot = b.snapshot
    for candidate, attempt, digest, stage in [
        ("b", first.attempt_id, snapshot.execution_digest, snapshot.stage),
        ("b", first.attempt_id, target[4].execution_digest, "other"),
    ]:
        result = recover_once(
            service.store,
            "op",
            candidate,
            snapshot.task_id,
            snapshot.revision,
            stage,
            attempt,
            digest,
        )
        assert result.state == "unverified"
    assert not queries and not sent


def test_old_schema_upgrade_preserves_records(tmp_path, monkeypatch):
    service, args, attempt, _ = setup_query(tmp_path, monkeypatch)
    old_path = tmp_path / "legacy.sqlite"
    tables = (
        "task",
        "revision",
        "attempt",
        "operation",
        "operation_attempt",
        "attempt_counter",
        "execution_candidate",
    )
    with service.store._connect() as source:
        original = {
            table: source.execute(f"SELECT * FROM {table}").fetchall()
            for table in tables
        }
        statements = [
            line
            for line in source.iterdump()
            if "recovery_observation" not in line
            and "artifact_locator" not in line
            and "first_artifact_" not in line
            and "sqlite_sequence" not in line
            and "query_invocation_no_delete" not in line
            and "query_request_baseline_no_" not in line
        ]
    with sqlite3.connect(old_path) as old:
        old.executescript("\n".join(statements))
    upgraded = Store(old_path)
    with upgraded._connect() as db:
        assert {
            table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        } == original
        assert db.execute(
            "SELECT upstream_id FROM attempt WHERE attempt_id=?", (attempt.attempt_id,)
        ).fetchone() == ("fixture-upstream",)
        assert db.execute("SELECT count(*) FROM recovery_observation").fetchone() == (
            0,
        )
        before = db.execute("SELECT * FROM operation").fetchall()
    Store(old_path)
    with upgraded._connect() as db:
        assert db.execute("SELECT * FROM operation").fetchall() == before
    result = recover_once(
        upgraded,
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == "unverified"
