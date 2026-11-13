import dataclasses
import socket
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ecommerce_product_image_creator import original_query, query_usage

from .test_original_query import setup_query


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def test_scoped_usage_deduplication_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    from ecommerce_product_image_creator import recovery

    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, identity: (
            identity.attempt_id == attempt.attempt_id
            and identity.upstream_id == "fixture-upstream"
        ),
    )
    monkeypatch.setattr(
        original_query, "_query_account_scope", lambda _: "fixture-account"
    )
    calls = []

    def adapter(identity):
        calls.append(identity.attempt_id)
        return {"data": {"task_id": identity.upstream_id, "status": "IN_PROGRESS"}}

    monkeypatch.setattr(original_query, "_query_adapter", adapter)
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    with service.store._connect() as db:
        counters_before = db.execute("SELECT * FROM attempt_counter").fetchall()
        reviews_before = db.execute("SELECT * FROM form_review").fetchall()
    first = original_query.query_once(service.store, *identity)
    second = original_query.query_once(service.store, *identity)
    assert len(calls) == 2 and len(sends) == 1
    assert first.invocation_id is not None and second.invocation_id is not None
    assert first.invocation_id != second.invocation_id
    initial = query_usage.summarize_query_evidence(
        service.store, "op", "a", attempt.attempt_id
    )
    assert initial["cost_status"] == "unknown"
    assert initial["known_event_amounts"] == {}
    evidence = query_usage.BillingEvidence(
        first.invocation_id,
        "op",
        "a",
        attempt.attempt_id,
        args[4].execution_digest,
        "fixture-provider",
        "fixture-account",
        "event-1",
        "0.25",
        "THB",
        "1",
        "query",
        "fixture-billing",
    )
    with service.store._connect() as db:
        provider = db.execute(
            "SELECT provider FROM query_invocation WHERE invocation_id=?",
            (first.invocation_id,),
        ).fetchone()[0]
    evidence = dataclasses.replace(evidence, provider=provider)
    with pytest.raises(ValueError, match="evidence_source_unverified"):
        query_usage.record_billing_evidence(service.store, evidence)
    monkeypatch.setattr(
        query_usage,
        "_verify_evidence_source",
        lambda _db, e: e.source == "fixture-billing",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: query_usage.record_billing_evidence(service.store, evidence),
                range(2),
            )
        )
    assert results == ["event-1", "event-1"]
    with pytest.raises(ValueError, match="billing_event_conflict"):
        query_usage.record_billing_evidence(
            service.store, dataclasses.replace(evidence, amount="0.50")
        )
    with pytest.raises(ValueError, match="evidence_identity_mismatch"):
        query_usage.record_billing_evidence(
            service.store, dataclasses.replace(evidence, account_scope="other")
        )
    with pytest.raises(ValueError, match="evidence_identity_mismatch"):
        query_usage.record_billing_evidence(
            service.store, dataclasses.replace(evidence, candidate_id="other")
        )
    for field in ("attempt_id", "execution_digest", "provider"):
        with pytest.raises(ValueError, match="evidence_identity_mismatch"):
            query_usage.record_billing_evidence(
                service.store, dataclasses.replace(evidence, **{field: "other"})
            )
    other = dataclasses.replace(
        evidence, invocation_id=second.invocation_id, event_id="event-2"
    )
    query_usage.record_billing_evidence(service.store, other)
    usage = query_usage.UsageEvidence(
        "usage-1",
        first.invocation_id,
        "op",
        "a",
        attempt.attempt_id,
        args[4].execution_digest,
        provider,
        "fixture-account",
        "1",
        "query",
        "fixture-billing",
    )
    query_usage.record_usage_evidence(service.store, usage)
    query_usage.record_usage_evidence(service.store, usage)
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER reject_test_event BEFORE INSERT ON query_billing_event WHEN NEW.event_id='rollback-test' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        query_usage.record_billing_evidence(
            service.store, dataclasses.replace(evidence, event_id="rollback-test")
        )
    summary = query_usage.summarize_query_evidence(
        service.store, "op", "a", attempt.attempt_id
    )
    assert summary["invocation_count"] == 2
    assert summary["known_event_amounts"] == {"THB": "0.50"}
    assert summary["usage_evidence_count"] == 1
    assert (
        summary["cost_status"] == "unknown"
        and summary["billing_completeness"] == "unverified"
    )
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert db.execute("SELECT * FROM attempt_counter").fetchall() == counters_before
        assert db.execute("SELECT * FROM form_review").fetchall() == reviews_before
        assert db.execute("SELECT count(*) FROM query_billing_event").fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM query_billing_event")
    code = "import sqlite3,sys; d=sqlite3.connect(sys.argv[1]); print(d.execute('SELECT count(*) FROM query_invocation').fetchone()[0], d.execute('SELECT count(*) FROM query_billing_event').fetchone()[0])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "2 2"
    query_usage.record_billing_evidence(
        service.store,
        dataclasses.replace(
            evidence, event_id="large", amount="999999999999999999.999999999999999999"
        ),
    )
    query_usage.record_billing_evidence(
        service.store,
        dataclasses.replace(evidence, event_id="small", amount="0.000000000000000001"),
    )
    exact = query_usage.summarize_query_evidence(
        service.store, "op", "a", attempt.attempt_id
    )
    assert exact["known_event_amounts"] == {
        "THB": "1000000000000000000.500000000000000000"
    }
