"""Deterministic eligibility versus wait interleaving; fake transport only."""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator import offline_submission, retry_scheduler

from .test_cross_candidate_submission import setup_cross


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def test_invalid_eligibility_cannot_block_or_consume_valid_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, invalid, _, _, _, b, sent = setup_cross(tmp_path, monkeypatch)
    confirmation = replace(invalid[5], confirmation_id="valid-race")
    valid = (*invalid[:5], confirmation, "valid-race-op", invalid[7])
    service.prepare(*valid, expires_at=time.time() + 120, candidate_id="b")
    monkeypatch.setattr(
        offline_submission,
        "_verify_cross_authorization",
        lambda _db, c, op, cid: (
            c.snapshot == b.snapshot and (op, cid) == ("valid-race-op", "valid-race")
        ),
    )
    entered = threading.Event()
    release = threading.Event()
    wait = service.store.retry_wait_status

    def controlled(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return wait(*args, **kwargs)

    monkeypatch.setattr(service.store, "retry_wait_status", controlled)
    scheduler = retry_scheduler.OfflineRetryScheduler(time.time)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(
            scheduler.step, service, *valid, root="op", candidate_id="b"
        )
        assert entered.wait(timeout=10)
        denied = pool.submit(
            scheduler.step, service, *invalid, root="op", candidate_id="b"
        ).result(timeout=10)
        assert denied.action == "stop" and denied.reason == "eligibility_unavailable"
        release.set()
        result = running.result(timeout=10)
    assert result.action == "submitted" and len(sent) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id=?", (valid[6],)
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE operation_id='op' AND sequence=4"
        ).fetchone() == (1,)
