"""Pre-send evidence across actual candidate digests with fake dispatch only."""

import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator import (
    candidate_preflight,
    faults,
    offline_submission,
)
from ecommerce_product_image_creator.send_evidence import (
    safe_failure_kind,
    verified_recoverability,
)

from .test_attempt003 import prepare_third
from .test_candidate_preflight import candidates


def test_recoverable_presend_evidence_across_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, args, _, _, calls = prepare_third(tmp_path, monkeypatch)

    def transient(_request):
        raise faults.TransientLocalError("fixture")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", transient)
    third = offline_submission.submit_offline(service, *args, retry_of="op")
    assert third.state == "unknown" and len(calls) == 2
    _, b = candidates(service, args)
    service.store.record_retry_wait("op", third.attempt_id, time.time() - 10, None, 0)
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, root, attempt, candidate: (
            db.execute(
                "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
                (attempt,),
            ).fetchone()
            == ("failed", "fixture-id")
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_authorization",
        lambda _db, c: c.snapshot == b.snapshot,
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_capability",
        lambda _db, c: c.snapshot == b.snapshot,
    )
    monkeypatch.setattr(
        candidate_preflight, "_verify_business", lambda _db, c: c.snapshot == b.snapshot
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, op, cid: (
            c.snapshot == b.snapshot
            and op.startswith("flare-")
            and cid.startswith("confirm-")
        ),
    )
    for seq in (4, 5, 6):
        confirmation = replace(
            args[5],
            confirmation_id=f"confirm-{seq}",
            execution_digest=b.snapshot.execution_digest,
        )
        target = (*args[:4], b.snapshot, confirmation, f"flare-{seq}", args[7])
        service.prepare(*target, expires_at=time.time() + 120, candidate_id="b")
        if seq == 6:
            monkeypatch.setattr(
                offline_submission, "_pre_dispatch_validate", lambda _: None
            )
        result = offline_submission.submit_offline(service, *target, retry_of="op")
        if seq < 6:
            assert result.state == "unknown" and len(calls) == 2
            with service.store._connect() as db:
                assert (
                    verified_recoverability(
                        db,
                        "op",
                        result.attempt_id,
                        args[0].task_id,
                        1,
                        b.snapshot.execution_digest,
                    )
                    == "transient_local"
                )
                assert (
                    verified_recoverability(
                        db,
                        "op",
                        result.attempt_id,
                        args[0].task_id,
                        1,
                        args[4].execution_digest,
                    )
                    == "unknown"
                )
            service.store.record_retry_wait(
                "op", result.attempt_id, time.time() - 10, None, 0
            )
        else:
            assert result.state == "failed" and len(calls) == 3
    view = candidate_preflight.budget_view(service.store, "op", "b")
    assert view.complete and (view.total_used, view.combination_used) == (6, 3)


@pytest.mark.parametrize(
    "change",
    ["source", "digest", "remote_conflict", "phase", "missing_category", "candidate"],
)
def test_b_evidence_conflict_and_missing_data_never_qualify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
):
    from .test_cross_candidate_submission import setup_cross

    service, target, _, _, _, b, sent = setup_cross(tmp_path, monkeypatch)

    def transient(_request):
        raise faults.TransientLocalError("fixture")

    monkeypatch.setattr(offline_submission, "_pre_dispatch_validate", transient)
    fourth = offline_submission.submit_offline(service, *target, retry_of="op")
    assert fourth.state == "unknown" and sent == []
    task_id = target[0].task_id
    digest = b.snapshot.execution_digest
    code = "import sqlite3,sys; from ecommerce_product_image_creator.send_evidence import verified_recoverability; d=sqlite3.connect(sys.argv[1]); print(verified_recoverability(d,'op',sys.argv[2],sys.argv[3],1,sys.argv[4]))"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.store.path),
            fourth.attempt_id,
            task_id,
            digest,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "transient_local"
    with service.store._connect() as db:
        assert (
            safe_failure_kind(db, "op", fourth.attempt_id, task_id, 1, digest)
            == "pre_send"
        )
        assert (
            safe_failure_kind(db, "other", fourth.attempt_id, task_id, 1, digest)
            == "unverified"
        )
        assert (
            safe_failure_kind(db, "op", fourth.attempt_id, "other", 1, digest)
            == "unverified"
        )
        if change == "source":
            db.execute(
                "UPDATE attempt_fault_evidence SET source='unverified' WHERE attempt_id=?",
                (fourth.attempt_id,),
            )
        elif change == "digest":
            db.execute(
                "UPDATE attempt_fault_evidence SET execution_digest='incorrect' WHERE attempt_id=?",
                (fourth.attempt_id,),
            )
        elif change == "remote_conflict":
            db.execute(
                "UPDATE attempt SET upstream_status='failed',upstream_id='conflict' WHERE attempt_id=?",
                (fourth.attempt_id,),
            )
        elif change == "phase":
            db.execute(
                "UPDATE attempt_transport_evidence SET phase='entered' WHERE attempt_id=?",
                (fourth.attempt_id,),
            )
        elif change == "candidate":
            db.execute(
                "UPDATE candidate_attempt SET candidate_id='a' WHERE attempt_id=?",
                (fourth.attempt_id,),
            )
        else:
            db.execute(
                "DELETE FROM attempt_fault_evidence WHERE attempt_id=?",
                (fourth.attempt_id,),
            )
        assert (
            safe_failure_kind(db, "op", fourth.attempt_id, task_id, 1, digest)
            != "pre_send"
        )
    assert not candidate_preflight.preflight(service, "op", "b").eligible
    confirmation = replace(target[5], confirmation_id="denied-evidence")
    next_args = (*target[:5], confirmation, "denied-operation", target[7])
    service.prepare(*next_args, expires_at=time.time() + 120, candidate_id="b")
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *next_args, retry_of="op")
    assert sent == []
