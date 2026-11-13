"""Recovery chain corruption and concurrent observations, offline only."""

import socket
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ecommerce_product_image_creator import offline_submission, original_query, recovery
from ecommerce_product_image_creator.execution_candidates import (
    CandidateError,
    read_candidate,
)

from .test_cross_candidate_submission import setup_cross
from .test_original_query import setup_query


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)


def test_fork_prevents_query(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            calls.append(i)
            or {"data": {"task_id": i.upstream_id, "status": "IN_PROGRESS"}}
        ),
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
    assert recovery.recover_once(service.store, *identity).state == "running"
    with service.store._connect() as db:
        db.execute(
            "INSERT INTO recovery_observation(root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,provider,model,endpoint,grouping,upstream_id,source,response_digest,remote_state,reason,observed_at,previous_id) SELECT root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,provider,model,endpoint,grouping,upstream_id,source,response_digest,remote_state,reason,observed_at,NULL FROM recovery_observation WHERE attempt_id=? LIMIT 1",
            (attempt.attempt_id,),
        )
    assert recovery.recover_once(service.store, *identity).state == "conflict"
    assert len(calls) == 1 and len(sends) == 1


def test_concurrent_queries_append_one_chain(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)

    def query(i):
        barrier.wait(timeout=10)
        return {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: recovery.recover_once(service.store, *identity), range(2)
            )
        )
    assert [r.state for r in results] == ["succeeded", "succeeded"]
    observations = original_query.read_observations(
        service.store, "op", "a", attempt.attempt_id
    )
    assert (
        len(observations) == 2
        and observations[1].previous_id == observations[0].observation_id
    )
    assert len(sends) == 1


@pytest.mark.parametrize(
    "column,value",
    [
        ("execution_digest", "wrong"),
        ("root_operation_id", "wrong"),
        ("previous_id", "self"),
    ],
)
def test_corrupt_history_is_rejected_before_query(tmp_path, monkeypatch, column, value):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            calls.append(i)
            or {"data": {"task_id": i.upstream_id, "status": "IN_PROGRESS"}}
        ),
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
    assert recovery.recover_once(service.store, *identity).state == "running"
    with service.store._connect() as db:
        db.execute("DROP TRIGGER recovery_observation_no_update")
        if value == "self":
            db.execute("UPDATE recovery_observation SET previous_id=observation_id")
        elif column == "execution_digest":
            db.execute("UPDATE recovery_observation SET execution_digest=?", (value,))
        else:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("UPDATE recovery_observation SET root_operation_id=?", (value,))
    assert recovery.recover_once(service.store, *identity).state == "conflict"
    assert len(calls) == 1 and len(sends) == 1


def test_concurrent_inconsistent_terminal_observations_conflict(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    statuses = iter(("SUCCESS", "FAILURE"))

    def query(i):
        with lock:
            status = next(statuses)
        barrier.wait(timeout=10)
        return {"data": {"task_id": i.upstream_id, "status": status}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda _: recovery.recover_once(service.store, *identity), range(2)
            )
        )
    history = original_query.read_observations(
        service.store, "op", "a", attempt.attempt_id
    )
    assert len(history) == 2 and history[1].state == "conflict"
    assert history[1].previous_id == history[0].observation_id and len(sends) == 1


def test_two_candidates_cannot_exchange_attempts(tmp_path, monkeypatch):
    service, target, first, _, _, b, sent = setup_cross(tmp_path, monkeypatch)
    from .authorized_query_fixture import install

    install(monkeypatch)
    fourth = offline_submission.submit_offline(service, *target, retry_of="op")
    assert fourth.state == "accepted" and len(sent) == 1
    a = read_candidate(service.store, "a")
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            calls.append(i)
            or {"data": {"task_id": i.upstream_id, "status": "IN_PROGRESS"}}
        ),
    )
    for candidate, attempt, snapshot in [
        ("a", fourth, a.snapshot),
        ("b", first, b.snapshot),
    ]:
        result = recovery.recover_once(
            service.store,
            "op",
            candidate,
            snapshot.task_id,
            snapshot.revision,
            snapshot.stage,
            attempt.attempt_id,
            snapshot.execution_digest,
        )
        assert result.state == "unverified"
    assert not calls
    snapshot = b.snapshot
    result = recovery.recover_once(
        service.store,
        "op",
        "b",
        snapshot.task_id,
        snapshot.revision,
        snapshot.stage,
        fourth.attempt_id,
        snapshot.execution_digest,
    )
    assert result.state == "running" and len(calls) == 1
    assert original_query.read_observations(service.store, "op", "b", fourth.attempt_id)
    assert len(sent) == 1


