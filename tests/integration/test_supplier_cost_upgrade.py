"""Known pre-raw schema with existing evidence remains auditable after upgrade."""

import multiprocessing

from ecommerce_product_image_creator import generation_cost
from integration.test_original_query import setup_query


def _upgrade_in_new_process(
    service, identity: tuple[str, str, str, int, str, str], output
):
    from ecommerce_product_image_creator import generation_cost
    from ecommerce_product_image_creator.storage import Store
    from ecommerce_product_image_creator.task_view import read_task_view

    service.store = Store(service.store.path)
    generation_cost._verify_supplier_response = lambda *_: True
    generation_cost._verify_evidence_source = lambda *_: True
    old = generation_cost.summarize_generation_cost(service.store, identity[2])[
        "known_subtotal"
    ]
    raw = b'{"data":{"task_id":"fixture-upstream","status":"SUCCESS","cost":"0.1500"}}'
    event = generation_cost.record_supplier_response_cost(
        service.store, *identity, raw, "new-event", "account"
    )
    view = read_task_view(
        service.store, identity[2], service.input_root, service.budget
    )
    Store(service.store.path)
    output.put(
        (
            old,
            event,
            view["generation_cost"]["known_subtotal"],
            view["attempts"][0]["supplier_cost_reports"],
        )
    )


def test_nonempty_known_schema_upgrade_recovers_in_new_process(tmp_path, monkeypatch):
    service, args, attempt, _ = setup_query(tmp_path, monkeypatch)
    snap = args[4]
    evidence = generation_cost.GenerationCostEvidence(
        "old-evidence",
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
        "old-reference",
        "old-event",
        "0.25",
        "THB",
    )
    monkeypatch.setattr(generation_cost, "_verify_evidence_source", lambda *_: True)
    generation_cost.record_generation_cost(service.store, evidence)
    with service.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM supplier_cost_raw").fetchone() == (0,)
        db.execute("DROP TABLE supplier_cost_raw")
        before = db.execute("SELECT * FROM operation_attempt").fetchall()
        consumed = db.execute(
            "SELECT operation_id,consumed FROM local_eligibility"
        ).fetchall()
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        snap.execution_digest,
    )
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    process = ctx.Process(
        target=_upgrade_in_new_process, args=(service, identity, output)
    )
    process.start()
    try:
        old, event, total, reports = output.get(timeout=45)
        process.join(timeout=30)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
    assert old == {"THB": "0.25"} and total == {"THB": "0.40"}
    assert reports[0]["evidence_id"] == event and reports[0]["raw_amount"] == "0.1500"
    with service.store._connect() as db:
        assert db.execute("SELECT * FROM operation_attempt").fetchall() == before
        assert (
            db.execute("SELECT operation_id,consumed FROM local_eligibility").fetchall()
            == consumed
        )
        assert db.execute(
            "SELECT COUNT(*) FROM generation_cost_evidence"
        ).fetchone() == (2,)
        assert db.execute("SELECT COUNT(*) FROM supplier_cost_raw").fetchone() == (1,)
