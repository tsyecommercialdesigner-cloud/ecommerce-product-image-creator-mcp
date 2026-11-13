import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ecommerce_product_image_creator.domain import (
    DeliveryStatus,
    TaskStatus,
    UpstreamStatus,
)
from ecommerce_product_image_creator.storage import ConflictError, Store


def test_concurrent_claim_and_restart_preserve_uncertainty(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    store = Store(db)
    store.create_task("task", "input", "instructions")
    store.create_revision("task", 1, "input")

    def claim(i: int) -> bool:
        return Store(db).claim_operation("op", "task", 1, "digest", f"attempt-{i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(claim, range(8)))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7
    restarted = Store(db)
    attempt = restarted.get_operation("op")
    assert attempt is not None
    assert attempt.upstream_status == "submission_uncertain"
    assert attempt.upstream_id is None
    assert attempt.delivery_status == "not_available"
    assert attempt.attempt_count == 1
    assert restarted.claim_operation("op", "task", 1, "digest", "another") is False
    with pytest.raises(ConflictError):
        restarted.claim_operation("op", "task", 1, "different", "different")


def test_local_token_consumption_is_atomic_and_not_authorization(
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.sqlite"
    store = Store(db)

    def consume(_i: int) -> bool:
        return Store(db).consume_local_marker("synthetic-marker")

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(consume, range(8)))
    assert outcomes.count(True) == 1
    assert store.consume_local_marker("synthetic-marker") is False


def test_unique_task_revision_attempt_and_foreign_keys(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    store.create_task("task", "input", "instructions")
    with pytest.raises(ConflictError):
        store.create_task("task", "input", "instructions")
    store.create_revision("task", 1, "input")
    with pytest.raises(ConflictError):
        store.create_revision("task", 1, "input")
    with pytest.raises(ConflictError):
        store.create_revision("missing", 1, "input")
    assert store.claim_operation("op1", "task", 1, "d", "a")
    with pytest.raises(ConflictError):
        store.claim_operation("op2", "task", 1, "d", "a")


def test_model_endpoint_counter_survives_restarts(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    store = Store(db)
    store.create_task("task", "input", "instructions")
    store.create_revision("task", 1, "input")
    assert store.claim_operation("op", "task", 1, "digest", "attempt")
    assert store.increment_attempt_counter("op", "model", "endpoint") == 1
    assert Store(db).increment_attempt_counter("op", "model", "endpoint") == 2
    resumed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; print(Store(Path(sys.argv[1])).increment_attempt_counter('op', 'model', 'endpoint'))",
            str(db),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert resumed.stdout.strip() == "3"
    with pytest.raises(ConflictError):
        Store(db).increment_attempt_counter("op", "model", "endpoint")
    assert Store(db).increment_attempt_counter("op", "backup", "endpoint") == 1


def test_upstream_success_is_independent_of_delivery_failure(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    store.create_task("task", "input", "instructions")
    store.create_revision("task", 1, "input")
    store.claim_operation("op", "task", 1, "d", "a")
    store.record_upstream("a", UpstreamStatus.SUCCEEDED, "remote-1")
    store.record_delivery_failure("a", DeliveryStatus.DOWNLOAD_FAILED)
    attempt = Store(tmp_path / "state.sqlite").get_operation("op")
    assert attempt is not None
    assert attempt.upstream_status == "succeeded"
    assert attempt.delivery_status == "download_failed"
    with pytest.raises(ValueError):
        store.record_upstream("a", UpstreamStatus.FAILED, "remote-1")


def test_local_task_transition_is_persisted_and_checked(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "state.sqlite"
    store = Store(db)
    store.create_task("task", "input", "instructions")
    with pytest.raises(ValueError):
        store.transition_task("task", TaskStatus.ACTIVE)
    store.transition_task("task", TaskStatus.AWAITING_CONFIRMATION)
    with sqlite3.connect(db) as connection:
        state = connection.execute(
            "SELECT status FROM task WHERE task_id='task'"
        ).fetchone()
    assert state == ("awaiting_confirmation",)


def test_delivery_failure_does_not_overwrite_delivered_state(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "state.sqlite"
    store = Store(db)
    store.create_task("task", "input", "instructions")
    store.create_revision("task", 1, "input")
    store.claim_operation("op", "task", 1, "digest", "attempt")
    store.record_upstream("attempt", UpstreamStatus.SUCCEEDED, "remote")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE attempt SET delivery_status='delivered' WHERE attempt_id='attempt'"
        )
    with pytest.raises(ConflictError):
        store.record_delivery_failure("attempt", DeliveryStatus.DOWNLOAD_FAILED)
    result = store.get_operation("op")
    assert result is not None and result.delivery_status == "delivered"


def test_legacy_operation_without_chain_is_auditable_but_unknown(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "legacy.sqlite")
    store.create_task("task", "input", "request")
    store.create_revision("task", 1, "input")
    store.claim_operation("old-op", "task", 1, "digest", "attempt")
    assert Store(store.path).get_operation("old-op") is not None
    assert Store(store.path).attempt_chain_status("old-op") == "unknown"
