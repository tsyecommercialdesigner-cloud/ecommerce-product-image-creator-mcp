"""US3 uncertain remote operations: isolated fakes and temporary SQLite."""

import socket
from types import SimpleNamespace

import pytest

from ecommerce_product_image_creator import (
    offline_submission,
    original_query,
    query_usage,
    recovery,
)
from ecommerce_product_image_creator.execution_candidates import register_root_candidate

from .test_linked_atomic_submission import linked_context  # noqa: F401
from .test_offline_submission import setup_context
from .test_original_query import setup_query


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.mark.parametrize(
    "scenario",
    [
        "known_upstream_id",
        "query_again",
        "missing_upstream_id",
        "conflicting_upstream_id",
        "timeout_after_send",
        "intent_without_response",
    ],
)
def test_offline_recovery_original_operation(tmp_path, monkeypatch, scenario):
    no_id = scenario in {
        "missing_upstream_id",
        "timeout_after_send",
        "intent_without_response",
    }
    if no_id:
        service, args = setup_context(tmp_path)
        monkeypatch.setattr(
            offline_submission, "_verify_authorization", lambda *_: True
        )
        monkeypatch.setattr(
            offline_submission, "_verify_business_choices", lambda *_: True
        )
        sends = []

        def timeout(_):
            sends.append(1)
            raise TimeoutError()

        monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
        if scenario == "intent_without_response":

            def crash():
                raise SystemExit("after durable intent")

            monkeypatch.setattr(offline_submission, "_after_intent_recorded", crash)
            with pytest.raises(SystemExit):
                offline_submission.submit_offline(service, *args)
            with service.store._connect() as db:
                attempt = SimpleNamespace(
                    attempt_id=db.execute(
                        "SELECT attempt_id FROM operation WHERE operation_id='op'"
                    ).fetchone()[0]
                )
        else:
            attempt = offline_submission.submit_offline(service, *args)
        register_root_candidate(service.store, "op", "a")
    else:
        service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    digest = args[4].execution_digest
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: (
            i.attempt_id == attempt.attempt_id
            and i.digest == digest
            and i.upstream_id == "fixture-upstream"
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_verify_observation_source",
        lambda i, row: (
            i.attempt_id == attempt.attempt_id
            and row[6] == i.attempt_id
            and row[7] == digest
        ),
    )
    queries = []

    def query(i):
        queries.append(i.upstream_id)
        upstream = (
            "unexpected" if scenario == "conflicting_upstream_id" else i.upstream_id
        )
        return {"data": {"status": "IN_PROGRESS", "task_id": upstream}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        digest,
    )
    with service.store._connect() as db:
        before = (
            db.execute("SELECT count(*) FROM operation_attempt").fetchone(),
            db.execute("SELECT * FROM attempt_counter").fetchall(),
            db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone(),
        )
    assert before[2] not in (None, ("failed",))
    result = recovery.recover_once(service.store, *identity)
    if scenario == "query_again":
        result = recovery.recover_once(service.store, *identity)
    assert result.attempt_id == attempt.attempt_id and result.candidate_id == "a"
    if no_id:
        assert (
            result.state == "unverified"
            and result.risk_of_duplicate_cost
            and result.original_may_still_run
        )
        assert not queries
    elif scenario == "conflicting_upstream_id":
        assert result.state == "conflict" and result.observation_id is not None
        assert queries == ["fixture-upstream"]
    else:
        assert result.state == "running" and result.observation_id is not None
        assert queries == ["fixture-upstream"] * (2 if scenario == "query_again" else 1)
    assert len(sends) == (0 if scenario == "intent_without_response" else 1)
    with service.store._connect() as db:
        after = (
            db.execute("SELECT count(*) FROM operation_attempt").fetchone(),
            db.execute("SELECT * FROM attempt_counter").fetchall(),
            db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone(),
        )
        assert after == before
        rows = db.execute(
            "SELECT attempt_id,candidate_id,execution_digest FROM recovery_observation ORDER BY observation_id"
        ).fetchall()
        assert rows == [(attempt.attempt_id, "a", digest)] * len(queries)


