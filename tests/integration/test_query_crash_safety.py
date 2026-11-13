"""Controlled query stop must remain attributable across replay."""

import pytest

from ecommerce_product_image_creator import original_query, query_registration

from .test_query_registration import setup_registration


def test_revoked_before_dispatch_is_durably_stopped(tmp_path, monkeypatch):
    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)
    monkeypatch.setattr(query_registration, "_verify_query_revocation", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_before_query_dispatch",
        lambda: query_registration.revoke_authorization(
            service.store, auth.authorization_id, "test-revoke"
        ),
    )
    calls = []
    monkeypatch.setattr(original_query, "_query_adapter", lambda i: calls.append(i))
    args = (
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    first = original_query.query_once(*args, request_id=req.request_id)
    replay = original_query.query_once(*args, request_id=req.request_id)
    assert first.state == replay.state == "stopped"
    assert first.reason == replay.reason == "stopped_before_dispatch"
    assert first.invocation_id == replay.invocation_id == intent.invocation_id
    assert not calls


def test_stop_evidence_write_failure_remains_unknown(tmp_path, monkeypatch):
    import sqlite3

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    query_registration.register_request(service.store, req)
    monkeypatch.setattr(query_registration, "_verify_query_revocation", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_before_query_dispatch",
        lambda: query_registration.revoke_authorization(
            service.store, auth.authorization_id, "test-revoke"
        ),
    )

    def broken(*_):
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(original_query, "_record_query_stop", broken)
    calls = []
    monkeypatch.setattr(original_query, "_query_adapter", lambda i: calls.append(i))
    args = (
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    first = original_query.query_once(*args, request_id=req.request_id)
    second = original_query.query_once(*args, request_id=req.request_id)
    assert first.state == second.state == "unknown"
    assert (
        first.reason == "query_result_unrecorded"
        and second.reason == "query_may_have_executed"
    )
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_dispatch_stop").fetchone() == (0,)
        assert db.execute("SELECT status FROM query_invocation").fetchone() == (
            "entered",
        )
    assert not calls


@pytest.mark.parametrize(
    "window",
    [
        "transaction",
        "intent",
        "entered",
        "final_check",
        "adapter",
        "response",
        "observation",
        "billing",
    ],
)
def test_windows_crash_windows_do_not_auto_requery(tmp_path, window):
    import json
    import subprocess
    import sys

    script = r"""
import dataclasses,json,os,socket,sys
from pathlib import Path
import pytest
from tests.integration.test_query_registration import setup_registration
from ecommerce_product_image_creator import original_query as o, query_registration as q, query_usage as u
root=Path(sys.argv[1]); root.mkdir()
patch=pytest.MonkeyPatch()
def blocked(*a,**k): raise AssertionError('network forbidden')
patch.setattr(socket.socket,'connect',blocked)
patch.setattr(socket,'create_connection',blocked)
service,req,auth,sends=setup_registration(root,patch)
(root/'request.json').write_text(json.dumps(dataclasses.asdict(req)),encoding='utf-8')
q.record_authorization(service.store,auth)
if sys.argv[2]=='transaction': q._before_commit=lambda:os._exit(81)
q.register_request(service.store,req)
if sys.argv[2]=='intent': os._exit(81)
if sys.argv[2]=='entered': o._before_query_dispatch=lambda:os._exit(81)
if sys.argv[2]=='final_check': o._after_query_final_check=lambda:os._exit(81)
if sys.argv[2]=='adapter': o._after_query_adapter=lambda:os._exit(81)
if sys.argv[2]=='response': o._after_query_received=lambda:os._exit(81)
if sys.argv[2]=='observation': o._after_query_observation=lambda:os._exit(81)
def adapter(i):
 (root/'fake-query-called').write_text('one',encoding='utf-8')
 return {'data':{'task_id':i.upstream_id,'status':'SUCCESS'}}
o._query_adapter=adapter
r=o.query_once(service.store,req.root_operation_id,req.candidate_id,req.task_id,req.revision,req.attempt_id,req.execution_digest_v2,request_id=req.request_id)
if sys.argv[2]=='billing':
 u._verify_evidence_source=lambda *_:True
 e=u.BillingEvidence(r.invocation_id,req.root_operation_id,req.candidate_id,req.attempt_id,req.execution_digest_v2,req.provider,req.account_scope,'fixture-event','0.2','THB',None,None,'fixture')
 u.record_billing_evidence(service.store,e)
 os._exit(81)
os._exit(82)
"""
    root = tmp_path / window
    child = subprocess.run(
        [sys.executable, "-c", script, str(root), window],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 81, child.stderr
    assert (
        json.loads((root / "request.json").read_text(encoding="utf-8"))["request_id"]
        == "req-1"
    )
    from ecommerce_product_image_creator.storage import Store

    store = Store(root / "state.sqlite")
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM query_request").fetchone() == (
            (0 if window == "transaction" else 1),
        )
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (
            (0 if window == "transaction" else 1),
        )
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == ((0 if window == "transaction" else 1),)
        assert db.execute("SELECT count(*) FROM query_billing_event").fetchone() == (
            (1 if window == "billing" else 0),
        )
        assert db.execute("SELECT count(*) FROM recovery_observation").fetchone() == (
            (1 if window in ("observation", "billing") else 0),
        )
        if window != "transaction":
            expected = (
                "intent"
                if window == "intent"
                else "entered"
                if window in ("entered", "final_check", "adapter")
                else "response_received"
                if window == "response"
                else "observed"
            )
            assert db.execute("SELECT status FROM query_invocation").fetchone() == (
                expected,
            )
    assert (root / "fake-query-called").exists() == (
        window in ("adapter", "response", "observation", "billing")
    )
    if window in ("transaction", "intent"):
        return
    replay = r"""
import json,sys
from pathlib import Path
from ecommerce_product_image_creator import original_query as o, query_registration as q, recovery
from ecommerce_product_image_creator.storage import Store
root=Path(sys.argv[1]); r=q.QueryRequest(**json.loads((root/'request.json').read_text(encoding='utf-8')))
o._verify_original_binding=lambda *_:True
o._query_account_scope=lambda _:'test-account'
q._verify_query_authorization=lambda *_:True
recovery._verify_observation_source=lambda *_:True
def forbidden(i):
 (root/'replay-called').write_text('bad',encoding='utf-8')
 raise AssertionError('replayed adapter')
o._query_adapter=forbidden
x=o.query_once(Store(root/'state.sqlite'),r.root_operation_id,r.candidate_id,r.task_id,r.revision,r.attempt_id,r.execution_digest_v2,request_id=r.request_id)
print(x.state)
"""
    again = subprocess.run(
        [sys.executable, "-c", replay, str(root)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert again.stdout.strip() == (
        "succeeded" if window in ("observation", "billing") else "unknown"
    )
    assert not (root / "replay-called").exists()
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (1,)


def test_terminal_appearing_after_intent_stops_first_query(tmp_path, monkeypatch):
    import dataclasses

    from ecommerce_product_image_creator import recovery

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    query_registration.record_authorization(service.store, auth)
    first = query_registration.register_request(service.store, req)
    other_req = dataclasses.replace(
        req, request_id="req-2", query_authorization_reference="auth-2"
    )
    other_auth = dataclasses.replace(
        auth, request_id="req-2", authorization_id="auth-2"
    )
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            calls.append(i.upstream_id)
            or {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}
        ),
    )

    def interleave():
        monkeypatch.setattr(original_query, "_before_query_dispatch", lambda: None)
        query_registration.record_authorization(service.store, other_auth)
        query_registration.register_request(service.store, other_req)
        assert (
            original_query.query_once(
                service.store,
                req.root_operation_id,
                req.candidate_id,
                req.task_id,
                req.revision,
                req.attempt_id,
                req.execution_digest_v2,
                request_id=other_req.request_id,
            ).state
            == "succeeded"
        )

    monkeypatch.setattr(original_query, "_before_query_dispatch", interleave)
    result = original_query.query_once(
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
        request_id=req.request_id,
    )
    assert result.state == "conflict" and result.reason == "original_terminal_changed"
    assert calls == ["fixture-upstream"]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT status FROM query_invocation WHERE invocation_id=?",
            (first.invocation_id,),
        ).fetchone() == ("entered",)


@pytest.mark.parametrize("when", ["after_final_check", "inside_adapter"])
def test_late_revocation_does_not_claim_remote_cancellation(
    tmp_path, monkeypatch, when
):
    from ecommerce_product_image_creator import recovery

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(query_registration, "_verify_query_revocation", lambda *_: True)
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)

    def revoke():
        query_registration.revoke_authorization(
            service.store, auth.authorization_id, "test-revoke"
        )

    if when == "after_final_check":
        monkeypatch.setattr(original_query, "_after_query_final_check", revoke)
    calls = []

    def adapter(i):
        calls.append(i.upstream_id)
        if when == "inside_adapter":
            revoke()
        return {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}

    monkeypatch.setattr(original_query, "_query_adapter", adapter)
    args = (
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    first = original_query.query_once(*args, request_id=req.request_id)
    replay = original_query.query_once(*args, request_id=req.request_id)
    assert first.state == replay.state == "succeeded" and calls == ["fixture-upstream"]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT status FROM query_invocation WHERE invocation_id=?",
            (intent.invocation_id,),
        ).fetchone() == ("observed",)
        assert db.execute("SELECT count(*) FROM query_dispatch_stop").fetchone() == (0,)


