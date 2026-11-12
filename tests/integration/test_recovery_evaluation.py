import pytest

from ecommerce_product_image_creator import original_query, recovery_evaluation

from .test_linked_atomic_submission import linked_context  # noqa: F401
from .test_original_query import setup_query


def test_recovery_evaluation_is_read_only_and_identity_bound(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(
        original_query, "_query_adapter", lambda *_: pytest.fail("query forbidden")
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
    with service.store._connect() as db:
        before = list(db.iterdump())
    result = recovery_evaluation.evaluate(
        service.store, *identity, artifact_root=tmp_path, budget=service.budget
    )
    assert result["action"] == "query_original"
    assert result["reason"] == "trusted_original_id"
    assert result["authorization"] == "not_granted"
    assert result["query_cost"]["billing_completeness"] == "unverified"
    with service.store._connect() as db:
        assert list(db.iterdump()) == before
    assert sends == [1]
    for index, value in [
        (0, "wrong-root"),
        (1, "wrong-candidate"),
        (5, "wrong-attempt"),
        (6, "wrong-digest"),
    ]:
        wrong: list = list(identity)
        wrong[index] = value
        with pytest.raises(ValueError):
            recovery_evaluation.evaluate(
                service.store, *wrong, artifact_root=tmp_path, budget=service.budget
            )


@pytest.mark.parametrize(
    ("status", "action", "reason"),
    [
        ("IN_PROGRESS", "wait", "original_running"),
        ("SUCCESS", "assess_original_artifact", "artifact_missing_or_invalid"),
        ("FAILURE", "review_failure_evidence", "remote_failed_not_authorization"),
    ],
)
def test_observed_states_do_not_execute(tmp_path, monkeypatch, status, action, reason):
    from ecommerce_product_image_creator import recovery

    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": status}},
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
    original_query.query_once(
        service.store,
        identity[0],
        identity[1],
        identity[2],
        identity[3],
        identity[5],
        identity[6],
    )
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda *_: pytest.fail("evaluation queried provider"),
    )
    with service.store._connect() as db:
        before = list(db.iterdump())
    result = recovery_evaluation.evaluate(
        service.store, *identity, artifact_root=tmp_path, budget=service.budget
    )
    assert (result["action"], result["reason"]) == (action, reason)
    assert result["authorization"] == "not_granted"
    with service.store._connect() as db:
        assert list(db.iterdump()) == before
    assert sends == [1]


def test_missing_or_unverified_id_is_never_trusted(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(
        original_query, "_query_adapter", lambda *_: pytest.fail("query forbidden")
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
    result = recovery_evaluation.evaluate(
        service.store, *identity, artifact_root=tmp_path, budget=service.budget
    )
    assert result["trusted_upstream_id"] is None
    assert result["action"] == "manual_risk_review"
    assert result["duplicate_generation_risk"]
    assert sends == [1]


def test_single_artifact_server_uses_readonly_store(tmp_path, monkeypatch):
    import shutil

    from mcp.server.mcpserver.exceptions import ToolError

    from ecommerce_product_image_creator import server

    from .test_mock_workflow import _registered_fixture

    state = tmp_path / "state"
    inputs = tmp_path / "inputs"
    state.mkdir()
    inputs.mkdir()
    store, _budget, form, mock, path, _ = _registered_fixture(state, monkeypatch)
    shutil.copyfile(store.path, state / "tasks.sqlite")
    monkeypatch.setenv("PRODUCT_IMAGE_STATE_DIR", str(state))
    monkeypatch.setenv("PRODUCT_IMAGE_INPUT_ROOT", str(inputs))
    monkeypatch.setenv("PRODUCT_IMAGE_MAX_INPUT_BYTES", "100000")
    monkeypatch.setenv("PRODUCT_IMAGE_MAX_PIXELS", "10000")
    monkeypatch.setenv("PRODUCT_IMAGE_MAX_DECODED_BYTES", "100000")
    statements = []
    original = server.ReadOnlyStore._connect

    def traced(self):
        db = original(self)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(server.ReadOnlyStore, "_connect", traced)
    result = server.get_product_image_task(
        form["task_id"], "mock-attempt", "mock", mock["revision"], "mock-artifact"
    )
    assert result["file"]["state"] == "verified"
    assert statements and all(
        sql.lstrip().upper().startswith(("SELECT", "BEGIN")) for sql in statements
    )
    path.unlink()
    assert (
        server.get_product_image_task(
            form["task_id"], "mock-attempt", "mock", mock["revision"], "mock-artifact"
        )["file"]["state"]
        == "missing"
    )
    path.write_bytes(b"corrupt")
    assert (
        server.get_product_image_task(
            form["task_id"], "mock-attempt", "mock", mock["revision"], "mock-artifact"
        )["file"]["state"]
        == "invalid_or_unreadable"
    )
    with pytest.raises(ToolError):
        server.get_product_image_task(
            "wrong-task", "mock-attempt", "mock", mock["revision"], "mock-artifact"
        )


def test_conflicting_observations_stop_and_keep_history(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import recovery

    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    states = iter(["SUCCESS", "FAILURE"])
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": next(states)}},
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
    for _ in range(2):
        original_query.query_once(
            service.store,
            identity[0],
            identity[1],
            identity[2],
            identity[3],
            identity[5],
            identity[6],
        )
    monkeypatch.setattr(
        original_query, "_query_adapter", lambda *_: pytest.fail("query forbidden")
    )
    result = recovery_evaluation.evaluate(
        service.store, *identity, artifact_root=tmp_path, budget=service.budget
    )
    assert result["action"] == "stop"
    assert result["reason"] == "observation_conflict"
    assert result["recovery"]["state"] == "conflict"
    assert sends == [1]


def test_linked_and_original_are_distinct(linked_context, tmp_path):  # noqa: F811
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
    digest = args[4].execution_digest
    source = recovery_evaluation.evaluate(
        service.store,
        "op",
        "source-candidate",
        args[0].task_id,
        1,
        args[4].stage,
        original.attempt_id,
        digest,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    target = recovery_evaluation.evaluate(
        service.store,
        "linked-op",
        candidate,
        args[0].task_id,
        1,
        args[4].stage,
        linked.attempt_id,
        digest,
        artifact_root=tmp_path,
        budget=service.budget,
    )
    assert source["linked_source"] is None
    assert source["linked_operations"][0]["attempt_id"] == linked.attempt_id
    assert source["duplicate_generation_risk"] is True
    assert target["linked_source"] == {"root": "op", "attempt_id": original.attempt_id}
    assert source["action"] == target["action"] == "manual_risk_review"
    assert len(calls) == 2


def test_verified_pre_send_is_evidence_not_permission(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import offline_submission
    from ecommerce_product_image_creator.execution_candidates import (
        register_root_candidate,
    )

    from .test_offline_submission import setup_context

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda _: calls.append(1)
    )

    def fail(_):
        raise ValueError("invalid input")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", fail)
    first = offline_submission.submit_offline(service, *args)
    register_root_candidate(service.store, "op", "a")
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        first.attempt_id,
        args[4].execution_digest,
    )
    result = recovery_evaluation.evaluate(
        service.store, *identity, artifact_root=tmp_path, budget=service.budget
    )
    assert result["pre_send_verified"] is True
    assert result["action"] == "review_failure_evidence"
    assert result["authorization"] == "not_granted"
    assert calls == []


def test_read_transaction_closed_when_identity_lookup_raises(tmp_path, monkeypatch):
    import sqlite3

    service, args, attempt, _ = setup_query(tmp_path, monkeypatch)
    opened = []
    connect = recovery_evaluation.ReadOnlyStore._connect

    def tracked(store):
        db = connect(store)
        opened.append(db)
        return db

    monkeypatch.setattr(recovery_evaluation.ReadOnlyStore, "_connect", tracked)

    def broken(*_):
        raise sqlite3.OperationalError("injected read failure")

    monkeypatch.setattr(recovery_evaluation, "read_candidate", broken)
    with pytest.raises(sqlite3.OperationalError):
        recovery_evaluation.evaluate(
            service.store,
            "op",
            "a",
            args[0].task_id,
            1,
            args[4].stage,
            attempt.attempt_id,
            args[4].execution_digest,
            artifact_root=tmp_path,
            budget=service.budget,
        )
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