def test_same_upstream_id_other_task_scope_is_unverified(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    service.store.create_task("other", "input", "request")
    service.store.create_revision("other", 1, "input")
    service.store.claim_operation(
        "other-op", "other", 1, "other-digest", "other-attempt"
    )
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_id=? WHERE attempt_id=?",
            ("fixture-upstream", "other-attempt"),
        )
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    queries = []
    monkeypatch.setattr(original_query, "_query_adapter", lambda i: queries.append(i))
    result = recovery.recover_once(
        service.store,
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == "unverified" and result.risk_of_duplicate_cost
    assert queries == [] and len(sends) == 1


@pytest.mark.parametrize("second_query_fails", [False, True])
def test_repeated_query_cost_evidence_preserves_original_history(
    tmp_path, monkeypatch, second_query_fails
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='unknown' WHERE attempt_id=?",
            (attempt.attempt_id,),
        )
    digest = args[4].execution_digest
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, identity: (
            identity.attempt_id == attempt.attempt_id
            and identity.digest == digest
            and identity.upstream_id == "fixture-upstream"
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_verify_observation_source",
        lambda identity, row: row[6] == identity.attempt_id and row[7] == digest,
    )
    calls = []

    def query(identity):
        calls.append(identity.attempt_id)
        if second_query_fails and len(calls) == 2:
            raise TimeoutError()
        return {"data": {"status": "IN_PROGRESS", "task_id": identity.upstream_id}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        digest,
    )
    tables = (
        "attempt",
        "operation",
        "operation_attempt",
        "attempt_counter",
        "local_eligibility",
        "artifact",
        "form_review",
        "mock_review",
    )
    with service.store._connect() as db:
        existing = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        before = {
            name: db.execute(f"SELECT * FROM {name}").fetchall()
            for name in tables
            if name in existing
        }
    first = recovery.recover_once(service.store, *identity)
    second = recovery.recover_once(service.store, *identity)
    assert first.state == "running" and second.state == "running"
    assert second.query_state == ("query_error" if second_query_fails else "running")
    assert first.observation_id != second.observation_id
    assert calls == [attempt.attempt_id, attempt.attempt_id] and len(sends) == 1
    with service.store._connect() as db:
        after = {
            name: db.execute(f"SELECT * FROM {name}").fetchall() for name in before
        }
        assert after == before
        invocations = db.execute(
            "SELECT invocation_id,attempt_id,candidate_id,execution_digest,observation_id FROM query_invocation ORDER BY rowid"
        ).fetchall()
        observations = db.execute(
            "SELECT observation_id,attempt_id,candidate_id,execution_digest,remote_state FROM recovery_observation ORDER BY observation_id"
        ).fetchall()
    assert len(invocations) == len(observations) == 2
    assert len({row[0] for row in invocations}) == 2
    assert [row[1:4] for row in invocations] == [(attempt.attempt_id, "a", digest)] * 2
    assert [row[4] for row in invocations] == [row[0] for row in observations]
    assert [row[4] for row in observations] == [
        "running",
        "query_error" if second_query_fails else "running",
    ]
    costs = query_usage.summarize_query_evidence(
        service.store, "op", "a", attempt.attempt_id
    )
    assert costs["invocation_count"] == 2
    assert (
        costs["cost_status"] == "unknown"
        and costs["billing_completeness"] == "unverified"
    )
    assert costs["known_event_amounts"] == {}


def test_authorized_linked_attempt_preserves_unknown_and_is_single_use(
    linked_context,  # noqa: F811
):
    service, args, original, sends = linked_context
    result = offline_submission.submit_offline(
        service, *args, risk_authorization_id="risk-1"
    )
    assert result.operation_id != "op" and result.attempt_id != original.attempt_id
    assert result.state == "unknown" and sends == [1, 1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (original.attempt_id,),
        ).fetchone() == ("unknown",)
        row = db.execute(
            "SELECT source_root,source_attempt,source_digest,linked_operation,linked_attempt,execution_digest FROM risk_consumption WHERE authorization_id='risk-1'"
        ).fetchone()
        assert row == (
            "op",
            original.attempt_id,
            args[4].execution_digest,
            "linked-op",
            result.attempt_id,
            args[4].execution_digest,
        )
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='linked-op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE operation_id='linked-op'"
        ).fetchone() == (1,)
        payload = db.execute(
            "SELECT evidence_payload FROM risk_reservation WHERE authorization_id='risk-1'"
        ).fetchone()[0]
        assert '"duplicate_cost":true' in payload and '"may_have_sent":true' in payload
    replay = offline_submission.submit_offline(
        service, *args, risk_authorization_id="risk-1"
    )
    assert replay.attempt_id == result.attempt_id and sends == [1, 1]


