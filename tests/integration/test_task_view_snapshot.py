import dataclasses
import socket

import pytest

from ecommerce_product_image_creator import (
    generation_cost,
    offline_submission,
    original_query,
    recovery,
    risk_reservation,
    task_view,
)

from .test_linked_atomic_submission import linked_context  # noqa: F401
from .test_original_query import setup_query


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def test_wal_cost_write_during_view_keeps_single_snapshot(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    with service.store._connect() as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    snap = args[4]
    e = generation_cost.GenerationCostEvidence(
        "one",
        args[0].task_id,
        1,
        snap.stage,
        "op",
        "a",
        attempt.attempt_id,
        snap.execution_digest,
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "account",
        "fixture",
        "ref",
        "event-one",
        "0.15",
        "THB",
    )
    monkeypatch.setattr(
        generation_cost, "_verify_evidence_source", lambda _db, x: x.source == "fixture"
    )
    generation_cost.record_generation_cost(service.store, e)
    original = task_view.summarize_generation_cost
    fired = []

    def interleave(*args, **kwargs):
        if not fired:
            fired.append(True)
            generation_cost.record_generation_cost(
                service.store,
                dataclasses.replace(e, evidence_id="two", event_id="event-two"),
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(task_view, "summarize_generation_cost", interleave)
    view = task_view.read_task_view(
        task_view.ReadOnlyStore(service.store.path),
        args[0].task_id,
        tmp_path,
        service.budget,
    )
    assert view["generation_cost"]["known_subtotal"] == {"THB": "0.15"}
    assert view["attempts"][0]["generation_cost"]["known_subtotal"] == {"THB": "0.15"}
    assert len(sends) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM generation_cost_evidence"
        ).fetchone() == (2,)


def test_wal_new_observation_cannot_mix_into_old_snapshot(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    with service.store._connect() as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, identity: identity.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda identity: {
            "data": {"task_id": identity.upstream_id, "status": "SUCCESS"}
        },
    )
    original = task_view.resolve_recorded
    calls = []

    def interleave(*values):
        if not calls:
            calls.append(True)
            assert (
                original_query.query_once(
                    service.store,
                    "op",
                    "a",
                    args[0].task_id,
                    1,
                    attempt.attempt_id,
                    args[4].execution_digest,
                ).state
                == "succeeded"
            )
        return original(*values)

    monkeypatch.setattr(task_view, "resolve_recorded", interleave)
    view = task_view.read_task_view(
        task_view.ReadOnlyStore(service.store.path),
        args[0].task_id,
        tmp_path,
        service.budget,
    )
    upstream = view["attempts"][0]["upstream"]
    assert (
        upstream["observations"] == [] and upstream["current_conclusion"] == "unknown"
    )
    assert sends == [1]


def test_wal_review_and_attempt_and_file_have_separate_boundaries(
    tmp_path, monkeypatch
):
    import time

    from ecommerce_product_image_creator import mock_workflow

    from .test_mock_workflow import _registered_fixture

    store, budget, form, mock, path, digest = _registered_fixture(tmp_path, monkeypatch)
    with store._connect() as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    original = task_view.read_artifact
    fired = []

    def interleave(*values, **kwargs):
        if not fired:
            fired.append(True)
            store.claim_operation(
                "new-op", form["task_id"], mock["revision"], "new-digest", "new-attempt"
            )
            mock_workflow.save_mock_review(
                store,
                mock_workflow.MockReview(
                    "new-review",
                    form["task_id"],
                    mock["revision"],
                    "mock-artifact",
                    digest,
                    "accept",
                    "fixture",
                    time.time(),
                    None,
                ),
            )
            path.write_bytes(b"corrupt")
        return original(*values, **kwargs)

    monkeypatch.setattr(task_view, "read_artifact", interleave)
    view = task_view.read_task_view(
        task_view.ReadOnlyStore(store.path), form["task_id"], tmp_path, budget
    )
    attempts = {x["attempt_id"]: x for x in view["attempts"]}
    assert "new-attempt" not in attempts
    artifact = attempts["mock-attempt"]["artifacts"][0]
    assert artifact["review"]["state"] == "pending"
    assert (
        artifact["delivery_status"] == "delivered"
        and artifact["file"]["state"] == "invalid_or_unreadable"
    )
    with store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM attempt WHERE attempt_id='new-attempt'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM mock_review WHERE review_id='new-review'"
        ).fetchone() == (1,)


