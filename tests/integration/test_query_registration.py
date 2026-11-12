"""B1a: query registration must persist distinct request and consumption facts."""

import sqlite3

import pytest

from ecommerce_product_image_creator.storage import Store


def test_registration_schema_supports_atomic_replay_without_execution(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    with store._connect() as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "query_request",
        "query_authorization",
        "query_authorization_consumption",
    } <= tables


def setup_registration(tmp_path, monkeypatch):
    import time

    from ecommerce_product_image_creator import original_query, query_registration

    from .test_original_query import setup_query

    service, args, attempt, sends = setup_query(
        tmp_path, monkeypatch, authorized_legacy=False
    )
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: (
            i.attempt_id == attempt.attempt_id and i.upstream_id == "fixture-upstream"
        ),
    )
    monkeypatch.setattr(
        original_query, "_query_account_scope", lambda _: "test-account"
    )
    monkeypatch.setattr(
        original_query, "_query_adapter", lambda *_: pytest.fail("query forbidden")
    )
    monkeypatch.setattr(
        query_registration,
        "_verify_query_authorization",
        lambda _db, e: e.source_reference == "test-source",
    )
    snap = args[4]
    request = query_registration.QueryRequest(
        "req-1",
        snap.task_id,
        1,
        snap.stage,
        "op",
        "a",
        attempt.attempt_id,
        snap.execution_digest,
        "fixture-upstream",
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "test-account",
        "query_original",
        "auth-1",
    )
    auth = query_registration.QueryAuthorization(
        "auth-1",
        "req-1",
        attempt.attempt_id,
        snap.execution_digest,
        "candidate_gateway",
        "test-account",
        "query_original",
        1,
        "test-source",
        time.time() + 300,
        "test-only unknown query cost",
    )
    return service, request, auth, sends


def test_atomic_registration_and_replay(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import query_registration

    service, req, auth, sends = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    before = len(sends)
    with service.store._connect() as db:
        counts_before = db.execute("SELECT * FROM attempt_counter").fetchall()
        attempts_before = db.execute("SELECT * FROM operation_attempt").fetchall()
    first = query_registration.register_request(service.store, req)
    second = query_registration.register_request(service.store, req)
    assert first.created and not second.created
    assert (
        first.invocation_id == second.invocation_id
        and first.status == second.status == "intent"
    )
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_request").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT status,observation_id FROM query_invocation"
        ).fetchall() == [("intent", None)]
        assert db.execute("SELECT count(*) FROM recovery_observation").fetchone() == (
            0,
        )
        assert db.execute("SELECT count(*) FROM query_billing_event").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert db.execute("SELECT * FROM attempt_counter").fetchall() == counts_before
        assert (
            db.execute("SELECT * FROM operation_attempt").fetchall() == attempts_before
        )
    assert len(sends) == before


def test_scope_replay_conflict_and_unauthorized(tmp_path, monkeypatch):
    import dataclasses

    from ecommerce_product_image_creator import query_registration

    service, req, auth, sends = setup_registration(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="query_authorization_missing"):
        query_registration.register_request(service.store, req)
    query_registration.record_authorization(service.store, auth)
    with pytest.raises(ValueError, match="query_authorization_scope_mismatch"):
        query_registration.register_request(
            service.store, dataclasses.replace(req, account_scope="other")
        )
    result = query_registration.register_request(service.store, req)
    with pytest.raises(ValueError, match="query_request_conflict"):
        query_registration.register_request(
            service.store, dataclasses.replace(req, trusted_upstream_id="other")
        )
    with pytest.raises(ValueError, match="query_authorization_missing"):
        query_registration.register_request(
            service.store, dataclasses.replace(req, request_id="req-2")
        )
    assert result.status == "intent" and sends == [1]


