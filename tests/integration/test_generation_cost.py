import dataclasses
import socket
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from ecommerce_product_image_creator import generation_cost, offline_submission

from .test_linked_atomic_submission import linked_context  # noqa: F401
from .test_original_query import setup_query


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def context(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    snap = args[4]
    evidence = generation_cost.GenerationCostEvidence(
        "evidence-1",
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
        "account-1",
        "fixture",
        "evidence-ref",
        "event-1",
        "0.15",
        "THB",
    )
    return service, args, attempt, sends, evidence


def allow_fixture(monkeypatch):
    monkeypatch.setattr(
        generation_cost,
        "_verify_evidence_source",
        lambda _db, e: e.source == "fixture" and e.account_scope == "account-1",
    )


def test_unknown_source_and_idempotent_event(context, monkeypatch):
    service, args, attempt, sends, e = context
    before = generation_cost.summarize_generation_cost(service.store, args[0].task_id)
    assert before["known_subtotal"] == {} and before["total_verified"] is False
    with pytest.raises(ValueError, match="unverified"):
        generation_cost.record_generation_cost(service.store, e)
    allow_fixture(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(
            pool.map(
                lambda _: generation_cost.record_generation_cost(service.store, e),
                range(2),
            )
        ) == ["evidence-1", "evidence-1"]
    assert (
        generation_cost.record_generation_cost(
            service.store,
            dataclasses.replace(
                e,
                evidence_id="evidence-copy",
                amount="0.150",
                evidence_reference="second-observation",
            ),
        )
        == "evidence-1"
    )
    with pytest.raises(ValueError, match="conflict"):
        generation_cost.record_generation_cost(
            service.store, dataclasses.replace(e, amount="0.16")
        )
    result = generation_cost.summarize_generation_cost(
        service.store, args[0].task_id, root="op", attempt_id=attempt.attempt_id
    )
    assert (
        result["known_subtotal"] == {"THB": "0.15"}
        and result["total_verified"] is False
    )
    assert sends == [1]
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.generation_cost import summarize_generation_cost; print(summarize_generation_cost(Store(Path(sys.argv[1])),sys.argv[2])['known_subtotal']['THB'])",
            str(service.store.path),
            args[0].task_id,
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert child.stdout.strip() == "0.15"


def test_equal_distinct_events_scoped_accounts_and_missing_event_id(
    context, monkeypatch
):
    service, args, attempt, sends, e = context
    allow_fixture(monkeypatch)
    for item in (
        e,
        dataclasses.replace(e, evidence_id="e2", event_id="event-2"),
        dataclasses.replace(e, evidence_id="unidentified", event_id=None),
    ):
        generation_cost.record_generation_cost(service.store, item)
    result = generation_cost.summarize_generation_cost(
        service.store, args[0].task_id, attempt_id=attempt.attempt_id
    )
    assert result["known_subtotal"] == {"THB": "0.30"}
    assert result["total_verified"] is False and sends == [1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt_id", "other"),
        ("execution_digest", "other"),
        ("candidate_id", "other"),
        ("task_id", "other"),
        ("provider", "other"),
        ("account_scope", "wrong"),
        ("source", "unverified"),
    ],
)
def test_mismatched_evidence_is_rejected(context, monkeypatch, field, value):
    service, _, _, sends, e = context
    allow_fixture(monkeypatch)
    with pytest.raises(ValueError):
        generation_cost.record_generation_cost(
            service.store, dataclasses.replace(e, **{field: value})
        )
    with service.store._connect() as db:
        present = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='generation_cost_evidence'"
        ).fetchone()
        assert not present or db.execute(
            "SELECT count(*) FROM generation_cost_evidence"
        ).fetchone() == (0,)
    assert sends == [1]


def test_explicit_zero_is_known_but_not_reconciled_and_rollback(context, monkeypatch):
    service, args, _, sends, e = context
    allow_fixture(monkeypatch)
    generation_cost.record_generation_cost(
        service.store, dataclasses.replace(e, amount="0")
    )
    total = generation_cost.summarize_generation_cost(service.store, args[0].task_id)
    assert total["known_subtotal"] == {"THB": "0"} and total["total_verified"] is False
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_cost BEFORE INSERT ON generation_cost_evidence BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        generation_cost.record_generation_cost(
            service.store, dataclasses.replace(e, evidence_id="two", event_id="two")
        )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM generation_cost_evidence"
        ).fetchone() == (1,)
    assert sends == [1]


def test_decimal_amounts_do_not_round_during_storage_or_aggregation(
    context, monkeypatch
):
    service, args, _, sends, evidence = context
    allow_fixture(monkeypatch)
    generation_cost.record_generation_cost(
        service.store,
        dataclasses.replace(evidence, amount="999999999999999999.999999999999999999"),
    )
    generation_cost.record_generation_cost(
        service.store,
        dataclasses.replace(
            evidence,
            evidence_id="second",
            event_id="second",
            amount="0.000000000000000001",
        ),
    )
    result = generation_cost.summarize_generation_cost(service.store, args[0].task_id)
    assert result["known_subtotal"] == {"THB": "1000000000000000000.000000000000000000"}
    assert sends == [1]


