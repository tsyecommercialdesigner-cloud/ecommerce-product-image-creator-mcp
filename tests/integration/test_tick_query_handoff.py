"""Recovery tick to independently authorized original query; no regeneration."""

import time
from dataclasses import replace

import pytest

from ecommerce_product_image_creator import (
    offline_submission,
    original_query,
    provider,
    query_registration,
    recovery,
    retry_scope,
    task_view,
    tasks,
)
from ecommerce_product_image_creator.domain import ExecutionSnapshot, UpstreamStatus
from ecommerce_product_image_creator.execution_candidates import (
    create_candidate,
    read_candidate,
    register_root_candidate,
)
from integration.test_attempt002 import prepare_retry
from integration.test_retry_scopes import make_scopes


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def context(tmp_path, monkeypatch):
    service, args, _, generation_calls, _, _ = make_scopes(tmp_path, monkeypatch)
    retry_scope.reserve_eligibility(
        service, *args, root="op", candidate_id="source", host_id="host", cost_id="cost"
    )
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: (
            generation_calls.append(1)
            or provider.ProviderResult("accepted", task_id="fixture-upstream")
        ),
    )
    attempted = offline_submission.submit_offline(
        service, *args, retry_of="op", require_retry_scope=True
    )
    assert attempted.state == "accepted"
    service.store.record_upstream(
        attempted.attempt_id, UpstreamStatus.UNKNOWN, "fixture-upstream"
    )
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: pytest.fail("generation forbidden during recovery"),
    )
    tick = provider.recovery_tick(service, "op", "tick-original", clock=time.time)
    assert tick.action == "query_original" and tick.attempt_id == attempted.attempt_id
    with service.store._connect() as db:
        task_id = db.execute(
            "SELECT task_id FROM operation WHERE operation_id=?", ("op",)
        ).fetchone()[0]
    view = task_view.read_task_view(
        service.store, task_id, tmp_path, service.budget, attempt_id=tick.attempt_id
    )
    item = view["attempts"][0]
    candidate = read_candidate(service.store, item["candidate_id"])
    identity = (
        item["root_operation_id"],
        item["candidate_id"],
        task_id,
        item["revision"],
        item["stage"],
        item["attempt_id"],
        item["execution_digest"],
    )
    assert identity[0] == "op" and identity[5] == attempted.attempt_id
    assert item["upstream"]["historical_status"] == "unknown"
    snap = candidate.snapshot
    req = query_registration.QueryRequest(
        "query-request-1",
        task_id,
        item["revision"],
        item["stage"],
        item["root_operation_id"],
        item["candidate_id"],
        item["attempt_id"],
        item["execution_digest"],
        item["upstream"]["upstream_id"],
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "fixture-account",
        "query_original",
        "query-auth-1",
    )
    auth = query_registration.QueryAuthorization(
        "query-auth-1",
        "query-request-1",
        item["attempt_id"],
        item["execution_digest"],
        "candidate_gateway",
        "fixture-account",
        "query_original",
        1,
        "fixture-query-source",
        time.time() + 300,
        "fixture unknown query cost",
    )
    monkeypatch.setattr(
        query_registration,
        "_verify_query_authorization",
        lambda db, value: value == auth,
    )
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda db, value: (
            (
                value.root,
                value.candidate_id,
                value.attempt_id,
                value.digest,
                value.upstream_id,
            )
            == (identity[0], identity[1], identity[5], identity[6], "fixture-upstream")
        ),
    )
    monkeypatch.setattr(
        original_query, "_query_account_scope", lambda identity: "fixture-account"
    )
    monkeypatch.setattr(
        recovery,
        "_verify_observation_source",
        lambda identity, row: (
            row[6] == identity.attempt_id and row[7] == identity.digest
        ),
    )
    query_calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda identity: (
            query_calls.append(identity)
            or {"data": {"task_id": identity.upstream_id, "status": "IN_PROGRESS"}}
        ),
    )
    return service, req, auth, identity, generation_calls, query_calls


def snapshot(store):
    with store._connect() as db:
        return {
            name: db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
            for name in (
                "attempt",
                "operation",
                "operation_attempt",
                "attempt_counter",
                "local_eligibility",
                "retry_scope_reservation",
                "retry_scope_evidence",
            )
        }