def test_repeated_query_generation_cost_is_not_counted_twice(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import generation_cost

    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    digest = args[4].execution_digest
    cost = generation_cost.GenerationCostEvidence(
        "cost-evidence",
        args[0].task_id,
        1,
        args[4].stage,
        "op",
        "a",
        attempt.attempt_id,
        digest,
        "candidate_gateway",
        args[4].model,
        args[4].endpoint,
        args[4].group,
        "fake-account",
        "fixture",
        "fixture-ref",
        "fake-event",
        "0.15",
        "THB",
    )
    monkeypatch.setattr(
        generation_cost, "_verify_evidence_source", lambda _db, e: e == cost
    )
    generation_cost.record_generation_cost(service.store, cost)
    before = generation_cost.summarize_generation_cost(
        service.store, args[0].task_id, attempt_id=attempt.attempt_id
    )
    assert before["known_subtotal"] == {"THB": "0.15"}
    with service.store._connect() as db:
        tables = (
            "attempt",
            "operation",
            "operation_attempt",
            "attempt_counter",
            "local_eligibility",
            "artifact",
            "form_review",
            "mock_review",
        )
        available = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        history = {
            t: db.execute(f"SELECT * FROM {t}").fetchall()
            for t in tables
            if t in available
        }
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, identity: (
            identity.attempt_id == attempt.attempt_id and identity.digest == digest
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_verify_observation_source",
        lambda identity, row: row[6] == identity.attempt_id and row[7] == digest,
    )
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda identity: {
            "data": {"status": "SUCCESS", "task_id": identity.upstream_id, "cost": 0.15}
        },
    )
    identity = (
        "op",
        "a",
        args[0].task_id,
        1,
        args[4].stage,
        attempt.attempt_id,
        digest,
    )
    assert recovery.recover_once(service.store, *identity).state == "succeeded"
    assert recovery.recover_once(service.store, *identity).state == "succeeded"
    assert len(sends) == 1
    evidence = query_usage.summarize_query_evidence(
        service.store, "op", "a", attempt.attempt_id
    )
    assert evidence["invocation_count"] == 2
    assert evidence["cost_status"] == "unknown"  # Query charges remain independent.
    assert (
        generation_cost.summarize_generation_cost(
            service.store, args[0].task_id, attempt_id=attempt.attempt_id
        )
        == before
    )
    assert before["total_verified"] is False
    with service.store._connect() as db:
        assert len(db.execute("SELECT * FROM recovery_observation").fetchall()) == 2
        assert {
            t: db.execute(f"SELECT * FROM {t}").fetchall() for t in history
        } == history


def test_succeeded_with_missing_file_recovers_original_artifact(tmp_path, monkeypatch):
    """Exercise the actual isolated query-to-reader chain rather than a draft API."""
    from .test_local_artifact_copy import (
        test_missing_form_artifact_has_verified_mock_copy_without_mutation as verify_chain,
    )

    verify_chain(
        tmp_path, monkeypatch, "valid_jpeg_observed", "recover_from_local_bytes"
    )