def test_original_and_linked_attempt_costs_are_separate(
    linked_context,  # noqa: F811
    monkeypatch,
):
    service, args, original, sends = linked_context
    linked = offline_submission.submit_offline(
        service, *args, risk_authorization_id="risk-1"
    )
    with service.store._connect() as db:
        linked_candidate = db.execute(
            "SELECT linked_candidate FROM risk_consumption"
        ).fetchone()[0]
    snap = args[4]
    original_evidence = generation_cost.GenerationCostEvidence(
        "original-cost",
        args[0].task_id,
        1,
        snap.stage,
        "op",
        "source-candidate",
        original.attempt_id,
        snap.execution_digest,
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "account-1",
        "fixture",
        "reference",
        "same-event",
        "0.15",
        "THB",
    )
    linked_evidence = dataclasses.replace(
        original_evidence,
        evidence_id="linked-cost",
        root="linked-op",
        candidate_id=linked_candidate,
        attempt_id=linked.attempt_id,
        account_scope="account-2",
    )
    monkeypatch.setattr(
        generation_cost,
        "_verify_evidence_source",
        lambda _db, e: (
            (e.attempt_id, e.account_scope)
            in {(original.attempt_id, "account-1"), (linked.attempt_id, "account-2")}
            and e.source == "fixture"
        ),
    )
    generation_cost.record_generation_cost(service.store, original_evidence)
    generation_cost.record_generation_cost(service.store, linked_evidence)
    total = generation_cost.summarize_generation_cost(service.store, args[0].task_id)
    assert (
        total["known_subtotal"] == {"THB": "0.30"} and total["total_verified"] is False
    )
    assert generation_cost.summarize_generation_cost(
        service.store, args[0].task_id, root="op"
    )["known_subtotal"] == {"THB": "0.15"}
    assert generation_cost.summarize_generation_cost(
        service.store, args[0].task_id, root="linked-op"
    )["known_subtotal"] == {"THB": "0.15"}
    assert sends == [1, 1]


def test_candidate_a_and_b_bind_actual_attempt_digests(tmp_path, monkeypatch):
    from .test_cross_candidate_submission import setup_cross

    service, target, first, _, _, candidate_b, sends = setup_cross(
        tmp_path, monkeypatch
    )
    fourth = offline_submission.submit_offline(service, *target, retry_of="op")
    with service.store._connect() as db:
        source = db.execute(
            "SELECT candidate_id,snapshot_payload FROM execution_candidate WHERE root_operation_id='op' AND source_candidate_id IS NULL"
        ).fetchone()
    from ecommerce_product_image_creator.domain import ExecutionSnapshot

    a = ExecutionSnapshot.from_payload(source[1])
    first_cost = generation_cost.GenerationCostEvidence(
        "a-cost",
        a.task_id,
        a.revision,
        a.stage,
        "op",
        source[0],
        first.attempt_id,
        a.execution_digest,
        "candidate_gateway",
        a.model,
        a.endpoint,
        a.group,
        "account-1",
        "fixture",
        "reference",
        "a-event",
        "0.15",
        "THB",
    )
    b = candidate_b.snapshot
    b_cost = dataclasses.replace(
        first_cost,
        evidence_id="b-cost",
        candidate_id=candidate_b.candidate_id,
        attempt_id=fourth.attempt_id,
        execution_digest=b.execution_digest,
        model=b.model,
        endpoint=b.endpoint,
        group=b.group,
        event_id="b-event",
    )
    allow_fixture(monkeypatch)
    generation_cost.record_generation_cost(service.store, first_cost)
    generation_cost.record_generation_cost(service.store, b_cost)
    with pytest.raises(ValueError, match="mismatch"):
        generation_cost.record_generation_cost(
            service.store,
            dataclasses.replace(
                b_cost, evidence_id="wrong", candidate_id=source[0], event_id="wrong"
            ),
        )
    assert generation_cost.summarize_generation_cost(
        service.store, a.task_id, root="op"
    )["known_subtotal"] == {"THB": "0.30"}
    assert len(sends) == 1


def test_legacy_attempt_without_chain_is_unknown_and_remains_readable(tmp_path):
    from ecommerce_product_image_creator.storage import Store

    path = tmp_path / "legacy.sqlite"
    store = Store(path)
    store.create_task("task", "input", "request")
    store.create_revision("task", 1, "input")
    store.claim_operation("old-op", "task", 1, "old-digest", "old-attempt")
    store = Store(path)
    store = Store(path)
    summary = generation_cost.summarize_generation_cost(store, "task")
    assert summary["known_subtotal"] == {} and summary["total_verified"] is False
    attempts = summary["attempts"]
    assert isinstance(attempts, dict)
    assert attempts["old-attempt"]["cost_status"] == "unknown"
