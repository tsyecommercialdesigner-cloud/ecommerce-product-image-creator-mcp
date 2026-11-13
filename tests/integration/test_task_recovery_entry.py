"""Task-level recovery uses the existing guarded query and read-only views."""

from typing import Any

import pytest

from ecommerce_product_image_creator import (
    original_query,
    query_registration,
    recovery,
    tasks,
)

from .test_linked_atomic_submission import linked_context  # noqa: F401
from .test_query_registration import setup_registration


def identity(req):
    return (
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.stage,
        req.attempt_id,
        req.execution_digest_v2,
    )


def test_evaluate_is_read_only(tmp_path, monkeypatch):
    service, req, _, sends = setup_registration(tmp_path, monkeypatch)
    with service.store._connect() as db:
        before = list(db.iterdump())
    result = tasks.recover_task_once(
        service.store, *identity(req), artifact_root=tmp_path, budget=service.budget
    )
    assert result["next_action"] == "query_original"
    assert result["action_executed"] is False
    with service.store._connect() as db:
        assert list(db.iterdump()) == before
    assert sends == [1]


@pytest.mark.parametrize(
    "status,expected",
    [("SUCCESS", "succeeded"), ("IN_PROGRESS", "running"), ("FAILURE", "failed")],
)
def test_registered_query_and_replay(tmp_path, monkeypatch, status, expected):
    service, req, auth, sends = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            calls.append(i.upstream_id)
            or {"data": {"task_id": i.upstream_id, "status": status}}
        ),
    )
    query_registration.record_authorization(service.store, auth)
    registered = query_registration.register_request(service.store, req)
    args = (service.store, *identity(req))
    first = tasks.recover_task_once(
        *args,
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    replay = tasks.recover_task_once(
        *args,
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert first["recovery"]["state"] == replay["recovery"]["state"] == expected
    assert first["action_executed"] is True
    assert replay["action_executed"] is False
    assert (
        first["query"]["invocation_id"]
        == replay["query"]["invocation_id"]
        == registered.invocation_id
    )
    assert first["recovery"]["observation_id"] == replay["recovery"]["observation_id"]
    assert calls == ["fixture-upstream"] and sends == [1]
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (1,)
    if expected == "succeeded":
        assert first["next_action"] == "assess_original_artifact"
        assert first["artifact_assessment"]["state"] != "already_available"


def test_missing_request_cannot_query(tmp_path, monkeypatch):
    service, req, _, _ = setup_registration(tmp_path, monkeypatch)
    result = tasks.recover_task_once(
        service.store,
        *identity(req),
        action="query_original",
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert result["reason"] == "query_request_required"
    assert result["action_executed"] is False


def test_timeout_does_not_create_generation(tmp_path, monkeypatch):
    service, req, auth, sends = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)

    def timed_out(_):
        raise TimeoutError("fixture")

    monkeypatch.setattr(original_query, "_query_adapter", timed_out)
    query_registration.record_authorization(service.store, auth)
    query_registration.register_request(service.store, req)
    with service.store._connect() as db:
        before = db.execute("SELECT * FROM attempt").fetchall()
    result = tasks.recover_task_once(
        service.store,
        *identity(req),
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert result["recovery"]["state"] == "unknown"
    assert result["action_executed"] is True
    assert result["duplicate_generation_risk"]
    with service.store._connect() as db:
        assert db.execute("SELECT * FROM attempt").fetchall() == before
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
    assert sends == [1]


def test_missing_trusted_id_stops_and_reports_risk(tmp_path, monkeypatch):
    service, req, _, sends = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: False)
    result = tasks.recover_task_once(
        service.store, *identity(req), artifact_root=tmp_path, budget=service.budget
    )
    assert result["next_action"] == "manual_risk_review"
    assert result["trusted_upstream_id"] is None
    assert result["duplicate_generation_risk"] is True
    assert result["human_intervention_required"] is True
    assert sends == [1]


def test_default_verifier_rejects_without_fake_query(tmp_path, monkeypatch):
    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    query_registration.register_request(service.store, req)
    monkeypatch.setattr(
        query_registration, "_verify_query_authorization", lambda *_: False
    )
    result = tasks.recover_task_once(
        service.store,
        *identity(req),
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert result["query"]["state"] == "unverified"
    with service.store._connect() as db:
        assert db.execute("SELECT status FROM query_invocation").fetchone() == (
            "intent",
        )


def test_scope_and_revision_mismatch_cannot_query(tmp_path, monkeypatch):
    service, req, _, _ = setup_registration(tmp_path, monkeypatch)
    for index, wrong in (
        (0, "wrong-root"),
        (1, "wrong-candidate"),
        (3, 42),
        (4, "mock"),
        (5, "wrong-attempt"),
    ):
        ident: list[Any] = list(identity(req))
        ident[index] = wrong
        with pytest.raises(ValueError):
            tasks.recover_task_once(
                service.store,
                *ident,
                action="query_original",
                request_id=req.request_id,
                artifact_root=tmp_path,
                budget=service.budget,
            )


def test_linked_operation_identity_is_separate(linked_context, tmp_path):  # noqa: F811
    from ecommerce_product_image_creator import offline_submission

    service, args, original, calls = linked_context
    linked = offline_submission.submit_offline(
        service, *args, risk_authorization_id="risk-1"
    )
    with service.store._connect() as db:
        candidate = db.execute(
            "SELECT linked_candidate FROM risk_consumption WHERE linked_attempt=?",
            (linked.attempt_id,),
        ).fetchone()[0]
        before = (
            db.execute("SELECT * FROM attempt ORDER BY rowid").fetchall(),
            db.execute("SELECT * FROM attempt_counter ORDER BY rowid").fetchall(),
            db.execute("SELECT * FROM risk_consumption").fetchall(),
        )
    original_view = tasks.recover_task_once(
        service.store,
        "op",
        "source-candidate",
        args[0].task_id,
        1,
        args[4].stage,
        original.attempt_id,
        args[4].execution_digest,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    linked_view = tasks.recover_task_once(
        service.store,
        "linked-op",
        candidate,
        args[0].task_id,
        1,
        args[4].stage,
        linked.attempt_id,
        args[4].execution_digest,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert original_view["operation_role"] == "original"
    assert linked_view["operation_role"] == "linked"
    assert original_view["linked_evidence"] == linked_view["linked_evidence"]
    assert original_view["linked_evidence"][0]["linked_candidate"] == candidate
    assert original_view["linked_evidence"][0]["risk_authorization_id"] == "risk-1"
    with service.store._connect() as db:
        assert (
            db.execute("SELECT * FROM attempt ORDER BY rowid").fetchall(),
            db.execute("SELECT * FROM attempt_counter ORDER BY rowid").fetchall(),
            db.execute("SELECT * FROM risk_consumption").fetchall(),
        ) == before
    assert len(calls) == 2


@pytest.mark.parametrize("condition", ["revoked", "expired", "entered"])
def test_stale_authorization_and_uncertain_invocation_stop(
    tmp_path, monkeypatch, condition
):
    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    registered = query_registration.register_request(service.store, req)
    if condition == "revoked":
        monkeypatch.setattr(
            query_registration, "_verify_query_revocation", lambda *_: True
        )
        query_registration.revoke_authorization(
            service.store, auth.authorization_id, "test"
        )
    elif condition == "expired":
        monkeypatch.setattr(
            query_registration.time, "time", lambda: auth.expires_at + 1
        )
    else:
        with service.store._connect() as db:
            db.execute(
                "UPDATE query_invocation SET status='entered' WHERE invocation_id=?",
                (registered.invocation_id,),
            )
    result = tasks.recover_task_once(
        service.store,
        *identity(req),
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert result["query"]["state"] == (
        "unknown" if condition == "entered" else "stopped"
    )
    assert result["action_executed"] is False
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (1,)


def test_task_query_billing_replay_does_not_change_generation_cost(
    tmp_path, monkeypatch
):
    from ecommerce_product_image_creator import generation_cost, query_usage

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(query_usage, "_verify_evidence_source", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            calls.append(i.upstream_id)
            or {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}
        ),
    )
    query_registration.record_authorization(service.store, auth)
    registered = query_registration.register_request(service.store, req)
    before = generation_cost.summarize_generation_cost(service.store, req.task_id)
    args = (service.store, *identity(req))
    first = tasks.recover_task_once(
        *args,
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert first["query_cost"]["cost_status"] == "unknown"
    event = query_usage.BillingEvidence(
        registered.invocation_id,
        req.root_operation_id,
        req.candidate_id,
        req.attempt_id,
        req.execution_digest_v2,
        req.provider,
        req.account_scope,
        "fixture-event",
        "0.2",
        "THB",
        None,
        None,
        "fixture",
    )
    query_usage.record_billing_evidence(service.store, event)
    query_usage.record_billing_evidence(service.store, event)
    replay = tasks.recover_task_once(
        *args,
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert replay["query_cost"]["known_event_amounts"] == {"THB": "0.2"}
    assert replay["query_cost"]["billing_completeness"] == "unverified"
    assert (
        generation_cost.summarize_generation_cost(service.store, req.task_id) == before
    )
    assert calls == ["fixture-upstream"]


def test_incomplete_response_does_not_requery_from_task_entry(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import recovery_evaluation

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    registered = query_registration.register_request(service.store, req)
    with service.store._connect() as db:
        db.execute(
            "UPDATE query_invocation SET status='response_received' WHERE invocation_id=?",
            (registered.invocation_id,),
        )
    result = tasks.recover_task_once(
        service.store,
        *identity(req),
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert result["query"]["state"] == "unknown"
    assert result["next_action"] == "stop"
    assert result["incomplete_query"]["status"] == "response_received"
    assert result["action_executed"] is False
    assert (
        recovery_evaluation.evaluate(
            service.store, *identity(req), artifact_root=tmp_path, budget=service.budget
        )["reason"]
        == "query_response_evidence_incomplete"
    )


def test_observation_conflict_stops_task_entry(tmp_path, monkeypatch):
    import dataclasses

    service, req, auth, sends = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    query_registration.record_authorization(service.store, auth)
    query_registration.register_request(service.store, req)
    req2 = dataclasses.replace(
        req, request_id="req-2", query_authorization_reference="auth-2"
    )
    auth2 = dataclasses.replace(auth, request_id="req-2", authorization_id="auth-2")
    query_registration.record_authorization(service.store, auth2)
    query_registration.register_request(service.store, req2)
    calls = []

    def adapter(i):
        calls.append("outer")
        monkeypatch.setattr(
            original_query,
            "_query_adapter",
            lambda x: (
                calls.append("inner")
                or {"data": {"task_id": x.upstream_id, "status": "SUCCESS"}}
            ),
        )
        assert (
            original_query.query_once(
                service.store,
                req2.root_operation_id,
                req2.candidate_id,
                req2.task_id,
                req2.revision,
                req2.attempt_id,
                req2.execution_digest_v2,
                request_id=req2.request_id,
            ).state
            == "succeeded"
        )
        return {"data": {"task_id": i.upstream_id, "status": "FAILURE"}}

    monkeypatch.setattr(original_query, "_query_adapter", adapter)
    result = tasks.recover_task_once(
        service.store,
        *identity(req),
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert result["recovery"]["state"] == "conflict"
    assert result["next_action"] == "stop"
    assert calls == ["outer", "inner"] and sends == [1]


def test_observed_replay_in_new_process_is_read_only(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    query_registration.record_authorization(service.store, auth)
    query_registration.register_request(service.store, req)
    first = tasks.recover_task_once(
        service.store,
        *identity(req),
        action="query_original",
        request_id=req.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    script = r"""
import json,socket,sys
from pathlib import Path
from ecommerce_product_image_creator import original_query as o, recovery as r, query_registration as q, tasks
from ecommerce_product_image_creator.storage import Store, ResourceBudget
def forbidden(*a,**k): raise AssertionError("network or adapter forbidden")
socket.socket.connect=forbidden
socket.create_connection=forbidden
o._query_adapter=forbidden
o._verify_original_binding=lambda *_:True
o._query_account_scope=lambda _:'test-account'
q._verify_query_authorization=lambda *_:True
r._verify_observation_source=lambda *_:True
ident=json.loads(sys.argv[2])
x=tasks.recover_task_once(Store(Path(sys.argv[1])),*ident,action='query_original',request_id='req-1',artifact_root=Path(sys.argv[3]),budget=ResourceBudget(100000,10000,100000))
print(json.dumps([x['recovery']['state'],x['recovery']['observation_id'],x['action_executed']]))
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(service.store.path),
            json.dumps(identity(req)),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(child.stdout) == [
        "succeeded",
        first["recovery"]["observation_id"],
        False,
    ]
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (1,)
