"""T046: process-exit windows for the existing offline submission path."""

import json
import pickle
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ecommerce_product_image_creator import (
    offline_submission,
    original_query,
    query_registration,
    recovery,
)
from ecommerce_product_image_creator.execution_candidates import register_root_candidate

from .test_artifact_reconciliation import first_context  # noqa: F401
from .test_offline_submission import setup_context

CHILD = r"""
import inspect, os, pickle, socket, sys
from pathlib import Path
from ecommerce_product_image_creator import offline_submission as sub, provider
service, args = pickle.loads(Path(sys.argv[1]).read_bytes())
mode, marker = sys.argv[2], Path(sys.argv[3])
def deny(*args, **kwargs): raise AssertionError("network forbidden")
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
sub._verify_authorization = lambda *_: True
sub._verify_business_choices = lambda *_: True
def die(): os._exit(71)
if mode == 'before_commit': sub._before_intent_commit = die
if mode == 'after_intent': sub._after_intent_recorded = die
if mode == 'before_dispatch': sub._before_offline_dispatch = die
def fake(request):
    marker.write_text('entered', encoding='utf-8')
    if mode == 'before_response': os._exit(71)
    result = provider.parse_submission(200, {'task_id':'persisted-id'})
    marker.write_text('returned', encoding='utf-8')
    return result
sub._offline_dispatch = fake
if mode == 'after_return':
    lines, start = inspect.getsourcelines(sub.submit_offline)
    index = next(i for i, text in enumerate(lines) if 'response = _offline_dispatch(request)' in text)
    target = start + index + 2
    def trace(frame, event, arg):
        if frame.f_code is sub.submit_offline.__code__ and event == 'line' and frame.f_lineno == target:
            assert frame.f_locals['response'].task_id == 'persisted-id'
            os._exit(71)
        return trace
    sys.settrace(trace)
sub.submit_offline(service, *args)
if mode == 'after_commit': os._exit(71)
raise AssertionError('crash seam not reached')
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def crash_submission(tmp_path: Path, mode: str):
    service, args = setup_context(tmp_path)
    fixture = tmp_path / "isolated-fixture.bin"
    fixture.write_bytes(pickle.dumps((service, args)))
    marker = tmp_path / "fake-dispatch.txt"
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(fixture), mode, str(marker)],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert child.returncode == 71, child.stderr
    reader = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(json.dumps(db.execute('SELECT attempt_id,upstream_status,upstream_id FROM attempt').fetchall()))",
            str(service.store.path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    with service.store._connect() as db:
        persisted = db.execute(
            "SELECT attempt_id,upstream_status,upstream_id FROM attempt"
        ).fetchall()
    assert json.loads(reader.stdout) == [list(row) for row in persisted]
    return service, args, marker


@pytest.mark.parametrize(
    "mode",
    [
        "before_commit",
        "after_intent",
        "before_dispatch",
        "before_response",
        "after_return",
        "after_commit",
    ],
)
def test_subprocess_submission_boundaries_preserve_persisted_facts(
    tmp_path, monkeypatch, mode
):
    service, args, marker = crash_submission(tmp_path, mode)
    with service.store._connect() as db:
        attempts = db.execute(
            "SELECT attempt_id,upstream_status,upstream_id FROM attempt"
        ).fetchall()
        ops = db.execute("SELECT operation_id,attempt_id FROM operation").fetchall()
        chain = db.execute("SELECT attempt_id FROM operation_attempt").fetchall()
        count = db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchall()
        eligibility = db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone()
        scope_table = db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='retry_scope_reservation'"
        ).fetchone()
    if mode == "before_commit":
        assert attempts == ops == chain == count == [] and eligibility == (0,)
        assert not marker.exists() and scope_table == (0,)
        return
    assert len(attempts) == len(ops) == len(chain) == 1
    assert ops[0][1] == chain[0][0] == attempts[0][0]
    assert count == [(1,)] and eligibility == (1,) and scope_table == (0,)
    assert attempts[0][1:] == (
        ("accepted", "persisted-id")
        if mode == "after_commit"
        else ("submission_uncertain", None)
    )
    if mode in {"after_intent", "before_dispatch"}:
        assert not marker.exists()
    else:
        assert marker.read_text() == (
            "entered" if mode == "before_response" else "returned"
        )
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda *_: pytest.fail("replay dispatched"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: offline_submission.submit_offline(service, *args), range(2)
            )
        )
    assert all(r.attempt_id == attempts[0][0] for r in results)
    assert all(
        r.state == ("accepted" if mode == "after_commit" else "unknown")
        for r in results
    )
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='retry_scope_reservation'"
        ).fetchone() == (0,)
    if mode in {"before_response", "after_return"}:
        register_root_candidate(service.store, "op", "a")
        monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
        monkeypatch.setattr(
            original_query,
            "_query_adapter",
            lambda *_: pytest.fail("query without persisted ID"),
        )
        snap = args[4]
        result = recovery.recover_once(
            service.store,
            "op",
            "a",
            snap.task_id,
            snap.revision,
            snap.stage,
            attempts[0][0],
            snap.execution_digest,
        )
        assert result.state == "unverified" and not result.adapter_invoked


def test_persisted_id_needs_separate_query_authorization_after_restart(
    tmp_path, monkeypatch
):
    service, args, marker = crash_submission(tmp_path, "after_commit")
    assert marker.read_text() == "returned"
    candidate = register_root_candidate(service.store, "op", "a")
    with service.store._connect() as db:
        attempt_id, upstream_id = db.execute(
            "SELECT attempt_id,upstream_id FROM attempt"
        ).fetchone()
    snap = args[4]
    assert (
        upstream_id == "persisted-id"
        and candidate.snapshot.execution_digest == snap.execution_digest
    )
    identity = (
        "op",
        "a",
        snap.task_id,
        snap.revision,
        snap.stage,
        attempt_id,
        snap.execution_digest,
    )
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == attempt_id and i.upstream_id == upstream_id,
    )
    monkeypatch.setattr(
        original_query, "_query_account_scope", lambda _: "test-account"
    )
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            calls.append(i.upstream_id)
            or {"data": {"task_id": i.upstream_id, "status": "IN_PROGRESS"}}
        ),
    )
    before = recovery.recover_once(service.store, *identity, request_id="query-1")
    assert not before.adapter_invoked and calls == []
    auth = query_registration.QueryAuthorization(
        "auth-1",
        "query-1",
        attempt_id,
        snap.execution_digest,
        "candidate_gateway",
        "test-account",
        "query_original",
        1,
        "fixture",
        time.time() + 300,
        "unknown",
    )
    req = query_registration.QueryRequest(
        "query-1",
        snap.task_id,
        snap.revision,
        snap.stage,
        "op",
        "a",
        attempt_id,
        snap.execution_digest,
        upstream_id,
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "test-account",
        "query_original",
        "auth-1",
    )
    with pytest.raises(ValueError, match="unverified"):
        query_registration.record_authorization(service.store, auth)
    monkeypatch.setattr(
        query_registration, "_verify_query_authorization", lambda *_: True
    )
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    query_registration.record_authorization(service.store, auth)
    query_registration.register_request(service.store, req)
    result = recovery.recover_once(service.store, *identity, request_id="query-1")
    assert (
        result.adapter_invoked and result.state == "running" and calls == [upstream_id]
    )
    replay = recovery.recover_once(service.store, *identity, request_id="query-1")
    assert not replay.adapter_invoked and calls == [upstream_id]
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "phase",
    [
        "partial_write",
        "written",
        "verified",
        "before_link",
        "after_link",
        "before_commit",
        "after_commit",
    ],
)
def test_first_artifact_crash_windows_preserve_identity(first_context, tmp_path, phase):  # noqa: F811
    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    is_stage = phase in {"partial_write", "written", "verified"}
    if not is_stage:
        assert (
            service.store.stage_first_artifact("first", tmp_path, service.budget)
            == "staged"
        )
    source = tmp_path / "isolated-source.bin"
    source.write_bytes(contents)
    code = r"""