def test_cross_attempt_predecessor_is_detected(tmp_path, monkeypatch):
    service, target, first, _, _, b, sent = setup_cross(tmp_path, monkeypatch)
    from .authorized_query_fixture import install

    install(monkeypatch)
    fourth = offline_submission.submit_offline(service, *target, retry_of="op")
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            calls.append(i)
            or {"data": {"task_id": i.upstream_id, "status": "IN_PROGRESS"}}
        ),
    )
    snap = b.snapshot
    identity = (
        "op",
        "b",
        snap.task_id,
        snap.revision,
        snap.stage,
        fourth.attempt_id,
        snap.execution_digest,
    )
    assert recovery.recover_once(service.store, *identity).state == "running"
    with service.store._connect() as db:
        db.execute(
            "INSERT INTO recovery_observation(root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,provider,model,endpoint,grouping,upstream_id,source,response_digest,remote_state,reason,observed_at,previous_id) SELECT root_operation_id,candidate_id,task_id,revision,stage,?,execution_digest,provider,model,endpoint,grouping,upstream_id,source,response_digest,remote_state,reason,observed_at,observation_id FROM recovery_observation WHERE attempt_id=?",
            (first.attempt_id, fourth.attempt_id),
        )
    assert recovery.recover_once(service.store, *identity).state == "conflict"
    assert len(calls) == 1 and len(sent) == 1