def test_default_authorization_rejects_and_revocation_blocks(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import query_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    monkeypatch.undo()
    with pytest.raises(ValueError, match="query_authorization_unverified"):
        query_registration.record_authorization(service.store, auth)
    monkeypatch.setattr(
        query_registration, "_verify_query_authorization", lambda *_: True
    )
    monkeypatch.setattr(query_registration, "_verify_query_revocation", lambda *_: True)
    query_registration.record_authorization(service.store, auth)
    query_registration.revoke_authorization(
        service.store, auth.authorization_id, "test-revoke"
    )
    with pytest.raises(ValueError, match="query_authorization_invalid"):
        query_registration.register_request(service.store, req)
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (0,)


def test_concurrent_registration_and_rollback(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from ecommerce_product_image_creator import query_registration

    service, req, auth, sends = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)

    def fail():
        raise RuntimeError("injected")

    monkeypatch.setattr(query_registration, "_before_commit", fail)
    with pytest.raises(RuntimeError):
        query_registration.register_request(service.store, req)
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_request").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (0,)
    monkeypatch.setattr(query_registration, "_before_commit", lambda: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: query_registration.register_request(service.store, req),
                range(2),
            )
        )
    assert sum(x.created for x in results) == 1
    assert results[0].invocation_id == results[1].invocation_id
    assert sends == [1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_id", "wrong"),
        ("attempt_id", "wrong"),
        ("execution_digest_v2", "a" * 64),
        ("trusted_upstream_id", "other"),
        ("provider", "other"),
        ("account_scope", "other"),
    ],
)
def test_mismatched_identity_or_authorization_is_rejected(
    tmp_path, monkeypatch, field, value
):
    import dataclasses

    from ecommerce_product_image_creator import query_registration

    service, req, auth, sends = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    with pytest.raises(ValueError):
        query_registration.register_request(
            service.store, dataclasses.replace(req, **{field: value})
        )
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_request").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (0,)
    assert sends == [1]


def test_expired_query_authorization_rejected_at_consumption(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import query_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    monkeypatch.setattr(query_registration.time, "time", lambda: auth.expires_at + 1)
    with pytest.raises(ValueError, match="query_authorization_invalid"):
        query_registration.register_request(service.store, req)


def test_new_process_replays_registered_intent_without_verifier_or_dispatch(
    tmp_path, monkeypatch
):
    import dataclasses
    import json
    import subprocess
    import sys

    from ecommerce_product_image_creator import query_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    first = query_registration.register_request(service.store, req)
    code = "import json,sys; from pathlib import Path; from ecommerce_product_image_creator.query_registration import QueryRequest,register_request; from ecommerce_product_image_creator.storage import Store; r=register_request(Store(Path(sys.argv[1])),QueryRequest(**json.loads(sys.argv[2]))); print(r.invocation_id,r.status,r.created)"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.store.path),
            json.dumps(dataclasses.asdict(req)),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == f"{first.invocation_id} intent False"


def test_two_processes_compete_for_one_request(tmp_path, monkeypatch):
    import dataclasses
    import json
    import subprocess
    import sys

    from ecommerce_product_image_creator import query_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    code = "import json,sys; from pathlib import Path; from ecommerce_product_image_creator import original_query as o, query_registration as q; from ecommerce_product_image_creator.storage import Store; o._verify_original_binding=lambda *_:True; o._query_account_scope=lambda _:'test-account'; q._verify_query_authorization=lambda *_:True; r=q.register_request(Store(Path(sys.argv[1])),q.QueryRequest(**json.loads(sys.argv[2]))); print(r.invocation_id,r.created)"
    argv = [
        sys.executable,
        "-c",
        code,
        str(service.store.path),
        json.dumps(dataclasses.asdict(req)),
    ]
    children = [
        subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    outputs = [child.communicate(timeout=30) for child in children]
    assert [c.returncode for c in children] == [0, 0], outputs
    parts = [out[0].strip().split() for out in outputs]
    assert parts[0][0] == parts[1][0]
    assert sorted(p[1] for p in parts) == ["False", "True"]
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_request").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (1,)


def test_crash_before_commit_rolls_back_in_new_process(tmp_path, monkeypatch):
    import dataclasses
    import json
    import subprocess
    import sys

    from ecommerce_product_image_creator import query_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    code = "import json,os,sys; from pathlib import Path; from ecommerce_product_image_creator import original_query as o, query_registration as q; from ecommerce_product_image_creator.storage import Store; o._verify_original_binding=lambda *_:True; o._query_account_scope=lambda _:'test-account'; q._verify_query_authorization=lambda *_:True; q._before_commit=lambda:os._exit(83); q.register_request(Store(Path(sys.argv[1])),q.QueryRequest(**json.loads(sys.argv[2])))"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.store.path),
            json.dumps(dataclasses.asdict(req)),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 83
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_request").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (0,)
    assert query_registration.register_request(service.store, req).created


