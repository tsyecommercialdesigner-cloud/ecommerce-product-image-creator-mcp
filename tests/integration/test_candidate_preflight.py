"""Read-only cross-combination preflight using real isolated attempt chains."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ecommerce_product_image_creator import (
    candidate_preflight,
    faults,
    offline_submission,
)
from ecommerce_product_image_creator.domain import ExecutionSnapshot
from ecommerce_product_image_creator.execution_candidates import (
    create_candidate,
    register_root_candidate,
)

from .test_attempt003 import prepare_third
from .test_execution_candidates import setup_candidates


def candidates(service, args):
    first = register_root_candidate(service.store, "op", "a")
    s = args[4]
    alternative = ExecutionSnapshot.create(
        args[1],
        args[3],
        s.stage,
        "gpt-image-2.5-flare",
        s.endpoint,
        s.group,
        references=s.references,
        operation_scope=s.operation_scope,
    )
    return first, create_candidate(service.store, "op", "b", "a", alternative)


def test_budget_exhaustion_and_readonly_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _first, _second, calls = prepare_third(tmp_path, monkeypatch)
    third = offline_submission.submit_offline(service, *args, retry_of="op")
    assert third.state == "failed" and len(calls) == 3
    _, alternate = candidates(service, args)
    budget = candidate_preflight.budget_view(service.store, "op", "b")
    assert (
        budget.combination_used,
        budget.combination_max,
        budget.total_used,
        budget.total_max,
    ) == (0, 3, 3, 6)
    assert budget.complete and not budget.conflict and budget.allows_evaluation
    default = candidate_preflight.preflight(service, "op", "b")
    assert not default.eligible and not default.can_send
    assert (
        "capability_unverified" in default.reasons
        and "authorization_unverified" in default.reasons
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, root, attempt, candidate: (
            root == "op"
            and candidate.candidate_id == "b"
            and db.execute(
                "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
                (attempt,),
            ).fetchone()
            == ("failed", "fixture-id")
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_business",
        lambda _db, candidate: (
            candidate.snapshot.execution_digest == alternate.snapshot.execution_digest
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_capability",
        lambda _db, candidate: (
            (
                candidate.snapshot.model,
                candidate.snapshot.endpoint,
                candidate.snapshot.group,
            )
            == (
                alternate.snapshot.model,
                alternate.snapshot.endpoint,
                alternate.snapshot.group,
            )
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_authorization",
        lambda _db, candidate: candidate.snapshot.execution_digest == "wrong-digest",
    )
    assert (
        "authorization_unverified"
        in candidate_preflight.preflight(service, "op", "b").reasons
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_authorization",
        lambda _db, candidate: (
            candidate.snapshot.execution_digest == alternate.snapshot.execution_digest
        ),
    )
    result = candidate_preflight.preflight(service, "op", "b")
    assert result.eligible and not result.can_send and not result.reasons
    assert result.budget is not None
    assert result.budget.candidate_digest == alternate.snapshot.execution_digest
    with service.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM operation_attempt").fetchone() == (3,)
        assert db.execute(
            "SELECT count(*) FROM local_eligibility WHERE execution_digest=?",
            (alternate.snapshot.execution_digest,),
        ).fetchone() == (0,)
    assert len(calls) == 3
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda _: candidate_preflight.budget_view(service.store, "op", "b"),
                range(3),
            )
        )
    assert all(x == budget for x in results)
    code = "import sys; from pathlib import Path; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.candidate_preflight import budget_view; b=budget_view(Store(Path(sys.argv[1])), 'op', 'b'); print(b.combination_used,b.total_used,b.complete)"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "0 3 True"


def test_switch_rejected_before_first_combination_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, calls = setup_candidates(tmp_path, monkeypatch)
    candidates(service, args)
    view = candidate_preflight.budget_view(service.store, "op", "b")
    assert (view.combination_used, view.total_used) == (0, 1)
    result = candidate_preflight.preflight(service, "op", "b")
    assert "source_budget_not_exhausted" in result.reasons and not result.can_send
    assert len(calls) == 1


@pytest.mark.parametrize("state", ["unknown", "running", "accepted"])
def test_unknown_and_processing_never_qualify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
):
    service, args, _first, _second, calls = prepare_third(tmp_path, monkeypatch)
    third = offline_submission.submit_offline(service, *args, retry_of="op")
    candidates(service, args)
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status=? WHERE attempt_id=?",
            (state, third.attempt_id),
        )
    monkeypatch.setattr(
        candidate_preflight, "_verify_terminal_recoverable", lambda *_: True
    )
    assert (
        "source_unresolved" in candidate_preflight.preflight(service, "op", "b").reasons
    )
    assert len(calls) == 3


def test_counter_or_chain_conflict_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, _ = setup_candidates(tmp_path, monkeypatch)
    candidates(service, args)
    with service.store._connect() as db:
        db.execute("DELETE FROM attempt_counter WHERE operation_id='op'")
    budget = candidate_preflight.budget_view(service.store, "op", "b")
    assert not budget.complete and budget.conflict and not budget.allows_evaluation
    assert (
        "budget_history_incomplete"
        in candidate_preflight.preflight(service, "op", "b").reasons
    )


def test_deterministic_presend_failure_cannot_be_reinterpreted_as_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _first, _second, calls = prepare_third(tmp_path, monkeypatch)

    def fail(_request):
        raise faults.DeterministicInputError("fixture")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", fail)
    third = offline_submission.submit_offline(service, *args, retry_of="op")
    assert third.state == "unknown" and len(calls) == 2
    candidates(service, args)
    monkeypatch.setattr(
        candidate_preflight, "_verify_terminal_recoverable", lambda *_: True
    )
    assert (
        "source_fault_not_recoverable"
        in candidate_preflight.preflight(service, "op", "b").reasons
    )
    assert len(calls) == 2


def test_conflicting_presend_and_remote_status_never_qualify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _first, _second, _calls = prepare_third(tmp_path, monkeypatch)
    third = offline_submission.submit_offline(service, *args, retry_of="op")
    candidates(service, args)
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt_transport_evidence SET phase='pre_send_failure',reason='local_validation' WHERE attempt_id=?",
            (third.attempt_id,),
        )
    monkeypatch.setattr(
        candidate_preflight, "_verify_terminal_recoverable", lambda *_: True
    )
    assert (
        "source_evidence_conflict"
        in candidate_preflight.preflight(service, "op", "b").reasons
    )


def test_group_change_does_not_reset_combination_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, _ = setup_candidates(tmp_path, monkeypatch)
    register_root_candidate(service.store, "op", "a")
    original = args[4]
    changed = ExecutionSnapshot.create(
        args[1],
        args[3],
        original.stage,
        original.model,
        original.endpoint,
        "another-group",
        references=original.references,
        operation_scope=original.operation_scope,
    )
    create_candidate(service.store, "op", "other-group", "a", changed)
    view = candidate_preflight.budget_view(service.store, "op", "other-group")
    assert (view.combination_used, view.total_used) == (1, 1)
