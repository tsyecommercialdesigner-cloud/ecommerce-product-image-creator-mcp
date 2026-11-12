"""Exercise same generation cost event through independent query invocations."""

from ecommerce_product_image_creator import generation_cost, original_query, recovery
from ecommerce_product_image_creator.task_view import read_task_view
from integration.test_original_query import setup_query


def test_distinct_query_responses_deduplicate_attested_generation_event(
    tmp_path, monkeypatch
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    responses = [
        b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":"0.1500"}}',
        b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":"0.15","progress":"100%"}}',
    ]
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda db, identity: identity.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(original_query, "_query_adapter", lambda _: responses.pop(0))
    monkeypatch.setattr(
        original_query,
        "_supplier_cost_event",
        lambda identity, raw: ("same-event", "fixture-account"),
    )
    monkeypatch.setattr(
        generation_cost,
        "_verify_supplier_response",
        lambda db, evidence, raw: (
            evidence.event_id == "same-event"
            and evidence.attempt_id == attempt.attempt_id
        ),
    )
    monkeypatch.setattr(
        generation_cost,
        "_verify_evidence_source",
        lambda db, evidence: evidence.account_scope == "fixture-account",
    )
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    first = original_query.query_once(service.store, *identity)
    second = original_query.query_once(service.store, *identity)
    assert first.state == second.state == "succeeded"
    assert first.observation_id != second.observation_id
    with service.store._connect() as db:
        rows = db.execute(
            "SELECT response_digest FROM recovery_observation ORDER BY observation_id"
        ).fetchall()
        assert len(rows) == 2 and rows[0] != rows[1]
        assert db.execute(
            "SELECT COUNT(*) FROM generation_cost_evidence"
        ).fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM supplier_cost_raw").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM query_invocation").fetchone() == (2,)
    for _ in range(2):
        view = read_task_view(service.store, args[0].task_id, tmp_path, service.budget)
        report = view["attempts"][0]["supplier_cost_reports"][0]
        assert report["raw_amount"] == "0.1500" and report["unit"] == "฿"
        assert view["generation_cost"]["known_subtotal"] == {"THB": "0.15"}
        assert view["attempts"][0]["query_cost"]["cost_status"] == "unknown"
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM generation_cost_evidence"
        ).fetchone() == (1,)
    assert sends == [1]
