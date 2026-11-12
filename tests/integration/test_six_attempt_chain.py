"""Six-attempt offline chain, using the actual submission and wait APIs."""

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator import candidate_preflight, offline_submission

from .test_cross_candidate_submission import setup_cross


def test_flare_three_attempts_then_budget_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, target, _, _, _, b, sent = setup_cross(tmp_path, monkeypatch)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda req: (
            sent.append(req)
            or offline_submission.ProviderResult("failed", task_id="fake-upstream")
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, root, attempt, c: (
            c.candidate_id == "b"
            and db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt,)
            ).fetchone()
            == ("failed",)
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, op, cid: (
            c.snapshot.execution_digest == b.snapshot.execution_digest
            and (op, cid)
            in {
                ("flare-request", "flare-independent"),
                ("flare-two", "flare-confirm-two"),
                ("flare-three", "flare-confirm-three"),
                ("flare-four", "flare-confirm-four"),
            }
        ),
    )
    previous = offline_submission.submit_offline(service, *target, retry_of="op")
    for number in (2, 3):
        service.store.record_retry_wait(
            "op", previous.attempt_id, time.time() - 10, None, 500
        )
        confirmation = replace(
            target[5],
            confirmation_id=f"flare-confirm-{'two' if number == 2 else 'three'}",
        )
        args = (
            *target[:5],
            confirmation,
            f"flare-{'two' if number == 2 else 'three'}",
            target[7],
        )
        service.prepare(*args, expires_at=time.time() + 120, candidate_id="b")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _, current_args=args: offline_submission.submit_offline(
                        service, *current_args, retry_of="op"
                    ),
                    range(2),
                )
            )
        assert results[0].attempt_id == results[1].attempt_id
        originals = [result for result in results if not result.replayed]
        replays = [result for result in results if result.replayed]
        assert len(originals) == len(replays) == 1
        current = originals[0]
        assert current.state == "failed"
        # A concurrent replay reads the committed state at its own transaction;
        # it may observe the intent before the first result is persisted.
        assert replays[0].state in {"unknown", "failed"}
        settled = offline_submission.submit_offline(service, *args, retry_of="op")
        assert settled.replayed and settled.attempt_id == current.attempt_id
        assert settled.state == "failed"
        with service.store._connect() as db:
            assert db.execute(
                "SELECT sequence,previous_attempt_id FROM operation_attempt WHERE attempt_id=?",
                (current.attempt_id,),
            ).fetchone() == (number + 3, previous.attempt_id)
            assert db.execute(
                "SELECT combination_sequence FROM candidate_attempt WHERE attempt_id=?",
                (current.attempt_id,),
            ).fetchone() == (number,)
        previous = current
    view = candidate_preflight.budget_view(service.store, "op", "b")
    assert view.complete and (
        view.total_used,
        view.combination_used,
        view.source_used,
    ) == (6, 3, 3)
    fourth_confirmation = replace(target[5], confirmation_id="flare-confirm-four")
    fourth_args = (*target[:5], fourth_confirmation, "flare-four", target[7])
    service.prepare(*fourth_args, expires_at=time.time() + 120, candidate_id="b")
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *fourth_args, retry_of="op")
    assert len(sent) == 3
    code = "import sys; from pathlib import Path; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.candidate_preflight import budget_view; v=budget_view(Store(Path(sys.argv[1])),'op','b'); print(v.total_used,v.combination_used,v.complete)"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "6 3 True"
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM retry_wait_attempt WHERE operation_id='op'"
        ).fetchone() == (4,)


def test_fifth_attempt_transaction_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, target, _, _, _, b, sent = setup_cross(tmp_path, monkeypatch)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda req: (
            sent.append(req)
            or offline_submission.ProviderResult("failed", task_id="fixture-id")
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, _root, attempt, _c: (
            db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt,)
            ).fetchone()
            == ("failed",)
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, _op, _cid: (
            c.snapshot.execution_digest == b.snapshot.execution_digest
        ),
    )
    fourth = offline_submission.submit_offline(service, *target, retry_of="op")
    service.store.record_retry_wait(
        "op", fourth.attempt_id, time.time() - 10, None, 500
    )
    confirmation = replace(target[5], confirmation_id="rollback-fifth")
    args = (*target[:5], confirmation, "rollback-operation", target[7])
    service.prepare(*args, expires_at=time.time() + 120, candidate_id="b")
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER fixture_reject_fifth BEFORE INSERT ON candidate_attempt WHEN NEW.combination_sequence=2 BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    with service.store._connect() as db:
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='rollback-operation'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation WHERE operation_id='rollback-operation'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT attempt_count FROM operation WHERE operation_id='op'"
        ).fetchone() == (4,)
        assert db.execute(
            "SELECT SUM(count) FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (4,)
    assert len(sent) == 1


@pytest.mark.parametrize("last_number", [2, 3])
def test_distinct_requests_compete_for_each_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, last_number: int
):
    service, target, _, _, _, b, sent = setup_cross(tmp_path, monkeypatch)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda req: (
            sent.append(req)
            or offline_submission.ProviderResult("failed", task_id="fixture-id")
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, _root, attempt, _c: (
            db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt,)
            ).fetchone()
            == ("failed",)
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, _op, _cid: (
            c.snapshot.execution_digest == b.snapshot.execution_digest
        ),
    )
    previous = offline_submission.submit_offline(service, *target, retry_of="op")
    for number in range(2, last_number + 1):
        service.store.record_retry_wait(
            "op", previous.attempt_id, time.time() - 10, None, 500
        )
        requests = []
        for suffix in ("a", "b"):
            confirmation = replace(target[5], confirmation_id=f"c-{number}-{suffix}")
            args = (*target[:5], confirmation, f"op-{number}-{suffix}", target[7])
            service.prepare(*args, expires_at=time.time() + 120, candidate_id="b")
            requests.append(args)

        def attempt(args):
            try:
                return offline_submission.submit_offline(service, *args, retry_of="op")
            except offline_submission.SubmissionError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, requests))
        successes = [outcome for outcome in outcomes if outcome is not None]
        assert len(successes) == 1
        previous = successes[0]
        with service.store._connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op' AND sequence=?",
                (number + 3,),
            ).fetchone() == (1,)
    assert len(sent) == last_number


@pytest.mark.parametrize("change", ["authorization", "business"])
def test_preflight_never_replaces_current_transaction_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
):
    service, target, _, _, _, b, sent = setup_cross(tmp_path, monkeypatch)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda req: (
            sent.append(req)
            or offline_submission.ProviderResult("failed", task_id="fixture-id")
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, _root, attempt, _c: (
            db.execute(
                "SELECT upstream_status FROM attempt WHERE attempt_id=?", (attempt,)
            ).fetchone()
            == ("failed",)
        ),
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, _op, _cid: (
            c.snapshot.execution_digest == b.snapshot.execution_digest
        ),
    )
    fourth = offline_submission.submit_offline(service, *target, retry_of="op")
    service.store.record_retry_wait(
        "op", fourth.attempt_id, time.time() - 10, None, 500
    )
    confirmation = replace(target[5], confirmation_id="stale-confirmation")
    args = (*target[:5], confirmation, "stale-operation", target[7])
    service.prepare(*args, expires_at=time.time() + 120, candidate_id="b")
    assert candidate_preflight.preflight(service, "op", "b").eligible
    if change == "authorization":
        monkeypatch.setattr(
            offline_submission, "_verify_cross_authorization", lambda *_: False
        )
    else:
        monkeypatch.setattr(candidate_preflight, "_verify_business", lambda *_: False)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(sent) == 1