@pytest.mark.parametrize(
    "window,expected", [("before", 0), ("response", 0), ("committed", 1)]
)
def test_subprocess_exit_windows(tmp_path, monkeypatch, window, expected):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    code = """
import os,socket,sys
from pathlib import Path
import pytest
from ecommerce_product_image_creator import original_query
from ecommerce_product_image_creator.storage import Store
from tests.integration.authorized_query_fixture import install
def blocked(*a,**k): raise RuntimeError('network forbidden')
socket.socket.connect=blocked
socket.create_connection=blocked
original_query._verify_original_binding=lambda *a: True
original_query._query_adapter=lambda i: {'data': {'task_id':i.upstream_id,'status':'SUCCESS'}}
install(pytest.MonkeyPatch())
if sys.argv[6]=='before': os._exit(31)
if sys.argv[6]=='response': original_query._after_query_received=lambda: os._exit(32)
result=original_query.query_once(Store(Path(sys.argv[1])), 'op','a',sys.argv[2],1,sys.argv[3],sys.argv[4])
if sys.argv[6]=='committed' and result.state=='succeeded': os._exit(33)
os._exit(34)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.store.path),
            args[0].task_id,
            attempt.attempt_id,
            args[4].execution_digest,
            "unused",
            window,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert child.returncode == {"before": 31, "response": 32, "committed": 33}[window]
    observations = original_query.read_observations(
        service.store, "op", "a", attempt.attempt_id
    )
    assert len(observations) == expected
    reader = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from ecommerce_product_image_creator.original_query import read_observations; from ecommerce_product_image_creator.storage import Store; r=read_observations(Store(Path(sys.argv[1])),'op','a',sys.argv[2]); print(len(r),r[-1].state if r else 'unknown')",
            str(service.store.path),
            attempt.attempt_id,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert reader.stdout.strip() == ("1 succeeded" if expected else "0 unknown")
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)
    assert len(sends) == 1


def test_explicit_legacy_schema_upgrade(tmp_path, monkeypatch):
    service, args, attempt, _ = setup_query(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute(
            "INSERT INTO artifact VALUES(?,?,?,?)",
            ("legacy-artifact", attempt.attempt_id, "relative-fixture.png", "a" * 64),
        )
    tables = (
        "task",
        "revision",
        "attempt",
        "operation",
        "artifact",
        "attempt_counter",
        "operation_attempt",
        "execution_candidate",
    )
    legacy = tmp_path / "explicit-legacy.sqlite"
    with sqlite3.connect(legacy) as db:
        db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE task(task_id TEXT PRIMARY KEY,input_digest TEXT NOT NULL,instructions TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft');
        CREATE TABLE revision(task_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),input_digest TEXT NOT NULL,PRIMARY KEY(task_id,revision),FOREIGN KEY(task_id) REFERENCES task(task_id));
        CREATE TABLE attempt(attempt_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,revision INTEGER NOT NULL,upstream_status TEXT NOT NULL,delivery_status TEXT NOT NULL,upstream_id TEXT,previous_attempt_id TEXT,FOREIGN KEY(task_id,revision) REFERENCES revision(task_id,revision),FOREIGN KEY(previous_attempt_id) REFERENCES attempt(attempt_id));
        CREATE TABLE operation(operation_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,revision INTEGER NOT NULL,digest TEXT NOT NULL,attempt_id TEXT NOT NULL UNIQUE,attempt_count INTEGER NOT NULL CHECK(attempt_count>0),FOREIGN KEY(task_id,revision) REFERENCES revision(task_id,revision),FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id));
        CREATE TABLE artifact(artifact_id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id));
        CREATE TABLE attempt_counter(operation_id TEXT NOT NULL,model TEXT NOT NULL,endpoint TEXT NOT NULL,count INTEGER NOT NULL CHECK(count BETWEEN 1 AND 3),PRIMARY KEY(operation_id,model,endpoint),FOREIGN KEY(operation_id) REFERENCES operation(operation_id));
        CREATE TABLE operation_attempt(operation_id TEXT NOT NULL,sequence INTEGER NOT NULL CHECK(sequence>0),attempt_id TEXT NOT NULL UNIQUE,previous_attempt_id TEXT,model TEXT NOT NULL,endpoint TEXT NOT NULL,execution_digest TEXT NOT NULL,PRIMARY KEY(operation_id,sequence),FOREIGN KEY(operation_id) REFERENCES operation(operation_id),FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),FOREIGN KEY(previous_attempt_id) REFERENCES attempt(attempt_id));
        CREATE TABLE execution_candidate(candidate_id TEXT PRIMARY KEY,root_operation_id TEXT NOT NULL,task_id TEXT NOT NULL,revision INTEGER NOT NULL,source_candidate_id TEXT,snapshot_payload TEXT NOT NULL,execution_digest TEXT NOT NULL,created_at REAL NOT NULL,UNIQUE(root_operation_id,execution_digest),FOREIGN KEY(root_operation_id) REFERENCES operation(operation_id),FOREIGN KEY(task_id,revision) REFERENCES revision(task_id,revision),FOREIGN KEY(source_candidate_id) REFERENCES execution_candidate(candidate_id));
        """)
        with service.store._connect() as source:
            data = {
                table: source.execute(f"SELECT * FROM {table}").fetchall()
                for table in tables
            }
        for table in tables:
            for row in data[table]:
                db.execute(
                    f"INSERT INTO {table} VALUES({','.join('?' for _ in row)})", row
                )
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    from ecommerce_product_image_creator.storage import Store

    upgraded = Store(legacy)
    Store(legacy)
    with upgraded._connect() as db:
        assert {
            table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        } == data
        assert db.execute("SELECT count(*) FROM recovery_observation").fetchone() == (
            0,
        )
        assert db.execute(
            "SELECT count(*) FROM artifact_recovery_intent"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM artifact_recovery_stage"
        ).fetchone() == (0,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    # This deliberately incomplete legacy sample lacks prepared snapshots; it is
    # preserved for audit but cannot be treated as an executable candidate.
    with pytest.raises(CandidateError):
        read_candidate(upgraded, "a")
    assert data["execution_candidate"][0][6] == args[4].execution_digest


def test_nonempty_observation_survives_recovery_schema_upgrade(tmp_path, monkeypatch):
    from ecommerce_product_image_creator.storage import Store

    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    result = recovery.recover_once(
        service.store,
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == "succeeded" and len(sends) == 1
    import hashlib
    import time

    from PIL import Image

    from ecommerce_product_image_creator.domain import UpstreamStatus
    from ecommerce_product_image_creator.form_workflow import register_form_artifact
    from ecommerce_product_image_creator.review import FormReview, save_review
    from ecommerce_product_image_creator.storage import ResourceBudget

    service.store.record_upstream(
        attempt.attempt_id, UpstreamStatus.SUCCEEDED, "fixture-upstream"
    )
    artifact_path = tmp_path / "historical.png"
    Image.new("RGB", (5, 5), "blue").save(artifact_path)
    artifact_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    register_form_artifact(
        service.store,
        args[0].task_id,
        1,
        attempt.attempt_id,
        "historical-artifact",
        tmp_path,
        artifact_path,
        "image/png",
        artifact_digest,
        ResourceBudget(100000, 10000, 100000),
    )
    save_review(
        service.store,
        FormReview(
            "historical-review",
            args[0].task_id,
            1,
            "historical-artifact",
            artifact_digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    tables = (
        "task",
        "revision",
        "attempt",
        "operation",
        "operation_attempt",
        "attempt_counter",
        "execution_candidate",
        "artifact",
        "recovery_observation",
        "form_review",
        "form_review_detail",
    )
    with service.store._connect() as db:
        original = {
            name: db.execute(f"SELECT * FROM {name}").fetchall() for name in tables
        }
        assert original["recovery_observation"]
        dump = [line for line in db.iterdump() if "artifact_recovery_" not in line]
    legacy = tmp_path / "nonempty-legacy.sqlite"
    with sqlite3.connect(legacy) as db:
        db.executescript("\n".join(dump))
    upgraded = Store(legacy)
    Store(legacy)
    with upgraded._connect() as db:
        assert {
            name: db.execute(f"SELECT * FROM {name}").fetchall() for name in tables
        } == original
        assert db.execute(
            "SELECT count(*) FROM artifact_recovery_intent"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM artifact_recovery_stage"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM artifact_recovery_publication"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM artifact_recovery_link_observation"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM artifact_recovery_reconciliation"
        ).fetchone() == (0,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