import os,socket,sys
from pathlib import Path
from ecommerce_product_image_creator import original_query,recovery,artifact_locator,first_artifact
from ecommerce_product_image_creator.storage import Store,ResourceBudget
def deny(*args,**kwargs): raise AssertionError('network forbidden')
socket.socket.connect=deny
socket.create_connection=deny
original_query._verify_original_binding=lambda *_:True
recovery._verify_observation_source=lambda *_:True
artifact_locator._verify_locator_source=lambda *_:True
artifact_locator._verify_fixture_access=lambda *_:True
artifact_locator._verify_fixture_content=lambda _,data: data==Path(sys.argv[3]).read_bytes()
artifact_locator._fetch_fixture=lambda *_:(Path(sys.argv[3]).read_bytes(),'image/png')
first_artifact._verify_digest_source=lambda _,digest,mime:digest==sys.argv[5] and mime=='image/png'
def hook(phase):
 if phase==sys.argv[4]: os._exit(83)
first_artifact._hook=hook
s=Store(Path(sys.argv[1])); root=Path(sys.argv[2]); budget=ResourceBudget(100000,10000,100000)
if sys.argv[4] in ('partial_write','written','verified'): s.stage_first_artifact('first',root,budget)
else: s.reconcile_first_artifact('first',root,budget)
raise AssertionError('crash seam missed')
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.store.path),
            str(tmp_path),
            str(source),
            phase,
            digest,
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert child.returncode == 83, child.stderr
    with service.store._connect() as db:
        artifacts = db.execute(
            "SELECT artifact_id,attempt_id,path,sha256 FROM artifact"
        ).fetchall()
        assert db.execute("SELECT count(*) FROM first_artifact_anchor").fetchone() == (
            1,
        )
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
    if is_stage:
        assert artifacts == [] and not target.exists()
        with pytest.raises(ValueError, match="first_stage_conflict"):
            service.store.stage_first_artifact("first", tmp_path, service.budget)
    else:
        assert target.exists() == (phase != "before_link")
        assert len(artifacts) == (1 if phase == "after_commit" else 0)
        assert (
            service.store.reconcile_first_artifact("first", tmp_path, service.budget)
            == "delivered"
        )
        assert target.read_bytes() == contents
        with service.store._connect() as db:
            assert db.execute(
                "SELECT artifact_id,attempt_id,path,sha256 FROM artifact"
            ).fetchall() == [
                ("first-artifact", attempt.attempt_id, str(target), digest)
            ]
    assert sends == [1]


