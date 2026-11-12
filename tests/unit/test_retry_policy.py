"""US3 retry policy contracts; no sleep and no network."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ecommerce_product_image_creator import provider
from ecommerce_product_image_creator.tasks import EligibilityError


@dataclass
class FakeClock:
    now: float = 0

    def advance(self, seconds: float) -> None:
        self.now += seconds


_SCHEDULER_CASES = {
    "third_failure",
    "sixth_failure",
    "switch_outside_authorized_scope",
    "ordinary_safe_failure",
    "429_valid_retry_after",
    "429_missing_retry_after",
    "429_invalid_retry_after",
    "5xx_without_nonexecution_evidence",
    "timeout_without_nonexecution_evidence",
    "unknown",
    "unsupported_endpoint",
    "retry_budget_without_cost_authorization",
    "restart_same_operation",
    "first_attempt",
    "changed_attempt_id",
    "new_human_revision",
}
_CANDIDATE_CASES = {"third_failure", "sixth_failure", "switch_outside_authorized_scope"}


@pytest.mark.parametrize(
    "case,expected",
    [
        pytest.param(
            case,
            expected,
            marks=()
            if case in _SCHEDULER_CASES
            else pytest.mark.xfail(
                strict=True,
                raises=EligibilityError if case in _CANDIDATE_CASES else AssertionError,
                reason="T040/T041: alternate execution candidate persistence is not implemented",
            ),
        )
        for case, expected in [
            ("first_attempt", "send"),
            ("third_failure", "switch_model"),
            ("sixth_failure", "stop"),
            ("restart_same_operation", "preserve_budget"),
            ("changed_attempt_id", "preserve_budget"),
            ("new_human_revision", "new_operation"),
            ("ordinary_safe_failure", "wait_5_seconds"),
            ("429_valid_retry_after", "wait_retry_after"),
            ("429_missing_retry_after", "stop"),
            ("429_invalid_retry_after", "stop"),
            ("5xx_without_nonexecution_evidence", "unknown"),
            ("timeout_without_nonexecution_evidence", "unknown"),
            ("unknown", "stop"),
            ("switch_outside_authorized_scope", "stop"),
            ("unsupported_endpoint", "stop"),
            ("retry_budget_without_cost_authorization", "stop"),
        ]
    ],
)
def test_retry_policy_is_bounded_safe_and_authorized(
    case: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if case in _CANDIDATE_CASES:
        import time
        from dataclasses import replace

        from integration.test_cross_candidate_submission import setup_cross
        from integration.test_retry_scheduler import FakeClock as SchedulerClock

        from ecommerce_product_image_creator import (
            candidate_preflight,
            offline_submission,
            retry_scheduler,
        )

        service, args, _first, _second, third, candidate, calls = setup_cross(
            tmp_path, monkeypatch
        )
        assert service.store.attempt_chain_status("op") == "third_recorded"
        assert not calls
        clock = SchedulerClock()
        monkeypatch.setattr(offline_submission, "_clock_now", clock)
        monkeypatch.setattr(
            candidate_preflight,
            "_verify_terminal_recoverable",
            lambda db, root, attempt, c: (
                root == "op"
                and c.candidate_id == "b"
                and db.execute(
                    "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt,)
                ).fetchone()
                == ("failed",)
            ),
        )
        scheduler = retry_scheduler.OfflineRetryScheduler(clock)
        if case == "third_failure":
            result = scheduler.step(service, *args, root="op", candidate_id="b")
            assert (
                result.action == "submitted"
                and result.result is not None
                and len(calls) == 1
            )
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT execution_digest FROM local_eligibility WHERE operation_id=?",
                    (args[6],),
                ).fetchone() == (candidate.snapshot.execution_digest,)
        elif case == "sixth_failure":
            monkeypatch.setattr(
                offline_submission,
                "_offline_dispatch",
                lambda req: (
                    calls.append(req)
                    or provider.ProviderResult("failed", task_id="fixture-id")
                ),
            )
            monkeypatch.setattr(
                offline_submission,
                "_verify_cross_authorization",
                lambda _db, c, _op, _cid: c.snapshot == candidate.snapshot,
            )
            result = scheduler.step(service, *args, root="op", candidate_id="b")
            assert result.result is not None
            previous = result.result
            for number in (5, 6):
                service.store.record_retry_wait(
                    "op", previous.attempt_id, clock() - 10, None, 500
                )
                confirmation = replace(args[5], confirmation_id=f"candidate-{number}")
                step = (*args[:5], confirmation, f"candidate-op-{number}", args[7])
                service.prepare(*step, expires_at=clock() + 120, candidate_id="b")
                result = scheduler.step(service, *step, root="op", candidate_id="b")
                assert result.action == "submitted" and result.result is not None
                previous = result.result
            confirmation = replace(args[5], confirmation_id="candidate-seven")
            step = (*args[:5], confirmation, "candidate-op-seven", args[7])
            service.prepare(*step, expires_at=clock() + 120, candidate_id="b")
            assert (
                scheduler.step(service, *step, root="op", candidate_id="b").action
                == "stop"
            )
            assert len(calls) == 3
        else:
            monkeypatch.setattr(
                offline_submission, "_verify_cross_authorization", lambda *_: False
            )
            assert (
                scheduler.step(service, *args, root="op", candidate_id="b").action
                == "stop"
            )
            assert calls == []
        return
    if case in _SCHEDULER_CASES:
        from integration.test_attempt002 import prepare_retry
        from integration.test_retry_scheduler import (
            FakeClock as SchedulerClock,
        )
        from integration.test_retry_scheduler import (
            configured_scheduler,
        )

        from ecommerce_product_image_creator import (
            faults,
            offline_submission,
            retry_scheduler,
        )

        if case == "first_attempt":
            from integration.test_offline_submission import setup_context

            service, args = setup_context(tmp_path)
            monkeypatch.setattr(
                offline_submission, "_verify_authorization", lambda *_: True
            )
            monkeypatch.setattr(
                offline_submission, "_verify_business_choices", lambda *_: True
            )
            calls = []
            monkeypatch.setattr(
                offline_submission,
                "_offline_dispatch",
                lambda _: (
                    calls.append(1)
                    or provider.ProviderResult("accepted", task_id="fixture")
                ),
            )
            result = offline_submission.submit_offline(service, *args)
            assert result.state == "accepted" and len(calls) == 1
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT count FROM attempt_counter WHERE operation_id='op'"
                ).fetchone() == (1,)
            return
        if case == "changed_attempt_id":
            from integration.test_attempt003 import prepare_third

            service, args, first, second, calls = prepare_third(tmp_path, monkeypatch)
            third = offline_submission.submit_offline(service, *args, retry_of="op")
            assert len({first.attempt_id, second.attempt_id, third.attempt_id}) == 3
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT count FROM attempt_counter WHERE operation_id='op'"
                ).fetchone() == (3,)
            assert len(calls) == 3
            return
        if case == "new_human_revision":
            from integration.test_form_revision import (
                test_revision_chain_links_fake_attempt_and_keeps_old_counter,
            )

            from ecommerce_product_image_creator.storage import Store

            test_revision_chain_links_fake_attempt_and_keeps_old_counter(
                tmp_path, monkeypatch
            )
            with Store(tmp_path / "state.sqlite")._connect() as db:
                assert db.execute(
                    "SELECT count FROM attempt_counter WHERE operation_id='old-op' AND model='model' AND endpoint='endpoint'"
                ).fetchone() == (2,)
                assert db.execute(
                    "SELECT SUM(count) FROM attempt_counter WHERE operation_id='new-op'"
                ).fetchone() == (1,)
            return

        if case in {"unknown", "timeout_without_nonexecution_evidence"}:
            service, args, first, calls = prepare_retry(
                tmp_path, monkeypatch, "unknown"
            )
            assert (
                faults.classify_transport("read_timeout", evidence="read_timeout").state
                == "unknown"
            )
            assert (
                retry_scheduler.OfflineRetryScheduler(SchedulerClock())
                .step(service, *args, root="op")
                .action
                == "stop"
            )
            assert first.state == "unknown" and len(calls) == 1
            return
        if case == "restart_same_operation":
            import time
            from dataclasses import replace

            from integration.test_attempt003 import prepare_third

            from ecommerce_product_image_creator.storage import Store

            service, args, _first, _second, calls = prepare_third(tmp_path, monkeypatch)
            offline_submission.submit_offline(service, *args, retry_of="op")
            with Store(service.store.path)._connect() as db:
                assert db.execute(
                    "SELECT count FROM attempt_counter WHERE operation_id='op'"
                ).fetchone() == (3,)
            confirmation = replace(args[5], confirmation_id="restart-fourth")
            fourth = (*args[:5], confirmation, "restart-fourth-op", args[7])
            service.prepare(*fourth, expires_at=time.time() + 120)
            assert (
                retry_scheduler.OfflineRetryScheduler(SchedulerClock())
                .step(service, *fourth, root="op")
                .action
                == "stop"
            )
            assert len(calls) == 3
            return
        status = 429 if case.startswith("429_") else 500
        header = (
            "7"
            if case == "429_valid_retry_after"
            else "invalid"
            if case == "429_invalid_retry_after"
            else None
        )
        scheduler, clock, service, args, _first, calls = configured_scheduler(
            tmp_path, monkeypatch, status=status, header=header
        )
        if case == "5xx_without_nonexecution_evidence":
            assert faults.classify_submission(500, {}).state == "unknown"
            monkeypatch.setattr(
                offline_submission, "_verify_retry_evidence", lambda *_: False
            )
        if case == "retry_budget_without_cost_authorization":
            monkeypatch.setattr(
                offline_submission, "_verify_retry_authorization", lambda *_: False
            )
        if case == "unsupported_endpoint":
            with service.store._connect() as db:
                db.execute(
                    "UPDATE operation_attempt SET endpoint='unsupported' WHERE operation_id='op'"
                )
        original = clock()
        result = scheduler.step(service, *args, root="op")
        if case in {"ordinary_safe_failure", "429_valid_retry_after"}:
            assert result.action == "waiting"
            with service.store._connect() as db:
                started, deadline = db.execute(
                    "SELECT started,deadline FROM retry_wait WHERE operation_id='op'"
                ).fetchone()
            seconds = 7 if case == "429_valid_retry_after" else 5
            assert deadline - started == seconds and clock() == original
            clock.advance(seconds)
            assert scheduler.step(service, *args, root="op").action == "submitted"
            assert len(calls) == 2
        else:
            assert result.action == "stop" and len(calls) == 1 and clock() == original
        return
    clock = FakeClock()
    retry_after = {
        "429_valid_retry_after": "7",
        "429_invalid_retry_after": "not-a-delay",
    }.get(case)
    inputs: dict[str, Any] = {
        "operation_id": "op",
        "attempt_id": "a1",
        "attempts": {"sunburst": 1, "flare": 0},
        "model": "gpt-image-2.5-sunburst",
        "endpoint": "edits",
        "status": 500,
        "remote_nonexecution_proven": True,
        "authorized": True,
    }
    changes = {
        "first_attempt": {"attempts": {"sunburst": 0, "flare": 0}, "status": None},
        "third_failure": {"attempts": {"sunburst": 3, "flare": 0}},
        "sixth_failure": {
            "attempts": {"sunburst": 3, "flare": 3},
            "model": "gpt-image-2.5-flare",
        },
        "restart_same_operation": {
            "attempts": {"sunburst": 3, "flare": 0},
            "restarted": True,
        },
        "changed_attempt_id": {
            "attempts": {"sunburst": 3, "flare": 0},
            "attempt_id": "a2",
        },
        "new_human_revision": {
            "operation_id": "new-op",
            "previous_operation_id": "op",
            "human_revision": True,
        },
        "429_valid_retry_after": {"status": 429},
        "429_missing_retry_after": {"status": 429},
        "429_invalid_retry_after": {"status": 429},
        "5xx_without_nonexecution_evidence": {"remote_nonexecution_proven": False},
        "timeout_without_nonexecution_evidence": {
            "status": "timeout",
            "remote_nonexecution_proven": False,
        },
        "unknown": {"remote_state": "unknown", "remote_nonexecution_proven": False},
        "switch_outside_authorized_scope": {
            "attempts": {"sunburst": 3, "flare": 0},
            "authorized_models": ["gpt-image-2.5-sunburst"],
        },
        "unsupported_endpoint": {"endpoint": "unsupported"},
        "retry_budget_without_cost_authorization": {"authorized": False},
    }
    inputs.update(changes.get(case, {}))
    decide = getattr(provider, "decide_safe_retry", None)
    assert callable(decide), "T040/T041 retry policy and scheduling service is missing"
    result: Any = decide(
        **inputs,
        retry_after=retry_after,
        clock=clock,
        models=("gpt-image-2.5-sunburst", "gpt-image-2.5-flare"),
        max_attempts=3,
    )
    assert result.action == expected
    assert result.total_attempts <= 6
    assert result.attempts_per_combination <= 3
    if expected == "wait_5_seconds":
        assert result.delay_seconds == 5
    if expected == "wait_retry_after":
        assert result.delay_seconds == 7
    assert clock.now == 0


# T040-A: pure offline classification; the T040/T041 orchestration xfails above remain.
def test_failure_classifier_distinguishes_send_boundary_and_unknown():
    from ecommerce_product_image_creator.faults import classify_transport

    before = classify_transport("not_sent", evidence="local_guard")
    uncertain = classify_transport("read_timeout", evidence="read_timeout")
    assert (before.state, before.remote_effect, before.evidence) == (
        "not_sent",
        "not_executed",
        "local_guard",
    )
    assert (uncertain.state, uncertain.remote_effect, uncertain.next_action) == (
        "unknown",
        "unknown",
        "stop",
    )
    assert uncertain.can_send is False


@pytest.mark.parametrize(
    "event", ["connect_interrupted", "connect_timeout", "read_timeout"]
)
def test_transport_failure_never_assumes_nonexecution(event: str):
    from ecommerce_product_image_creator.faults import classify_transport

    result = classify_transport(event, evidence="fixture")
    assert result.state == "unknown" and result.remote_effect == "unknown"
    assert result.next_action == "stop" and not result.can_send


@pytest.mark.parametrize("status", [429, 500, 503, 599])
def test_http_error_does_not_prove_safe_retry(status: int):
    from ecommerce_product_image_creator.faults import classify_submission

    result = classify_submission(status, {}, retry_after="7")
    assert result.state == "unknown" and result.remote_effect == "unknown"
    assert result.http_status == status and result.next_action == "stop"
    assert result.can_send is False


def test_async_response_classification_and_conflicting_ids():
    from ecommerce_product_image_creator.faults import (
        classify_query,
        classify_submission,
    )

    accepted = classify_submission(200, {"task_id": "abc"})
    assert accepted.state == "accepted" and accepted.task_id == "abc"
    assert accepted.next_action == "query_original"
    for wire, expected in [
        ("NOT_START", "accepted"),
        ("IN_PROGRESS", "running"),
        ("FAILURE", "failed"),
        ("SUCCESS", "succeeded"),
    ]:
        result = classify_query(
            200, {"data": {"task_id": "abc", "status": wire}}, "abc"
        )
        assert result.state == expected and result.task_id == "abc"
    for payload in (
        None,
        {"task_id": "a", "data": "b"},
        {"data": {}},
        {"data": {"task_id": "abc", "status": "NEW"}},
    ):
        result = (
            classify_submission(200, payload)
            if payload != {"data": {"task_id": "abc", "status": "NEW"}}
            else classify_query(200, payload, "abc")
        )
        assert result.state == "uninterpretable" and result.remote_effect == "unknown"
        assert result.can_send is False


def test_known_task_id_prioritizes_original_query():
    from ecommerce_product_image_creator.faults import classify_transport

    result = classify_transport("read_timeout", evidence="fixture", task_id="abc")
    assert result.state == "unknown" and result.next_action == "query_original"
    assert result.task_id == "abc" and not result.can_send
    assert (
        classify_transport(
            "read_timeout", evidence="fixture", task_id="bad id"
        ).next_action
        == "stop"
    )


def test_retry_after_delta_and_http_date_with_fake_clock():
    from datetime import UTC, datetime

    from ecommerce_product_image_creator.faults import parse_retry_after

    now = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("7", now) == 7
    assert parse_retry_after("Sun, 08 Nov 2026 12:00:09 GMT", now) == 9
    for raw in (
        None,
        "",
        "-1",
        "1.5",
        "NaN",
        "invalid",
        "Sun, 08 Nov 2026 11:59:59 GMT",
    ):
        assert parse_retry_after(raw, now) is None


def test_candidate_policy_requires_all_independent_evidence():
    from ecommerce_product_image_creator.faults import (
        RetryFacts,
        classify_transport,
        evaluate_retry_candidate,
    )

    known = classify_transport("not_sent", evidence="local_guard")
    facts = RetryFacts(
        operation_id="op",
        model="gpt-image-2.5-sunburst",
        endpoint=provider.ENDPOINT,
        attempts_for_combination=1,
        attempts_total=1,
        safe_failure_proven=True,
        fee_scope_verified=True,
        combination_verified=True,
        business_clear=True,
        recoverability="transient_local",
    )
    assert evaluate_retry_candidate(known, facts).outcome == "candidate_only"
    from dataclasses import replace

    for field in (
        "safe_failure_proven",
        "fee_scope_verified",
        "combination_verified",
        "business_clear",
    ):
        assert (
            evaluate_retry_candidate(known, replace(facts, **{field: False})).outcome
            == "stop"
        )
    assert (
        evaluate_retry_candidate(
            known, replace(facts, attempts_for_combination=3)
        ).outcome
        == "stop"
    )
    assert (
        evaluate_retry_candidate(known, replace(facts, attempts_total=6)).outcome
        == "stop"
    )
    assert (
        evaluate_retry_candidate(known, replace(facts, endpoint="unsupported")).outcome
        == "stop"
    )
    unknown = classify_transport("read_timeout", evidence="fixture")
    assert evaluate_retry_candidate(unknown, facts).outcome == "stop"


def test_retry_after_missing_or_invalid_never_becomes_a_send_permission():
    from datetime import UTC, datetime

    from ecommerce_product_image_creator.faults import classify_submission

    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    for header, delay in [("7", 7), ("invalid", None), (None, None)]:
        result = classify_submission(429, {}, retry_after=header, now=now)
        assert result.retry_after_seconds == delay
        assert result.state == "unknown"
        assert not result.can_send and result.next_action == "stop"


def test_unsubstantiated_not_sent_claim_is_unknown():
    from ecommerce_product_image_creator.faults import classify_transport

    result = classify_transport("not_sent", evidence="agent_says_so")
    assert result.state == "unknown" and not result.can_send


def test_classification_never_connects_or_sends(monkeypatch):
    import socket

    from ecommerce_product_image_creator.faults import (
        RetryFacts,
        classify_submission,
        evaluate_retry_candidate,
    )

    def forbid_connect(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbid_connect)
    result = classify_submission(503, {"error": "unavailable"})
    decision = evaluate_retry_candidate(
        result,
        RetryFacts(
            operation_id="op",
            model="gpt-image-2.5-sunburst",
            endpoint=provider.ENDPOINT,
            attempts_for_combination=1,
            attempts_total=1,
            safe_failure_proven=True,
            fee_scope_verified=True,
            combination_verified=True,
            business_clear=True,
        ),
    )
    assert decision.outcome == "stop" and not decision.can_send