def test_wal_revocation_keeps_same_snapshot(
    linked_context,  # noqa: F811
    monkeypatch,
):
    service, args, _, sends = linked_context
    offline_submission.submit_offline(service, *args, risk_authorization_id="risk-1")
    with service.store._connect() as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    monkeypatch.setattr(risk_reservation, "_verify_revocation_source", lambda *_: True)
    original = task_view.summarize_generation_cost
    fired = []

    def interleave(*values, **kwargs):
        if not fired:
            fired.append(True)
            risk_reservation.revoke(service.store, "risk-1", "fixture-revoke")
        return original(*values, **kwargs)

    monkeypatch.setattr(task_view, "summarize_generation_cost", interleave)
    view = task_view.read_task_view(
        task_view.ReadOnlyStore(service.store.path),
        args[0].task_id,
        service.input_root,
        service.budget,
    )
    linked = next(
        row for row in view["attempts"] if row["root_operation_id"] == "linked-op"
    )
    assert linked["linked_source"]["revoked"] is False and sends == [1, 1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM risk_reservation_revocation"
        ).fetchone() == (1,)


def test_one_read_connection_no_writes_and_releases_after_exception(
    tmp_path, monkeypatch
):
    import sqlite3

    service, args, _attempt, sends = setup_query(tmp_path, monkeypatch)
    connect = task_view.ReadOnlyStore._connect
    statements = []
    connections = []

    def trace(store):
        db = connect(store)
        connections.append(db)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(task_view.ReadOnlyStore, "_connect", trace)
    task_view.read_task_view(
        task_view.ReadOnlyStore(service.store.path),
        args[0].task_id,
        tmp_path,
        service.budget,
    )
    assert len(connections) == 1
    assert statements[0] == "BEGIN"
    assert all(s.lstrip().upper().startswith(("BEGIN", "SELECT")) for s in statements)
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")

    def fail(*_args):
        raise RuntimeError("injected")

    monkeypatch.setattr(task_view, "resolve_recorded", fail)
    with pytest.raises(RuntimeError, match="injected"):
        task_view.read_task_view(
            task_view.ReadOnlyStore(service.store.path),
            args[0].task_id,
            tmp_path,
            service.budget,
        )
    with service.store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE task SET status=status WHERE task_id=?", (args[0].task_id,))
    assert sends == [1]


def test_large_legacy_history_and_bad_candidate_stay_auditable(tmp_path, monkeypatch):
    from ecommerce_product_image_creator.storage import ResourceBudget, Store

    store = Store(tmp_path / "history.sqlite")
    store.create_task("history", "input", "request")
    store.create_revision("history", 1, "input")
    for n in range(100):
        store.claim_operation(f"op-{n}", "history", 1, f"digest-{n}", f"attempt-{n}")
    view = task_view.read_task_view(
        task_view.ReadOnlyStore(store.path),
        "history",
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
    )
    assert len(view["attempts"]) == 100
    assert all(
        item["time"]["duration_status"] == "unknown" for item in view["attempts"]
    )

    other = tmp_path / "candidate"
    other.mkdir()
    service, args, _attempt, sends = setup_query(other, monkeypatch)
    with service.store._connect() as db:
        db.execute(
            "INSERT INTO execution_candidate SELECT 'bad',root_operation_id,task_id,revision,candidate_id,snapshot_payload,'wrong-digest',created_at FROM execution_candidate WHERE candidate_id='a'"
        )
    result = task_view.read_task_view(
        task_view.ReadOnlyStore(service.store.path),
        args[0].task_id,
        other,
        service.budget,
    )
    invalid = next(c for c in result["candidates"] if c["candidate_id"] == "bad")
    assert invalid["snapshot_status"] == "unverified" and sends == [1]
