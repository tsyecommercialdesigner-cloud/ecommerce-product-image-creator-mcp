"""Raw cost must be captured before JSON numbers lose precision."""

import asyncio
import sqlite3
import sys

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ecommerce_product_image_creator import generation_cost, original_query, recovery
from integration.test_original_query import setup_query


def test_raw_response_cost_needs_verified_source_and_retains_exact_number(
    tmp_path, monkeypatch
):
    service, args, attempt, _ = setup_query(tmp_path, monkeypatch)
    payload = b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":0.123456789012345678}}'
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    with pytest.raises(ValueError, match="source_unverified"):
        generation_cost.record_supplier_response_cost(
            service.store, *identity, payload, "fixture-event", "fixture-account"
        )
    monkeypatch.setattr(
        generation_cost, "_verify_supplier_response", lambda db, evidence, raw: True
    )
    monkeypatch.setattr(
        generation_cost,
        "_verify_evidence_source",
        lambda db, evidence: evidence.source == "supplier_response",
    )
    generation_cost.record_supplier_response_cost(
        service.store, *identity, payload, "fixture-event", "fixture-account"
    )
    cost = generation_cost.read_supplier_response_costs(
        service.store, "op", attempt.attempt_id
    )
    assert cost[0]["raw_amount"] == "0.123456789012345678"
    assert cost[0]["currency"] == "THB"
    assert generation_cost.summarize_generation_cost(service.store, args[0].task_id)[
        "known_subtotal"
    ] == {"THB": "0.123456789012345678"}


def test_raw_query_to_mcp_view_is_exact_and_idempotent(tmp_path, monkeypatch):
    root, state = tmp_path / "input", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    service, args, attempt, sends = setup_query(root, monkeypatch)
    raw = b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":0.123456789012345678}}'
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda db, identity: identity.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(original_query, "_query_adapter", lambda _: raw)
    monkeypatch.setattr(
        original_query,
        "_supplier_cost_event",
        lambda identity, payload: (
            ("event-1", "fixture-account") if payload == raw else None
        ),
    )
    monkeypatch.setattr(
        generation_cost,
        "_verify_supplier_response",
        lambda db, evidence, payload: evidence.event_id == "event-1" and payload == raw,
    )
    monkeypatch.setattr(
        generation_cost,
        "_verify_evidence_source",
        lambda db, evidence: (
            evidence.source == "supplier_response"
            and evidence.account_scope == "fixture-account"
        ),
    )
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert original_query.query_once(service.store, *identity).state == "succeeded"
    assert original_query.query_once(service.store, *identity).state == "succeeded"
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM generation_cost_evidence"
        ).fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM supplier_cost_raw").fetchone() == (1,)
        with sqlite3.connect(state / "tasks.sqlite") as target:
            db.backup(target)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ecommerce_product_image_creator.server"],
        env={
            "PRODUCT_IMAGE_INPUT_ROOT": str(root),
            "PRODUCT_IMAGE_STATE_DIR": str(state),
            "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
            "PRODUCT_IMAGE_MAX_PIXELS": "10000",
            "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
        },
    )

    async def call():
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            for _ in range(2):
                result = await client.call_tool(
                    "get_product_image_task", {"task_id": args[0].task_id}
                )
                assert not result.is_error
                view = result.structured_content
                assert view is not None
                item = view["attempts"][0]
                assert (
                    item["supplier_cost_reports"][0]["raw_amount"]
                    == "0.123456789012345678"
                )
                assert item["supplier_cost_reports"][0]["currency"] == "THB"
                assert item["supplier_cost_reports"][0]["unit"] == "฿"
                assert item["generation_cost"]["total_verified"] is False
                assert item["query_cost"]["cost_status"] == "unknown"
                assert view["host_token_usage"] == "not_measured"

    asyncio.run(call())
    with sqlite3.connect(state / "tasks.sqlite") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM generation_cost_evidence"
        ).fetchone() == (1,)
    assert sends == [1]