def test_legacy_invocation_remains_readable_after_compatible_initialization(
    tmp_path, monkeypatch
):
    from ecommerce_product_image_creator import original_query, query_registration
    from ecommerce_product_image_creator.storage import Store

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    query_registration.register_request(service.store, req)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "IN_PROGRESS"}},
    )
    legacy = original_query.query_once(
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
        request_id=req.request_id,
    )
    assert legacy.state == "running" and legacy.invocation_id
    with service.store._connect() as db:
        old = db.execute(
            "SELECT * FROM query_invocation WHERE invocation_id=?",
            (legacy.invocation_id,),
        ).fetchone()
        for table in (
            "query_authorization_consumption",
            "query_dispatch_stop",
            "query_request_observation_baseline",
            "query_request",
            "query_authorization_revocation",
            "query_authorization",
        ):
            db.execute(f"DROP TABLE {table}")
    upgraded = Store(service.store.path)
    with upgraded._connect() as db:
        assert (
            db.execute(
                "SELECT * FROM query_invocation WHERE invocation_id=?",
                (legacy.invocation_id,),
            ).fetchone()
            == old
        )
        assert db.execute("SELECT count(*) FROM query_request").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM query_authorization").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable query invocation"):
            db.execute(
                "DELETE FROM query_invocation WHERE invocation_id=?",
                (legacy.invocation_id,),
            )
    assert query_registration._verify_query_authorization is not None


def test_independent_requests_need_independent_authorizations(tmp_path, monkeypatch):
    import dataclasses

    from ecommerce_product_image_creator import query_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    other_req = dataclasses.replace(
        req, request_id="req-2", query_authorization_reference="auth-2"
    )
    other_auth = dataclasses.replace(
        auth, authorization_id="auth-2", request_id="req-2"
    )
    query_registration.record_authorization(service.store, auth)
    query_registration.record_authorization(service.store, other_auth)
    first = query_registration.register_request(service.store, req)
    second = query_registration.register_request(service.store, other_req)
    assert first.invocation_id != second.invocation_id
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (2,)
        for table, column in [
            ("query_request", "scope_payload"),
            ("query_authorization", "evidence_payload"),
            ("query_authorization_consumption", "consumed_at"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(f"UPDATE {table} SET {column}={column}")


def test_observation_conflict_blocks_intent(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import (
        original_query,
        query_registration,
        recovery,
    )

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    statuses = iter(["SUCCESS", "FAILURE"])
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": next(statuses)}},
    )
    import dataclasses

    for index in range(2):
        request_id = f"prior-{index}"
        authorization_id = f"prior-auth-{index}"
        prior_req = dataclasses.replace(
            req, request_id=request_id, query_authorization_reference=authorization_id
        )
        prior_auth = dataclasses.replace(
            auth, request_id=request_id, authorization_id=authorization_id
        )
        query_registration.record_authorization(service.store, prior_auth)
        query_registration.register_request(service.store, prior_req)
        original_query.query_once(
            service.store,
            req.root_operation_id,
            req.candidate_id,
            req.task_id,
            req.revision,
            req.attempt_id,
            req.execution_digest_v2,
            request_id=request_id,
        )
    query_registration.record_authorization(service.store, auth)
    with pytest.raises(ValueError, match="query_observation_unverified"):
        query_registration.register_request(service.store, req)
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_request").fetchone() == (2,)
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (2,)
