"""Task-entry recovery acceptance with controlled historical side effects."""

import socket
import time

import pytest

from ecommerce_product_image_creator import (
    artifact_locator,
    original_query,
    query_registration,
    recovery,
    recovery_intent,
    tasks,
)

from .test_linked_atomic_submission import linked_context  # noqa: F401
from .test_recovery_acceptance import make_form


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def test_local_staging_failure_preserves_remote_success_and_review(
    tmp_path, monkeypatch
):
    from ecommerce_product_image_creator.execution_candidates import read_candidate

    service, ident, sends, queries, path = make_form(tmp_path, monkeypatch, True)
    data = path.read_bytes()
    candidate = read_candidate(service.store, "a")
    snap = candidate.snapshot
    with service.store._connect() as db:
        observation = db.execute(
            "SELECT observation_id FROM recovery_observation WHERE attempt_id=?",
            (ident[5],),
        ).fetchone()[0]
    path.unlink()
    monkeypatch.setattr(
        artifact_locator,
        "_verify_locator_source",
        lambda i, e: i.attempt_id == e.attempt_id and e.scope == "fixture-account",
    )
    monkeypatch.setattr(
        artifact_locator,
        "_verify_fixture_access",
        lambda e: e.scope == "fixture-account",
    )
    monkeypatch.setattr(
        artifact_locator,
        "_verify_fixture_content",
        lambda e, value: e.artifact_identity == "image-1" and value == data,
    )
    monkeypatch.setattr(
        artifact_locator, "_fetch_fixture", lambda *_: (data, "image/png")
    )
    evidence = artifact_locator.LocatorEvidence(
        "locator",
        "op",
        "a",
        ident[2],
        ident[3],
        ident[4],
        ident[5],
        ident[6],
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "fixture-account",
        "fixture-upstream",
        observation,
        "image-1",
        "fixture_handle",
        "fixture-source",
        "fixture-handle",
        time.time(),
        time.time() + 300,
    )
    artifact_locator.register(service.store, evidence)
    recovery_intent.register_pending(
        service.store,
        "restore",
        *ident,
        observation,
        artifact_id="artifact",
        artifact_root=tmp_path,
        budget=service.budget,
    )

    def fail(phase):
        if phase == "partial_write":
            raise OSError("isolated write failure")

    monkeypatch.setattr(recovery_intent, "_stage_hook", fail)
    with pytest.raises(ValueError, match="stage_io_failed"):
        recovery_intent.stage_pending(
            service.store, "restore", tmp_path, service.budget
        )
    with service.store._connect() as db:
        before = list(db.iterdump())
        assert db.execute(
            "SELECT state FROM artifact_recovery_intent WHERE recovery_id='restore'"
        ).fetchone() == ("pending",)
    result = tasks.recover_task_once(
        service.store, *ident, artifact_root=tmp_path, budget=service.budget
    )
    assert result["recovery"]["state"] == "succeeded"
    assert result["next_action"] == "assess_original_artifact"
    assert result["reason"] == "artifact_missing_or_invalid"
    assert result["artifacts"][0]["file"]["state"] == "missing"
    assert result["artifacts"][0]["delivery_status"] == "delivered"
    assert result["artifacts"][0]["review"]["state"] == "accept"
    assert result["artifacts"][0]["review"]["trust_state"] == "unverified"
    assert result["action_executed"] is False
    with service.store._connect() as db:
        assert list(db.iterdump()) == before
    assert len(sends) == len(queries) == 1 and not path.exists()