def test_w5_first_delivery_crash_before_staging_restarts(first_context, tmp_path):  # noqa: F811
    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    source = tmp_path / "w5-fixture.bin"
    source.write_bytes(contents)
    crash = "import os,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); assert db.execute('SELECT count(*) FROM first_artifact_anchor').fetchone()==(1,); assert db.execute('SELECT count(*) FROM recovery_observation WHERE remote_state=\"succeeded\"').fetchone()==(1,); os._exit(85)"
    child = subprocess.run(
        [sys.executable, "-c", crash, str(service.store.path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 85, child.stderr
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM first_artifact_stage").fetchone() == (
            0,
        )
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
    assert not target.exists()
    code = r"""
import socket,sys
from pathlib import Path
from ecommerce_product_image_creator import original_query,recovery,artifact_locator,first_artifact
from ecommerce_product_image_creator.storage import Store,ResourceBudget
def deny(*args,**kwargs): raise AssertionError('network forbidden')
socket.socket.connect=deny
socket.create_connection=deny
original_query._verify_original_binding=lambda _db,i:i.attempt_id==sys.argv[4]
recovery._verify_observation_source=lambda i,row:row[6]==i.attempt_id
artifact_locator._verify_locator_source=lambda i,e:i.attempt_id==e.attempt_id
artifact_locator._verify_fixture_access=lambda *_:True
artifact_locator._verify_fixture_content=lambda _,data:data==Path(sys.argv[3]).read_bytes()
artifact_locator._fetch_fixture=lambda *_:(Path(sys.argv[3]).read_bytes(),'image/png')
first_artifact._verify_digest_source=lambda _,sha,mime:sha==sys.argv[5] and mime=='image/png'
store=Store(Path(sys.argv[1])); root=Path(sys.argv[2]); budget=ResourceBudget(100000,10000,100000)
print(store.stage_first_artifact('first',root,budget),store.reconcile_first_artifact('first',root,budget))
"""
    recovered = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.store.path),
            str(tmp_path),
            str(source),
            attempt.attempt_id,
            digest,
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout.strip() == "staged delivered"
    assert target.read_bytes() == contents and sends == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT artifact_id,attempt_id,sha256 FROM artifact"
        ).fetchall() == [("first-artifact", attempt.attempt_id, digest)]
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
