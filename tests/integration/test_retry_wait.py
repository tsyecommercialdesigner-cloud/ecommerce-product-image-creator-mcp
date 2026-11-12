from pathlib import Path

import pytest

from ecommerce_product_image_creator.storage import ConflictError, Store


def setup_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "state.sqlite")
    store.create_task("task", "input", "request")
    store.create_revision("task", 1, "input")
    store.claim_operation("op", "task", 1, "digest", "attempt")
    store.increment_attempt_counter("op", "model", "endpoint")
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='failed' WHERE attempt_id='attempt'"
        )
    return store


def test_wait_is_durable_and_never_grants_send(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    store.record_retry_wait("op", "attempt", 100.0, None, 500)
    assert store.retry_wait_status("op", 104.0) == "waiting"
    assert (
        Store(store.path).retry_wait_status("op", 105.0) == "elapsed_recheck_required"
    )
    assert Store(store.path).retry_wait_status("op", 103.0) == "clock_invalid"


def test_429_delay_and_invalid_header(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    with pytest.raises(ConflictError):
        store.record_retry_wait("op", "attempt", 100.0, "invalid", 429)
    store.record_retry_wait("op", "attempt", 100.0, "7", 429)
    assert Store(store.path).retry_wait_status("op", 106.0) == "waiting"
    assert store.retry_wait_status("op", 107.0) == "elapsed_recheck_required"


def test_unknown_and_conflicting_wait_are_rejected(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    with pytest.raises(ConflictError):
        store.record_retry_wait("op", "attempt", 100.0, None, 400)
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='unknown' WHERE attempt_id='attempt'"
        )
    with pytest.raises(ConflictError):
        store.record_retry_wait("op", "attempt", 100.0, None, 500)
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='failed' WHERE attempt_id='attempt'"
        )
    store.record_retry_wait("op", "attempt", 100.0, None, 500)
    with pytest.raises(ConflictError):
        store.record_retry_wait("op", "attempt", 100.0, "12", 429)


def test_http_date_and_missing_history_are_conservative(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    assert store.retry_wait_status("missing", 100.0) == "unknown"
    store.record_retry_wait(
        "op", "attempt", 100.0, "Thu, 01 Jan 1970 00:01:50 GMT", 429
    )
    assert Store(store.path).retry_wait_status("op", 109.0) == "waiting"
    assert (
        Store(store.path).retry_wait_status("op", 110.0) == "elapsed_recheck_required"
    )


def test_retry_wait_survives_new_python_process(tmp_path: Path) -> None:
    import subprocess
    import sys

    store = setup_store(tmp_path)
    store.record_retry_wait("op", "attempt", 100.0, None, 500)
    code = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; print(Store(Path(sys.argv[1])).retry_wait_status('op', 104.0))"
    child = subprocess.run(
        [sys.executable, "-c", code, str(store.path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "waiting"