def test_response_without_observation_is_visible_to_readonly_evaluator(
    tmp_path, monkeypatch
):
    from ecommerce_product_image_creator import recovery_evaluation

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    query_registration.register_request(service.store, req)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_obs BEFORE INSERT ON recovery_observation BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    result = original_query.query_once(
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
        request_id=req.request_id,
    )
    assert result.state == "unknown"
    view = recovery_evaluation.evaluate(
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.stage,
        req.attempt_id,
        req.execution_digest_v2,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert (
        view["action"] == "stop"
        and view["reason"] == "query_response_evidence_incomplete"
    )
    assert view["query_cost"]["cost_status"] == "unknown"


def test_late_billing_events_are_scoped_and_do_not_change_generation_cost(
    tmp_path, monkeypatch
):
    import dataclasses

    from ecommerce_product_image_creator import generation_cost, query_usage, recovery

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(query_usage, "_verify_evidence_source", lambda *_: True)
    current = ["test-account"]
    monkeypatch.setattr(original_query, "_query_account_scope", lambda _: current[0])
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    gen_before = generation_cost.summarize_generation_cost(service.store, req.task_id)
    args = (
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    query_registration.record_authorization(service.store, auth)
    first_intent = query_registration.register_request(service.store, req)
    assert (
        original_query.query_once(*args, request_id=req.request_id).state == "succeeded"
    )
    assert (
        query_usage.summarize_query_evidence(
            service.store, req.root_operation_id, req.candidate_id, req.attempt_id
        )["cost_status"]
        == "unknown"
    )
    evidence = query_usage.BillingEvidence(
        first_intent.invocation_id,
        req.root_operation_id,
        req.candidate_id,
        req.attempt_id,
        req.execution_digest_v2,
        req.provider,
        req.account_scope,
        "event-1",
        "0.2",
        "THB",
        None,
        None,
        "fixture",
    )
    assert query_usage.record_billing_evidence(service.store, evidence) == "event-1"
    assert query_usage.record_billing_evidence(service.store, evidence) == "event-1"
    with pytest.raises(ValueError, match="billing_event_conflict"):
        query_usage.record_billing_evidence(
            service.store, dataclasses.replace(evidence, amount="0.3")
        )
    query_usage.record_billing_evidence(
        service.store, dataclasses.replace(evidence, event_id="event-2")
    )
    current[0] = "other-account"
    req2 = dataclasses.replace(
        req,
        request_id="req-2",
        query_authorization_reference="auth-2",
        account_scope=current[0],
    )
    auth2 = dataclasses.replace(
        auth, authorization_id="auth-2", request_id="req-2", account_scope=current[0]
    )
    query_registration.record_authorization(service.store, auth2)
    second_intent = query_registration.register_request(service.store, req2)
    assert (
        original_query.query_once(*args, request_id=req2.request_id).state
        == "succeeded"
    )
    query_usage.record_billing_evidence(
        service.store,
        dataclasses.replace(
            evidence,
            invocation_id=second_intent.invocation_id,
            account_scope=current[0],
        ),
    )
    summary = query_usage.summarize_query_evidence(
        service.store, req.root_operation_id, req.candidate_id, req.attempt_id
    )
    assert summary["invocation_count"] == 2 and summary["known_event_amounts"] == {
        "THB": "0.6"
    }
    assert (
        summary["cost_status"] == "unknown"
        and summary["billing_completeness"] == "unverified"
    )
    assert (
        generation_cost.summarize_generation_cost(service.store, req.task_id)
        == gen_before
    )


def test_terminal_observation_after_adapter_started_preserves_conflict(
    tmp_path, monkeypatch
):
    import dataclasses

    from ecommerce_product_image_creator import recovery

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    query_registration.record_authorization(service.store, auth)
    first = query_registration.register_request(service.store, req)
    req2 = dataclasses.replace(
        req, request_id="req-2", query_authorization_reference="auth-2"
    )
    auth2 = dataclasses.replace(auth, request_id="req-2", authorization_id="auth-2")
    query_registration.record_authorization(service.store, auth2)
    query_registration.register_request(service.store, req2)
    args = (
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    calls = []

    def adapter(i):
        calls.append("outer")
        monkeypatch.setattr(
            original_query,
            "_query_adapter",
            lambda inner: (
                calls.append("inner")
                or {"data": {"task_id": inner.upstream_id, "status": "SUCCESS"}}
            ),
        )
        assert (
            original_query.query_once(*args, request_id=req2.request_id).state
            == "succeeded"
        )
        return {"data": {"task_id": i.upstream_id, "status": "FAILURE"}}

    monkeypatch.setattr(original_query, "_query_adapter", adapter)
    result = original_query.query_once(*args, request_id=req.request_id)
    assert result.state == "conflict" and result.reason == "terminal_state_conflict"
    assert calls == ["outer", "inner"]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT status FROM query_invocation WHERE invocation_id=?",
            (first.invocation_id,),
        ).fetchone() == ("observed",)
        assert db.execute(
            "SELECT remote_state FROM recovery_observation ORDER BY observation_id"
        ).fetchall() == [("succeeded",), ("conflict",)]


def test_stopped_evidence_survives_new_process(tmp_path, monkeypatch):
    import dataclasses
    import json
    import sqlite3
    import subprocess
    import sys

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)
    monkeypatch.setattr(query_registration, "_verify_query_revocation", lambda *_: True)
    query_registration.revoke_authorization(
        service.store, auth.authorization_id, "test-revoke"
    )
    args = (
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    assert (
        original_query.query_once(*args, request_id=req.request_id).state == "stopped"
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT entered,reason,source FROM query_dispatch_stop"
        ).fetchone() == (0, "revoked", "server_pre_dispatch")
        with pytest.raises(sqlite3.IntegrityError, match="immutable query stop"):
            db.execute("DELETE FROM query_dispatch_stop")
    code = "import json,sys; from pathlib import Path; from ecommerce_product_image_creator import original_query as o,query_registration as q; from ecommerce_product_image_creator.storage import Store; o._verify_original_binding=lambda *_:True; o._query_account_scope=lambda _:'test-account'; q._verify_query_authorization=lambda *_:True; q._verify_query_revocation=lambda *_:True; o._query_adapter=lambda *_: (_ for _ in ()).throw(AssertionError('must not call')); r=q.QueryRequest(**json.loads(sys.argv[2])); z=o.query_once(Store(Path(sys.argv[1])),r.root_operation_id,r.candidate_id,r.task_id,r.revision,r.attempt_id,r.execution_digest_v2,request_id=r.request_id); print(z.state,z.invocation_id)"
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
    assert child.stdout.strip() == f"stopped {intent.invocation_id}"


def test_legacy_intent_without_observation_baseline_is_readable_but_cannot_execute(
    tmp_path, monkeypatch
):
    from ecommerce_product_image_creator.storage import Store

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)
    with service.store._connect() as db:
        db.execute("DROP TABLE query_request_observation_baseline")
    reopened = Store(service.store.path)
    calls = []
    monkeypatch.setattr(original_query, "_query_adapter", lambda i: calls.append(i))
    result = original_query.query_once(
        reopened,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
        request_id=req.request_id,
    )
    assert result.state == "unverified" and result.reason == "query_baseline_missing"
    assert result.invocation_id == intent.invocation_id and not calls
    assert query_registration.register_request(reopened, req).created is False