@pytest.mark.parametrize(
    "cost", [b"-1", b"NaN", b"1e999", b"null", b'"-1"', b'"invalid"', b"true"]
)
def test_invalid_supplier_cost_never_creates_evidence(tmp_path, monkeypatch, cost):
    service, args, attempt, _ = setup_query(tmp_path, monkeypatch)
    raw = (
        b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":'
        + cost
        + b"}}"
    )
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    monkeypatch.setattr(generation_cost, "_verify_supplier_response", lambda *_: True)
    monkeypatch.setattr(generation_cost, "_verify_evidence_source", lambda *_: True)
    with pytest.raises(ValueError):
        generation_cost.record_supplier_response_cost(
            service.store, *identity, raw, "event", "account"
        )
    assert (
        generation_cost.summarize_generation_cost(service.store, args[0].task_id)[
            "known_subtotal"
        ]
        == {}
    )


def test_wrong_identity_conflicting_currency_and_event_replay(tmp_path, monkeypatch):
    service, args, attempt, _ = setup_query(tmp_path, monkeypatch)
    raw = b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":"0.1500"}}'
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    monkeypatch.setattr(generation_cost, "_verify_supplier_response", lambda *_: True)
    monkeypatch.setattr(generation_cost, "_verify_evidence_source", lambda *_: True)
    for other in [
        ("op", "a", "wrong", 1, attempt.attempt_id, args[4].execution_digest),
        ("op", "a", args[0].task_id, 2, attempt.attempt_id, args[4].execution_digest),
        ("op", "a", args[0].task_id, 1, "wrong", args[4].execution_digest),
    ]:
        with pytest.raises(ValueError):
            generation_cost.record_supplier_response_cost(
                service.store, *other, raw, "event", "account"
            )
    with pytest.raises(ValueError):
        generation_cost.record_supplier_response_cost(
            service.store,
            *identity,
            raw.replace(b'"cost"', b'"currency":"USD","cost"'),
            "event",
            "account",
        )
    with pytest.raises(ValueError):
        generation_cost.record_supplier_response_cost(
            service.store,
            *identity,
            raw.replace(b'{"data"', b'{"code":"error","data"'),
            "event",
            "account",
        )
    first = generation_cost.record_supplier_response_cost(
        service.store, *identity, raw, "event", "account"
    )
    assert (
        generation_cost.record_supplier_response_cost(
            service.store, *identity, raw, "event", "account"
        )
        == first
    )
    with pytest.raises(ValueError):
        generation_cost.record_supplier_response_cost(
            service.store,
            *identity,
            raw.replace(b"0.1500", b"0.1600"),
            "event",
            "account",
        )
    generation_cost.record_supplier_response_cost(
        service.store, *identity, raw, "other-event", "account"
    )
    reports = generation_cost.read_supplier_response_costs(
        service.store, "op", attempt.attempt_id
    )
    assert len(reports) == 2 and all(item["raw_amount"] == "0.1500" for item in reports)
    assert generation_cost.summarize_generation_cost(service.store, args[0].task_id)[
        "known_subtotal"
    ] == {"THB": "0.30"}


def test_raw_and_cost_event_are_atomic_on_storage_failure(tmp_path, monkeypatch):
    service, args, attempt, _ = setup_query(tmp_path, monkeypatch)
    raw = b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":1.25}}'
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    monkeypatch.setattr(generation_cost, "_verify_supplier_response", lambda *_: True)
    monkeypatch.setattr(generation_cost, "_verify_evidence_source", lambda *_: True)
    with service.store._connect() as db:
        generation_cost._schema(db)
        db.execute(
            "CREATE TRIGGER fail_raw BEFORE INSERT ON supplier_cost_raw BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        generation_cost.record_supplier_response_cost(
            service.store, *identity, raw, "event", "account"
        )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM generation_cost_evidence"
        ).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM supplier_cost_raw").fetchone() == (0,)


def test_raw_query_with_unverified_cost_source_stays_unknown(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    raw = b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":0.5}}'
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(original_query, "_query_adapter", lambda _: raw)
    monkeypatch.setattr(
        original_query, "_supplier_cost_event", lambda *_: ("event", "account")
    )
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert original_query.query_once(service.store, *identity).state == "succeeded"
    assert (
        generation_cost.summarize_generation_cost(service.store, args[0].task_id)[
            "known_subtotal"
        ]
        == {}
    )
    assert (
        generation_cost.read_supplier_response_costs(
            service.store, "op", attempt.attempt_id
        )
        == []
    )
    assert sends == [1]
