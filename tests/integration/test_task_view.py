import dataclasses
import socket
from pathlib import Path

import pytest

from ecommerce_product_image_creator import (
    generation_cost,
    offline_submission,
    original_query,
    recovery,
    timing_evidence,
)
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.task_view import ReadOnlyStore, read_task_view

from .test_cross_candidate_submission import setup_cross
from .test_linked_atomic_submission import linked_context  # noqa: F401
from .test_mock_workflow import _registered_fixture
from .test_original_query import setup_query


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def test_form_mock_history_integrity_and_no_mutation(tmp_path, monkeypatch):
    store, budget, form, mock, path, _ = _registered_fixture(tmp_path, monkeypatch)
    with store._connect() as db:
        tables = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        before = {
            table: db.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables
        }
    view = read_task_view(ReadOnlyStore(store.path), form["task_id"], tmp_path, budget)
    assert view["current_revision"] == mock["revision"]
    assert view["current_stage"] == "mock"
    assert [r["stage"] for r in view["revisions"]] == ["form", "mock"]
    attempts = {a["attempt_id"]: a for a in view["attempts"]}
    assert attempts["form-attempt"]["artifacts"][0]["review"]["decision"] == "accept"
    assert attempts["mock-attempt"]["artifacts"][0]["review"]["state"] == "pending"
    assert attempts["mock-attempt"]["artifacts"][0]["file"]["state"] == "verified"
    assert all(a["time"]["duration_status"] == "unknown" for a in attempts.values())
    assert str(tmp_path) not in str(view)
    with store._connect() as db:
        assert {
            table: db.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables
        } == before
    path.write_bytes(b"corrupt")
    changed = read_task_view(
        ReadOnlyStore(store.path),
        form["task_id"],
        tmp_path,
        budget,
        attempt_id="mock-attempt",
    )
    assert len(changed["attempts"]) == 1
    art = changed["attempts"][0]["artifacts"][0]
    assert (
        art["delivery_status"] == "delivered"
        and art["file"]["state"] == "invalid_or_unreadable"
    )
    with pytest.raises(ValueError, match="attempt_not_found"):
        read_task_view(
            ReadOnlyStore(store.path),
            form["task_id"],
            tmp_path,
            budget,
            attempt_id="other",
        )


def test_task_costs_are_separate_and_not_complete(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    snap = args[4]
    event = generation_cost.GenerationCostEvidence(
        "view-evidence",
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
        "fixture-account",
        "fixture",
        "reference",
        "event",
        "0.15",
        "THB",
    )
    monkeypatch.setattr(
        generation_cost,
        "_verify_evidence_source",
        lambda _db, e: e.source == "fixture" and e.account_scope == "fixture-account",
    )
    generation_cost.record_generation_cost(service.store, event)
    generation_cost.record_generation_cost(
        service.store,
        dataclasses.replace(
            event,
            evidence_id="usd-evidence",
            event_id="usd-event",
            currency="USD",
            amount="2",
        ),
    )
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
            "data": {"status": "SUCCESS", "task_id": identity.upstream_id}
        },
    )
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
    )
    assert recovery.recover_once(service.store, *identity).state == "succeeded"
    assert recovery.recover_once(service.store, *identity).state == "succeeded"
    timing = timing_evidence.TimingEvidence(
        "op",
        "a",
        args[0].task_id,
        1,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
        1000.0,
        1004.25,
        "fixture-clock",
    )
    with pytest.raises(ValueError, match="source_unverified"):
        timing_evidence.record_timing(service.store, timing)
    monkeypatch.setattr(
        timing_evidence, "_verify_timing_source", lambda _db, e: e == timing
    )
    timing_evidence.record_timing(service.store, timing)
    view = read_task_view(
        ReadOnlyStore(service.store.path), args[0].task_id, tmp_path, service.budget
    )
    assert view["generation_cost"]["known_subtotal"] == {"THB": "0.15", "USD": "2"}
    assert view["generation_cost"]["total_verified"] is False
    item = view["attempts"][0]
    assert item["generation_cost"]["known_subtotal"] == {"THB": "0.15", "USD": "2"}
    assert item["query_cost"]["cost_status"] == "unknown"
    assert item["query_cost"]["invocation_count"] == 2
    assert len(item["upstream"]["observations"]) == 2
    assert item["upstream"]["current_conclusion"] == "succeeded"
    assert item["time"]["remote_completed_at"] == 1004.25
    assert item["time"]["duration_seconds"] == 4.25
    assert item["generation_cost_evidence"][0]["source_trust"] == "configured_verifier"
    assert sends == [1]


def test_cross_candidate_history_keeps_attempt_identity(tmp_path, monkeypatch):
    service, target, first, second, third, b, sent = setup_cross(tmp_path, monkeypatch)
    fourth = offline_submission.submit_offline(service, *target, retry_of="op")
    view = read_task_view(
        ReadOnlyStore(service.store.path), target[0].task_id, tmp_path, service.budget
    )
    attempts = view["attempts"]
    assert [a["attempt_id"] for a in attempts] == [
        first.attempt_id,
        second.attempt_id,
        third.attempt_id,
        fourth.attempt_id,
    ]
    assert attempts[-1]["candidate_id"] == b.candidate_id
    assert attempts[-1]["execution_digest"] == b.snapshot.execution_digest
    assert len(sent) == 1


def test_six_attempts_restore_from_existing_offline_chain(tmp_path, monkeypatch):
    from .test_cross_candidate_scheduler import (
        test_full_six_attempts_via_explicit_single_steps,
    )

    test_full_six_attempts_via_explicit_single_steps(tmp_path, monkeypatch)
    store = ReadOnlyStore(tmp_path / "state.sqlite")
    with store._connect() as db:
        task_id = db.execute("SELECT task_id FROM task").fetchone()[0]
    view = read_task_view(
        store, task_id, tmp_path, ResourceBudget(100000, 10000, 100000)
    )
    assert [row["sequence"] for row in view["attempts"]] == [1, 2, 3, 4, 5, 6]
    assert len({row["candidate_id"] for row in view["attempts"]}) == 2


def test_linked_attempt_is_separate_and_unknown(linked_context, monkeypatch):  # noqa: F811
    service, args, original, sends = linked_context
    linked = offline_submission.submit_offline(
        service, *args, risk_authorization_id="risk-1"
    )
    view = read_task_view(
        ReadOnlyStore(service.store.path),
        args[0].task_id,
        Path(service.input_root),
        service.budget,
    )
    attempts = {a["attempt_id"]: a for a in view["attempts"]}
    assert attempts[original.attempt_id]["upstream"]["historical_status"] == "unknown"
    assert attempts[linked.attempt_id]["linked_source"]["root_operation_id"] == "op"
    assert attempts[linked.attempt_id]["root_operation_id"] == "linked-op"
    assert sends == [1, 1]


def test_legacy_empty_history_and_missing_db_do_not_create_schema(tmp_path):
    store = Store(tmp_path / "old.sqlite")
    store.create_task("old", "input", "request")
    store.create_revision("old", 1, "input")
    with store._connect() as db:
        before = db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
    view = read_task_view(
        ReadOnlyStore(store.path),
        "old",
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
    )
    assert (
        view["current_stage"] == "unknown"
        and view["generation_cost"]["total_verified"] is False
    )
    with store._connect() as db:
        assert (
            db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
            == before
        )