def test_tick_query_original_uses_independent_authorization_and_persisted_identity(
    tmp_path, monkeypatch
):
    service, req, auth, identity, sends, queries = context(tmp_path, monkeypatch)
    before = snapshot(service.store)
    assert (
        provider.recovery_tick(service, "op", "tick-original", clock=time.time).action
        == "query_original"
    )
    assert queries == []
    query_registration.record_authorization(service.store, auth)
    registered = query_registration.register_request(service.store, req)
    assert req.request_id != "tick-original"
    first = tasks.recover_task_once(
        service.store,
        *identity,
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    replay = tasks.recover_task_once(
        service.store,
        *identity,
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert first["action_executed"] and not replay["action_executed"]
    assert first["recovery"]["state"] == "running" and len(queries) == 1
    assert (
        first["query"]["invocation_id"]
        == replay["query"]["invocation_id"]
        == registered.invocation_id
    )
    assert snapshot(service.store) == before and len(sends) == 2
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM query_authorization_consumption"
        ).fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM recovery_observation").fetchone() == (
            1,
        )
        assert db.execute(
            "SELECT status FROM query_invocation WHERE invocation_id=?",
            (registered.invocation_id,),
        ).fetchone() == ("observed",)
        trace = db.execute(
            "SELECT q.request_id,c.authorization_id,c.invocation_id,i.root_operation_id,i.candidate_id,i.attempt_id,i.execution_digest,i.upstream_id,o.root_operation_id,o.candidate_id,o.attempt_id,o.execution_digest,o.upstream_id "
            "FROM query_request q JOIN query_authorization_consumption c ON c.request_id=q.request_id JOIN query_invocation i ON i.invocation_id=c.invocation_id JOIN recovery_observation o ON o.observation_id=i.observation_id WHERE q.request_id=?",
            (req.request_id,),
        ).fetchone()
        assert trace == (
            req.request_id,
            auth.authorization_id,
            registered.invocation_id,
            identity[0],
            identity[1],
            identity[5],
            identity[6],
            req.trusted_upstream_id,
            identity[0],
            identity[1],
            identity[5],
            identity[6],
            req.trusted_upstream_id,
        )


@pytest.mark.parametrize(
    "case",
    [
        "untrusted_source",
        "other_attempt",
        "other_candidate",
        "other_root",
        "digest",
        "no_authorization",
        "scope",
        "revoked",
        "expired",
    ],
)
def test_tick_advice_never_bypasses_query_identity_or_authorization(
    tmp_path, monkeypatch, case
):
    service, req, auth, identity, sends, queries = context(tmp_path, monkeypatch)
    before = snapshot(service.store)
    if case == "untrusted_source":
        monkeypatch.setattr(
            original_query, "_verify_original_binding", lambda *_: False
        )
    elif case == "other_attempt":
        with service.store._connect() as db:
            previous = db.execute(
                "SELECT previous_attempt_id FROM operation_attempt WHERE operation_id=? AND attempt_id=?",
                ("op", identity[5]),
            ).fetchone()[0]
        req = replace(req, attempt_id=previous)
        auth = replace(auth, attempt_id=previous)
    elif case == "other_candidate":
        source = read_candidate(service.store, req.candidate_id)
        snap = source.snapshot
        from ecommerce_product_image_creator.domain import Revision
        from ecommerce_product_image_creator.tasks import read_prompt_context

        prompt = read_prompt_context(service.store, req.task_id, req.revision).prompt
        alt = ExecutionSnapshot.create(
            Revision(req.task_id, req.revision, snap.input_digest),
            prompt,
            snap.stage,
            "gpt-image-2.5-flare",
            snap.endpoint,
            snap.group,
            references=snap.references,
            operation_scope=snap.operation_scope,
        )
        create_candidate(service.store, "op", "other-candidate", req.candidate_id, alt)
        req = replace(req, candidate_id="other-candidate")
    elif case == "other_root":
        req = replace(req, root_operation_id="different-root")
    elif case == "digest":
        req = replace(req, execution_digest_v2="a" * 64)
        auth = replace(auth, execution_digest_v2="a" * 64)
    elif case == "scope":
        req = replace(req, account_scope="different-account")
    monkeypatch.setattr(
        query_registration,
        "_verify_query_authorization",
        lambda db, value: value == auth,
    )
    if case != "no_authorization":
        query_registration.record_authorization(service.store, auth)
    if case in {"revoked", "expired"}:
        registered = query_registration.register_request(service.store, req)
        if case == "revoked":
            monkeypatch.setattr(
                query_registration,
                "_verify_query_revocation",
                lambda db, auth_id, reference: (
                    (auth_id, reference) == (auth.authorization_id, "fixture-revoke")
                ),
            )
            query_registration.revoke_authorization(
                service.store, auth.authorization_id, "fixture-revoke"
            )
        else:
            monkeypatch.setattr(
                query_registration.time, "time", lambda: auth.expires_at + 1
            )
        outcome = tasks.recover_task_once(
            service.store,
            *identity,
            action="query_original",
            request_id=req.request_id,
            artifact_root=tmp_path,
            budget=service.budget,
        )
        assert not outcome["action_executed"]
        with service.store._connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM query_authorization_consumption WHERE invocation_id=?",
                (registered.invocation_id,),
            ).fetchone() == (1,)
    else:
        with pytest.raises(ValueError):
            query_registration.register_request(service.store, req)
        outcome = tasks.recover_task_once(
            service.store,
            *identity,
            action="query_original",
            request_id=req.request_id,
            artifact_root=tmp_path,
            budget=service.budget,
        )
        assert not outcome["action_executed"]
    assert queries == [] and len(sends) == 2
    assert snapshot(service.store) == before