@pytest.mark.parametrize("mode", ["stopped", "unknown"])
def test_original_late_success_does_not_change_linked_attempt(
    linked_context,  # noqa: F811
    tmp_path,
    monkeypatch,
    mode,
):
    from ecommerce_product_image_creator import offline_submission, risk_reservation
    from ecommerce_product_image_creator.domain import UpstreamStatus

    service, args, source, sends = linked_context
    if mode == "stopped":
        monkeypatch.setattr(
            risk_reservation, "_verify_revocation_source", lambda *_: True
        )
        monkeypatch.setattr(
            offline_submission,
            "_after_intent_recorded",
            lambda: risk_reservation.revoke(service.store, "risk-1", "fixture revoke"),
        )
    linked = offline_submission.submit_offline(
        service, *args, risk_authorization_id="risk-1"
    )
    # Simulate the separately verified delayed ID using the existing controlled record API.
    service.store.record_upstream(source.attempt_id, UpstreamStatus.UNKNOWN, "late-id")
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == source.attempt_id and i.upstream_id == "late-id",
    )
    monkeypatch.setattr(
        original_query, "_query_account_scope", lambda _: "fixture-account"
    )
    monkeypatch.setattr(
        recovery, "_verify_observation_source", lambda i, row: row[6] == i.attempt_id
    )
    monkeypatch.setattr(
        query_registration,
        "_verify_query_authorization",
        lambda _db, e: e.source_reference == "fixture-query-auth",
    )
    queries = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            queries.append(i.upstream_id)
            or {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}
        ),
    )
    snap = args[4]
    request = query_registration.QueryRequest(
        "query-1",
        snap.task_id,
        1,
        snap.stage,
        "op",
        "source-candidate",
        source.attempt_id,
        snap.execution_digest,
        "late-id",
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "fixture-account",
        "query_original",
        "query-auth",
    )
    auth = query_registration.QueryAuthorization(
        "query-auth",
        "query-1",
        source.attempt_id,
        snap.execution_digest,
        "candidate_gateway",
        "fixture-account",
        "query_original",
        1,
        "fixture-query-auth",
        time.time() + 300,
        "fixture cost unknown",
    )
    query_registration.record_authorization(service.store, auth)
    registered = query_registration.register_request(service.store, request)
    with service.store._connect() as db:
        linked_candidate = db.execute(
            "SELECT linked_candidate FROM risk_consumption WHERE linked_attempt=?",
            (linked.attempt_id,),
        ).fetchone()[0]
        before = {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "attempt",
                "artifact",
                "attempt_counter",
                "risk_consumption",
                "form_review",
            )
        }
        query_consumption = db.execute(
            "SELECT * FROM query_authorization_consumption"
        ).fetchall()
    original_identity = (
        "op",
        "source-candidate",
        snap.task_id,
        1,
        snap.stage,
        source.attempt_id,
        snap.execution_digest,
    )
    linked_identity = (
        "linked-op",
        linked_candidate,
        snap.task_id,
        1,
        snap.stage,
        linked.attempt_id,
        snap.execution_digest,
    )
    original = tasks.recover_task_once(
        service.store,
        *original_identity,
        action="query_original",
        request_id=request.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    replay = tasks.recover_task_once(
        service.store,
        *original_identity,
        action="query_original",
        request_id=request.request_id,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    target = tasks.recover_task_once(
        service.store, *linked_identity, artifact_root=tmp_path, budget=service.budget
    )
    assert original["recovery"]["state"] == replay["recovery"]["state"] == "succeeded"
    assert original["historical_upstream_status"] == "unknown"
    assert (
        original["query"]["invocation_id"]
        == replay["query"]["invocation_id"]
        == registered.invocation_id
    )
    assert original["action_executed"] is True and replay["action_executed"] is False
    assert target["recovery"]["state"] != "succeeded"
    assert target["historical_upstream_status"] in ("unknown", "submission_uncertain")
    assert original["artifacts"] == target["artifacts"] == []
    assert target["linked_source"]["attempt_id"] == source.attempt_id
    assert original["linked_evidence"][0]["linked_attempt"] == linked.attempt_id
    if mode == "stopped":
        assert target["local_stop"] == "entered_then_stopped"
    assert queries == ["late-id"]
    assert len(sends) == (1 if mode == "stopped" else 2)
    with service.store._connect() as db:
        assert {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in before
        } == before
        assert (
            db.execute("SELECT * FROM query_authorization_consumption").fetchall()
            == query_consumption
        )
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (2,)


def test_two_existing_artifacts_remain_bound_to_their_attempts(
    linked_context,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from .test_recovery_acceptance import (
        test_original_and_linked_keep_separate_artifacts,
    )

    test_original_and_linked_keep_separate_artifacts(
        linked_context, tmp_path, monkeypatch
    )
    service, args, source, sends = linked_context
    with service.store._connect() as db:
        linked_root, candidate, attempt = db.execute(
            "SELECT linked_operation,linked_candidate,linked_attempt FROM risk_consumption"
        ).fetchone()
        before = list(db.iterdump())
    snap = args[4]
    original = tasks.recover_task_once(
        service.store,
        "op",
        "source-candidate",
        snap.task_id,
        1,
        snap.stage,
        source.attempt_id,
        snap.execution_digest,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    linked = tasks.recover_task_once(
        service.store,
        linked_root,
        candidate,
        snap.task_id,
        1,
        snap.stage,
        attempt,
        snap.execution_digest,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert [a["artifact_id"] for a in original["artifacts"]] == ["source-art"]
    assert [a["artifact_id"] for a in linked["artifacts"]] == ["linked-art"]
    assert original["artifacts"][0]["file"]["state"] == "verified"
    assert linked["artifacts"][0]["file"]["state"] == "verified"
    assert original["artifacts"][0]["review"]["state"] == "pending"
    assert linked["artifacts"][0]["review"]["state"] == "pending"
    assert original["action_executed"] is linked["action_executed"] is False
    with service.store._connect() as db:
        assert list(db.iterdump()) == before
    assert len(sends) == 2
