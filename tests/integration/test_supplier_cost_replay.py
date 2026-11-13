"""A single attested event can appear in distinct query responses."""

import pytest

from ecommerce_product_image_creator import generation_cost, offline_submission
from integration.test_cross_candidate_submission import setup_cross
from integration.test_original_query import setup_query


def test_same_event_changed_response_remains_idempotent(tmp_path, monkeypatch):
    service, args, attempt, _ = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(generation_cost, "_verify_supplier_response", lambda *_: True)
    monkeypatch.setattr(generation_cost, "_verify_evidence_source", lambda *_: True)
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    first = (
        b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":"0.1500"}}'
    )
    later = b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":"0.15","extra":"later"}}'
    one = generation_cost.record_supplier_response_cost(
        service.store, *identity, first, "same-event", "account"
    )
    two = generation_cost.record_supplier_response_cost(
        service.store, *identity, later, "same-event", "account"
    )
    assert one == two
    assert generation_cost.summarize_generation_cost(service.store, args[0].task_id)[
        "known_subtotal"
    ] == {"THB": "0.15"}
    assert (
        generation_cost.read_supplier_response_costs(
            service.store, "op", attempt.attempt_id
        )[0]["raw_amount"]
        == "0.1500"
    )


def test_flare_raw_cost_stays_unverified_without_unit_contract(tmp_path, monkeypatch):
    service, target, _, _, _, flare, sends = setup_cross(tmp_path, monkeypatch)
    attempt = offline_submission.submit_offline(service, *target, retry_of="op")
    snap = flare.snapshot
    raw = b'{"data":{"task_id":"flare-fixture","status":"SUCCESS","cost":0.15}}'
    monkeypatch.setattr(generation_cost, "_verify_supplier_response", lambda *_: True)
    monkeypatch.setattr(generation_cost, "_verify_evidence_source", lambda *_: True)
    with pytest.raises(ValueError, match="supplier_contract_unverified"):
        generation_cost.record_supplier_response_cost(
            service.store,
            "op",
            flare.candidate_id,
            snap.task_id,
            snap.revision,
            attempt.attempt_id,
            snap.execution_digest,
            raw,
            "flare-event",
            "account",
        )
    assert (
        generation_cost.read_supplier_response_costs(
            service.store, "op", attempt.attempt_id
        )
        == []
    )
    assert len(sends) == 1