@pytest.mark.parametrize(
    "mode", ["wrong_identity", "query_error", "observation_write_failure"]
)
def test_tick_query_failure_never_reclassifies_generation(tmp_path, monkeypatch, mode):
    service, req, auth, identity, sends, queries = context(tmp_path, monkeypatch)
    before = snapshot(service.store)
    query_registration.record_authorization(service.store, auth)
    query_registration.register_request(service.store, req)
    if mode == "wrong_identity":
        monkeypatch.setattr(
            original_query,
            "_query_adapter",
            lambda i: (
                queries.append(i)
                or {"data": {"task_id": "other-upstream", "status": "SUCCESS"}}
            ),
        )
    elif mode == "query_error":

        def fail(i):
            queries.append(i)
            raise TimeoutError("fixture")

        monkeypatch.setattr(original_query, "_query_adapter", fail)
    else:
        with service.store._connect() as db:
            db.execute(
                "CREATE TRIGGER fixture_recovery_write_failure BEFORE INSERT ON recovery_observation BEGIN SELECT RAISE(ABORT,'fixture'); END"
            )
    result = tasks.recover_task_once(
        service.store,
        *identity,
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert len(queries) == 1 and len(sends) == 2
    assert result["recovery"]["state"] not in {"succeeded", "failed"}
    assert snapshot(service.store) == before
    replay = tasks.recover_task_once(
        service.store,
        *identity,
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert not replay["action_executed"] and len(queries) == 1


def test_no_upstream_id_remains_unknown_without_query_or_resubmit(
    tmp_path, monkeypatch
):
    service, args, first, sends = prepare_retry(tmp_path, monkeypatch, "unknown")
    register_root_candidate(service.store, "op", "source")
    monkeypatch.setattr(
        original_query, "_query_adapter", lambda _: pytest.fail("query forbidden")
    )
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: pytest.fail("generation forbidden"),
    )
    tick = provider.recovery_tick(service, "op", "no-upstream", clock=time.time)
    assert tick.action == "stop" and tick.attempt_id == first.attempt_id
    view = task_view.read_task_view(
        service.store,
        args[0].task_id,
        tmp_path,
        service.budget,
        attempt_id=tick.attempt_id,
    )
    item = view["attempts"][0]
    identity = (
        item["root_operation_id"],
        item["candidate_id"],
        args[0].task_id,
        item["revision"],
        item["stage"],
        item["attempt_id"],
        item["execution_digest"],
    )
    result = tasks.recover_task_once(
        service.store,
        *identity,
        action="query_original",
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert not result["action_executed"] and result["duplicate_generation_risk"]
    assert len(sends) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
            (first.attempt_id,),
        ).fetchone() == ("unknown", None)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM query_invocation").fetchone() == (0,)


def test_confirmed_success_is_not_downgraded_by_later_request(tmp_path, monkeypatch):
    service, req, auth, identity, sends, queries = context(tmp_path, monkeypatch)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            queries.append(i)
            or {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}
        ),
    )
    query_registration.record_authorization(service.store, auth)
    query_registration.register_request(service.store, req)
    first = tasks.recover_task_once(
        service.store,
        *identity,
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert first["recovery"]["state"] == "succeeded" and len(queries) == 1
    second_auth = replace(
        auth, authorization_id="query-auth-2", request_id="query-request-2"
    )
    second_req = replace(
        req, request_id="query-request-2", query_authorization_reference="query-auth-2"
    )
    monkeypatch.setattr(
        query_registration,
        "_verify_query_authorization",
        lambda db, value: value in (auth, second_auth),
    )
    query_registration.record_authorization(service.store, second_auth)
    query_registration.register_request(service.store, second_req)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda _: pytest.fail("terminal must not requery"),
    )
    second = tasks.recover_task_once(
        service.store,
        *identity,
        action="query_original",
        request_id=second_req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert second["recovery"]["state"] == "succeeded" and not second["action_executed"]
    assert len(queries) == 1 and len(sends) == 2
    with service.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_observation").fetchone() == (
            1,
        )
