"""One offline cross-candidate attempt through the existing submission entry."""

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator import candidate_preflight, offline_submission

from .test_attempt003 import prepare_third
from .test_candidate_preflight import candidates


def setup_cross(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, args, first, second, calls = prepare_third(tmp_path, monkeypatch)
    third = offline_submission.submit_offline(service, *args, retry_of="op")
    assert third.state == "failed" and len(calls) == 3
    _, b = candidates(service, args)
    service.store.record_retry_wait("op", third.attempt_id, time.time() - 10, None, 500)
    confirmation = replace(
        args[5],
        confirmation_id="flare-independent",
        execution_digest=b.snapshot.execution_digest,
    )
    target = (*args[:4], b.snapshot, confirmation, "flare-request", args[7])
    service.prepare(*target, expires_at=time.time() + 120, candidate_id="b")
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_terminal_recoverable",
        lambda db, root, attempt, c: (
            root == "op"
            and c.candidate_id == "b"
            and db.execute(
                "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
                (attempt,),
            ).fetchone()
            == ("failed", "fixture-id")
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_authorization",
        lambda _db, c: c.snapshot.execution_digest == b.snapshot.execution_digest,
    )
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, op, confirmation_id: (
            (c.snapshot.execution_digest, op, confirmation_id)
            == (b.snapshot.execution_digest, "flare-request", "flare-independent")
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_candidate_capability",
        lambda _db, c: (
            (
                c.snapshot.model,
                c.snapshot.endpoint,
                c.snapshot.group,
                c.snapshot.stage,
                c.snapshot.references,
            )
            == (
                b.snapshot.model,
                b.snapshot.endpoint,
                b.snapshot.group,
                b.snapshot.stage,
                b.snapshot.references,
            )
        ),
    )
    monkeypatch.setattr(
        candidate_preflight,
        "_verify_business",
        lambda _db, c: c.snapshot.execution_digest == b.snapshot.execution_digest,
    )
    sent = []
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda request: (
            sent.append(request)
            or offline_submission.ProviderResult("accepted", task_id="flare-fixture")
        ),
    )
    return service, target, first, second, third, b, sent


def test_fourth_attempt_uses_candidate_snapshot_and_independent_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, target, first, second, third, b, sent = setup_cross(tmp_path, monkeypatch)
    fourth = offline_submission.submit_offline(service, *target, retry_of="op")
    assert fourth.state == "accepted" and len(sent) == 1
    assert b"gpt-image-2.5-flare" in sent[0].body
    assert (
        offline_submission.submit_offline(service, *target, retry_of="op").attempt_id
        == fourth.attempt_id
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT sequence,previous_attempt_id,model,execution_digest FROM operation_attempt WHERE attempt_id=?",
            (fourth.attempt_id,),
        ).fetchone() == (
            4,
            third.attempt_id,
            b.snapshot.model,
            b.snapshot.execution_digest,
        )
        assert db.execute(
            "SELECT model,count FROM attempt_counter WHERE operation_id='op' ORDER BY model"
        ).fetchall() == [("gpt-image-2.5-flare", 1), ("gpt-image-2.5-sunburst", 3)]
        assert db.execute(
            "SELECT attempt_count FROM operation WHERE operation_id='op'"
        ).fetchone() == (4,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='flare-request'"
        ).fetchone() == (1,)
    assert first.attempt_id != second.attempt_id != third.attempt_id
    view = candidate_preflight.budget_view(service.store, "op", "b")
    assert view.complete and (
        view.total_used,
        view.combination_used,
        view.source_used,
    ) == (4, 1, 3)
    a_view = candidate_preflight.budget_view(service.store, "op", "a")
    assert a_view.complete and (a_view.total_used, a_view.combination_used) == (4, 3)
    code = "import sys; from pathlib import Path; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.candidate_preflight import budget_view; v=budget_view(Store(Path(sys.argv[1])),'op','b'); print(v.total_used,v.combination_used,v.complete)"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "4 1 True"


def test_transaction_failure_rolls_back_qualification_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, target, _, _, _, _, sent = setup_cross(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER fixture_abort BEFORE INSERT ON candidate_attempt BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *target, retry_of="op")
    with service.store._connect() as db:
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='flare-request'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation WHERE operation_id='flare-request'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT attempt_count FROM operation WHERE operation_id='op'"
        ).fetchone() == (3,)
        assert db.execute(
            "SELECT SUM(count) FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (3,)
    assert sent == []


def test_concurrent_same_request_produces_at_most_one_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, target, _, _, _, _, sent = setup_cross(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: offline_submission.submit_offline(
                    service, *target, retry_of="op"
                ),
                range(2),
            )
        )
    assert results[0].attempt_id == results[1].attempt_id and len(sent) == 1


def test_distinct_requests_cannot_both_claim_fourth_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, target, _, _, _, b, sent = setup_cross(tmp_path, monkeypatch)
    other_confirmation = replace(target[5], confirmation_id="another-confirmation")
    other = (*target[:5], other_confirmation, "another-request", target[7])
    service.prepare(*other, expires_at=time.time() + 120, candidate_id="b")
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, op, cid: (
            c.snapshot.execution_digest == b.snapshot.execution_digest
            and (op, cid)
            in {
                ("flare-request", "flare-independent"),
                ("another-request", "another-confirmation"),
            }
        ),
    )

    def run(args):
        try:
            return offline_submission.submit_offline(service, *args, retry_of="op")
        except offline_submission.SubmissionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(run, (target, other)))
    assert sum(value is not None for value in values) == 1
    assert len(sent) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op' AND sequence=4"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "failure",
    ["authorization", "capability", "unknown", "processing", "wait", "default"],
)
def test_cross_candidate_denial_never_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    service, target, _, _, third, _, sent = setup_cross(tmp_path, monkeypatch)
    if failure == "authorization":
        monkeypatch.setattr(
            offline_submission, "_verify_cross_authorization", lambda *_: False
        )
    elif failure in ("capability", "default"):
        monkeypatch.setattr(
            candidate_preflight, "_verify_candidate_capability", lambda *_: False
        )
    elif failure in ("unknown", "processing"):
        with service.store._connect() as db:
            db.execute(
                "UPDATE attempt SET upstream_status=? WHERE attempt_id=?",
                ("unknown" if failure == "unknown" else "running", third.attempt_id),
            )
    else:
        with service.store._connect() as db:
            db.execute(
                "UPDATE retry_wait_attempt SET deadline=? WHERE operation_id='op' AND attempt_id=?",
                (time.time() + 60, third.attempt_id),
            )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *target, retry_of="op")
    assert sent == []
