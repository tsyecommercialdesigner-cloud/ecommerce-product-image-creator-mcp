"""First delivery must not require a pre-existing Artifact record."""

import hashlib
import multiprocessing
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from ecommerce_product_image_creator import (
    artifact_locator,
    first_artifact,
    original_query,
    recovery,
    storage,
)
from ecommerce_product_image_creator.artifact_locator import LocatorEvidence
from ecommerce_product_image_creator.storage import ResourceBudget, Store

from .test_original_query import setup_query


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def _reconcile_worker(db_path, root, ready, start, output):
    def deny(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny
    original_query._verify_original_binding = lambda *_: True
    recovery._verify_observation_source = lambda *_: True
    artifact_locator._verify_locator_source = lambda *_: True
    artifact_locator._verify_fixture_access = lambda *_: True
    first_artifact._verify_digest_source = lambda *_: True
    store = Store(db_path)
    ready.put(True)
    if not start.wait(30):
        output.put("timeout")
        return
    try:
        output.put(
            store.reconcile_first_artifact(
                "first", root, ResourceBudget(100000, 10000, 100000)
            )
        )
    except (ValueError, OSError) as exc:
        output.put(type(exc).__name__ + ":" + str(exc))


def _integrity_worker(
    db_path, root, attempt_id, ready, start, output, crash_phase=None
):
    def hook(phase):
        if phase == crash_phase:
            os._exit(91)

    storage._integrity_hook = hook
    store = Store(db_path)
    ready.put(True)
    if not start.wait(30):
        return
    try:
        output.put(
            store.reconcile_delivered_artifact(
                attempt_id,
                "first-artifact",
                root,
                ResourceBudget(100000, 10000, 100000),
            )
        )
    except (ValueError, OSError) as exc:
        output.put(type(exc).__name__ + ":" + str(exc))


def _paused_integrity_worker(db_path, root, attempt_id, observed, resume, output):
    def hook(phase):
        if phase == "observed":
            observed.set()
            if not resume.wait(30):
                raise RuntimeError("resume timed out")

    storage._integrity_hook = hook
    try:
        result = Store(db_path).reconcile_delivered_artifact(
            attempt_id, "first-artifact", root, ResourceBudget(100000, 10000, 100000)
        )
        output.put(result)
    except (ValueError, OSError) as exc:
        output.put(str(exc))


def _exclusive_file_worker(target, ready, release):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(target), 0x80000000, 0, None, 3, 0, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        ready.put(False)
        return
    try:
        ready.put(True)
        release.wait(30)
    finally:
        close(handle)


def test_locked_file_persists_unverified_delivery(first_context, tmp_path):
    service, args, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Queue(), ctx.Event()
    holder = ctx.Process(target=_exclusive_file_worker, args=(target, ready, release))
    holder.start()
    try:
        assert ready.get(timeout=30)
        assert (
            service.store.reconcile_delivered_artifact(
                attempt.attempt_id, "first-artifact", tmp_path, service.budget
            )
            == "missing_or_corrupt"
        )
        assert (
            service.store.reconcile_delivered_artifact(
                attempt.attempt_id, "first-artifact", tmp_path, service.budget
            )
            == "missing_or_corrupt"
        )
        with service.store._connect() as db:
            assert db.execute(
                "SELECT state,reason,previous_status FROM artifact_integrity_observation"
            ).fetchall() == [
                ("missing_or_corrupt", "file_pin_unavailable", "delivered")
            ]
            assert db.execute("SELECT delivery_status FROM attempt").fetchone() == (
                "missing_or_corrupt",
            )
        from ecommerce_product_image_creator.task_view import (
            ReadOnlyStore,
            read_task_view,
        )

        view = read_task_view(
            ReadOnlyStore(service.store.path), args[0].task_id, tmp_path, service.budget
        )
        result = next(
            item
            for item in view["attempts"]
            if item["attempt_id"] == attempt.attempt_id
        )
        assert result["delivery_status"] == "missing_or_corrupt"
        assert (
            result["artifacts"][0]["integrity_observation"]["reason"]
            == "file_pin_unavailable"
        )
    finally:
        release.set()
        holder.join(timeout=30)
        if holder.is_alive():
            holder.terminate()
            holder.join()
    assert holder.exitcode == 0 and target.read_bytes() == contents and sends == [1]


def test_locked_observation_cannot_override_cross_process_recovery(
    first_context, tmp_path
):
    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    ctx = multiprocessing.get_context("spawn")
    lock_ready, release = ctx.Queue(), ctx.Event()
    observed, resume, output = ctx.Event(), ctx.Event(), ctx.Queue()
    ready, start, restored = ctx.Queue(), ctx.Event(), ctx.Queue()
    holder = ctx.Process(
        target=_exclusive_file_worker, args=(target, lock_ready, release)
    )
    observer = ctx.Process(
        target=_paused_integrity_worker,
        args=(
            service.store.path,
            tmp_path,
            attempt.attempt_id,
            observed,
            resume,
            output,
        ),
    )
    restorer = ctx.Process(
        target=_reconcile_worker,
        args=(service.store.path, tmp_path, ready, start, restored),
    )
    holder.start()
    try:
        assert lock_ready.get(timeout=30)
        assert (
            service.store.reconcile_delivered_artifact(
                attempt.attempt_id, "first-artifact", tmp_path, service.budget
            )
            == "missing_or_corrupt"
        )
        observer.start()
        assert observed.wait(30)
        release.set()
        holder.join(timeout=30)
        assert holder.exitcode == 0
        restorer.start()
        assert ready.get(timeout=30)
        start.set()
        assert restored.get(timeout=30) == "delivered"
        restorer.join(timeout=30)
        assert restorer.exitcode == 0
        resume.set()
        assert output.get(timeout=30) == "artifact_observation_stale"
        observer.join(timeout=30)
        assert observer.exitcode == 0
    finally:
        release.set()
        resume.set()
        start.set()
        for child in (holder, observer, restorer):
            if child.pid is not None and child.is_alive():
                child.terminate()
                child.join()
    with service.store._connect() as db:
        assert db.execute(
            "SELECT state,reason FROM artifact_integrity_observation ORDER BY observation_id"
        ).fetchall() == [
            ("missing_or_corrupt", "file_pin_unavailable"),
            ("delivered", "verified_first_artifact_link"),
        ]
        assert db.execute(
            "SELECT delivery_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("delivered",)
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
    assert target.read_bytes() == contents and sends == [1]


def _first_delivery_crash_worker(db_path, root, ready, start, output):
    original_query._verify_original_binding = lambda *_: True
    recovery._verify_observation_source = lambda *_: True
    artifact_locator._verify_locator_source = lambda *_: True
    artifact_locator._verify_fixture_access = lambda *_: True
    first_artifact._verify_digest_source = lambda *_: True

    def hook(phase):
        if phase == "after_link":
            os._exit(92)

    first_artifact._hook = hook
    ready.put(True)
    if not start.wait(30):
        return
    try:
        Store(db_path).reconcile_first_artifact(
            "first", root, ResourceBudget(100000, 10000, 100000)
        )
    except (ValueError, OSError) as exc:
        output.put(str(exc))


@pytest.fixture
def first_context(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    monkeypatch.setattr(artifact_locator, "_verify_locator_source", lambda *_: True)
    snap = args[4]
    identity = (
        "op",
        "a",
        snap.task_id,
        snap.revision,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
    )
    observation = recovery.recover_once(service.store, *identity)
    assert observation.state == "succeeded" and observation.observation_id is not None
    locator = LocatorEvidence(
        "first-locator",
        *identity,
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "test-account",
        "fixture-upstream",
        observation.observation_id,
        "image-1",
        "fixture_handle",
        "isolated-source",
        "handle-1",
        time.time(),
        time.time() + 300,
    )
    artifact_locator.register(service.store, locator)
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
    data = BytesIO()
    Image.new("RGB", (5, 5), "blue").save(data, format="PNG")
    contents = data.getvalue()
    digest = hashlib.sha256(contents).hexdigest()
    monkeypatch.setattr(
        first_artifact,
        "_verify_digest_source",
        lambda _, sha, mime: sha == digest and mime == "image/png",
    )
    monkeypatch.setattr(artifact_locator, "_verify_fixture_access", lambda _: True)
    monkeypatch.setattr(
        artifact_locator, "_verify_fixture_content", lambda _, data: data == contents
    )
    monkeypatch.setattr(
        artifact_locator, "_fetch_fixture", lambda *_: (contents, "image/png")
    )
    target = tmp_path / "first.png"
    return service, args, attempt, sends, locator, contents, digest, target


def test_first_delivery_without_artifact_record(first_context, tmp_path):
    service, _args, attempt, sends, locator, contents, digest, target = first_context
    with service.store._connect() as db:
        before = db.execute(
            "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone()
        counter = db.execute("SELECT * FROM attempt_counter").fetchall()
    result = service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    assert result == "first"
    assert (
        service.store.stage_first_artifact("first", tmp_path, service.budget)
        == "staged"
    )
    assert (
        service.store.reconcile_first_artifact("first", tmp_path, service.budget)
        == "delivered"
    )
    assert (
        service.store.reconcile_first_artifact("first", tmp_path, service.budget)
        == "delivered"
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT artifact_id,attempt_id,path,sha256 FROM artifact"
        ).fetchall() == [("first-artifact", attempt.attempt_id, str(target), digest)]
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert (
            db.execute(
                "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone()
            == before
        )
        assert db.execute("SELECT * FROM attempt_counter").fetchall() == counter
    assert target.read_bytes() == contents and sends == [1]
    from ecommerce_product_image_creator.task_view import ReadOnlyStore, read_task_view

    view = read_task_view(
        ReadOnlyStore(service.store.path), _args[0].task_id, tmp_path, service.budget
    )
    artifact = next(
        a for a in view["attempts"] if a["attempt_id"] == attempt.attempt_id
    )["artifacts"][0]
    assert artifact["artifact_id"] == "first-artifact"
    assert (
        artifact["delivery_status"] == "delivered"
        and artifact["file"]["state"] == "verified"
    )
    assert artifact["review"]["state"] != "accept"


def test_delivered_missing_is_persisted_without_losing_history(first_context, tmp_path):
    service, _, attempt, sends, locator, _, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    assert (
        service.store.reconcile_first_artifact("first", tmp_path, service.budget)
        == "delivered"
    )
    with service.store._connect() as db:
        upstream = db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone()
        observations = db.execute("SELECT * FROM recovery_observation").fetchall()
    target.unlink()
    assert (
        service.store.reconcile_delivered_artifact(
            attempt.attempt_id, "first-artifact", tmp_path, service.budget
        )
        == "missing_or_corrupt"
    )
    assert (
        service.store.reconcile_delivered_artifact(
            attempt.attempt_id, "first-artifact", tmp_path, service.budget
        )
        == "missing_or_corrupt"
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status,delivery_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == (upstream[0], "missing_or_corrupt")
        assert db.execute("SELECT artifact_id,sha256 FROM artifact").fetchone() == (
            "first-artifact",
            digest,
        )
        assert db.execute(
            "SELECT state,reason,previous_status FROM artifact_integrity_observation"
        ).fetchall() == [("missing_or_corrupt", "missing", "delivered")]
        assert (
            db.execute("SELECT * FROM recovery_observation").fetchall() == observations
        )
    assert sends == [1]


@pytest.mark.parametrize("damage", ["truncated", "different_image"])
def test_delivered_corrupt_is_recorded_and_never_overwritten(
    first_context, tmp_path, damage
):
    service, args, attempt, sends, locator, _, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    target.unlink()
    if damage == "truncated":
        altered = b"\x89PNG\r\n"
    else:
        data = BytesIO()
        Image.new("RGB", (5, 5), "red").save(data, format="PNG")
        altered = data.getvalue()
    target.write_bytes(altered)
    assert (
        service.store.reconcile_delivered_artifact(
            attempt.attempt_id, "first-artifact", tmp_path, service.budget
        )
        == "missing_or_corrupt"
    )
    with pytest.raises(ValueError):
        service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    assert target.read_bytes() == altered and sends == [1]
    with service.store._connect() as db:
        reason = db.execute(
            "SELECT reason FROM artifact_integrity_observation"
        ).fetchone()[0]
        assert reason == (
            "invalid_or_unreadable" if damage == "truncated" else "digest_mismatch"
        )
        assert db.execute("SELECT sha256 FROM artifact").fetchone() == (digest,)
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
    from ecommerce_product_image_creator.task_view import ReadOnlyStore, read_task_view

    view = read_task_view(
        ReadOnlyStore(service.store.path), args[0].task_id, tmp_path, service.budget
    )
    a = next(x for x in view["attempts"] if x["attempt_id"] == attempt.attempt_id)
    assert a["delivery_status"] == "missing_or_corrupt"
    assert a["artifacts"][0]["file"]["state"] == reason


def test_missing_first_artifact_recovers_without_overwrite_and_preserves_observation(
    first_context, tmp_path
):
    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    target.unlink()
    service.store.reconcile_delivered_artifact(
        attempt.attempt_id, "first-artifact", tmp_path, service.budget
    )
    assert (
        service.store.reconcile_first_artifact("first", tmp_path, service.budget)
        == "delivered"
    )
    assert target.read_bytes() == contents and sends == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT state,previous_status FROM artifact_integrity_observation ORDER BY observation_id"
        ).fetchall() == [
            ("missing_or_corrupt", "delivered"),
            ("delivered", "missing_or_corrupt"),
        ]
        assert db.execute("SELECT delivery_status FROM attempt").fetchone() == (
            "delivered",
        )


def test_stale_missing_observation_cannot_override_restoration(
    first_context, tmp_path, monkeypatch
):
    service, _, attempt, sends, locator, _, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    target.unlink()
    observed, resume = threading.Event(), threading.Event()

    def hook(phase):
        if phase == "observed":
            observed.set()
            assert resume.wait(30)

    monkeypatch.setattr(storage, "_integrity_hook", hook)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            service.store.reconcile_delivered_artifact,
            attempt.attempt_id,
            "first-artifact",
            tmp_path,
            service.budget,
        )
        try:
            assert observed.wait(30)
            service.store.reconcile_first_artifact("first", tmp_path, service.budget)
        finally:
            resume.set()
        with pytest.raises(ValueError, match="artifact_observation_stale"):
            future.result(timeout=30)
    with service.store._connect() as db:
        assert db.execute("SELECT delivery_status FROM attempt").fetchone() == (
            "delivered",
        )
        assert db.execute(
            "SELECT count(*) FROM artifact_integrity_observation"
        ).fetchone() == (0,)
    assert sends == [1]


def test_cross_process_stale_observation_loses_to_verified_recovery(
    first_context, tmp_path
):
    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    target.unlink()
    service.store.reconcile_delivered_artifact(
        attempt.attempt_id, "first-artifact", tmp_path, service.budget
    )
    with service.store._connect() as db:
        before = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("attempt_counter", "operation_attempt")
        }
    ctx = multiprocessing.get_context("spawn")
    observed, resume, ready, start = ctx.Event(), ctx.Event(), ctx.Queue(), ctx.Event()
    output, restored = ctx.Queue(), ctx.Queue()
    a = ctx.Process(
        target=_paused_integrity_worker,
        args=(
            service.store.path,
            tmp_path,
            attempt.attempt_id,
            observed,
            resume,
            output,
        ),
    )
    b = ctx.Process(
        target=_reconcile_worker,
        args=(service.store.path, tmp_path, ready, start, restored),
    )
    a.start()
    try:
        assert observed.wait(30)
        b.start()
        assert ready.get(timeout=30)
        start.set()
        assert restored.get(timeout=45) == "delivered"
        b.join(timeout=30)
        assert b.exitcode == 0
        resume.set()
        assert output.get(timeout=30) == "artifact_observation_stale"
        a.join(timeout=30)
        assert a.exitcode == 0
    finally:
        resume.set()
        start.set()
        for process in (a, b):
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join()
    with service.store._connect() as db:
        assert db.execute(
            "SELECT state,previous_status FROM artifact_integrity_observation ORDER BY observation_id"
        ).fetchall() == [
            ("missing_or_corrupt", "delivered"),
            ("delivered", "missing_or_corrupt"),
        ]
        assert db.execute(
            "SELECT delivery_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("delivered",)
        for table, rows in before.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == rows
    assert target.read_bytes() == contents and sends == [1]


def test_published_before_database_crash_is_reconciled(first_context, tmp_path):
    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    ctx = multiprocessing.get_context("spawn")
    ready, start, output = ctx.Queue(), ctx.Event(), ctx.Queue()
    child = ctx.Process(
        target=_first_delivery_crash_worker,
        args=(service.store.path, tmp_path, ready, start, output),
    )
    child.start()
    try:
        assert ready.get(timeout=30)
        start.set()
        child.join(timeout=45)
        assert child.exitcode == 92
    finally:
        if child.is_alive():
            child.terminate()
            child.join()
    assert target.read_bytes() == contents
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
    assert (
        service.store.reconcile_first_artifact("first", tmp_path, service.budget)
        == "delivered"
    )
    assert (
        service.store.reconcile_first_artifact("first", tmp_path, service.budget)
        == "delivered"
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT artifact_id,attempt_id,sha256 FROM artifact"
        ).fetchall() == [("first-artifact", attempt.attempt_id, digest)]
    assert sends == [1]


def test_unpinnable_file_fails_closed_without_claiming_corruption(
    first_context, tmp_path, monkeypatch
):
    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    from ecommerce_product_image_creator import recovery_intent

    def deny(_path):
        raise ValueError("stage_file_unavailable")

    monkeypatch.setattr(recovery_intent, "_pin_file", deny)
    assert (
        service.store.reconcile_delivered_artifact(
            attempt.attempt_id, "first-artifact", tmp_path, service.budget
        )
        == "missing_or_corrupt"
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT reason FROM artifact_integrity_observation"
        ).fetchone() == ("file_pin_unavailable",)
        assert db.execute("SELECT delivery_status FROM attempt").fetchone() == (
            "missing_or_corrupt",
        )
    assert target.read_bytes() == contents and sends == [1]


def test_windows_exclusive_file_handle_stops_without_claiming_corruption(
    first_context, tmp_path
):
    import ctypes
    from ctypes import wintypes

    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(target), 0x80000000, 0, None, 3, 0, None)
    assert handle and handle != ctypes.c_void_p(-1).value
    try:
        assert (
            service.store.reconcile_delivered_artifact(
                attempt.attempt_id, "first-artifact", tmp_path, service.budget
            )
            == "missing_or_corrupt"
        )
    finally:
        assert close(handle)
    with service.store._connect() as db:
        assert db.execute(
            "SELECT reason FROM artifact_integrity_observation"
        ).fetchone() == ("file_pin_unavailable",)
        assert db.execute("SELECT delivery_status FROM attempt").fetchone() == (
            "missing_or_corrupt",
        )
    assert target.read_bytes() == contents and sends == [1]


def test_unreadable_file_is_not_claimed_to_be_corrupt(
    first_context, tmp_path, monkeypatch
):
    from ecommerce_product_image_creator import artifact_reader

    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)

    def deny(*_args, **_kwargs):
        raise PermissionError("fixture access denied")

    monkeypatch.setattr(artifact_reader, "read_reference", deny)
    assert (
        service.store.reconcile_delivered_artifact(
            attempt.attempt_id, "first-artifact", tmp_path, service.budget
        )
        == "missing_or_corrupt"
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT reason,previous_status FROM artifact_integrity_observation"
        ).fetchall() == [("invalid_or_unreadable", "delivered")]
    assert target.read_bytes() == contents and sends == [1]


def test_successful_upstream_with_local_save_failure_is_separate(
    first_context, tmp_path, monkeypatch
):
    from ecommerce_product_image_creator.domain import DeliveryStatus, UpstreamStatus

    service, _, attempt, sends, locator, _, digest, target = first_context
    service.store.record_upstream(
        attempt.attempt_id, UpstreamStatus.SUCCEEDED, "fixture-upstream"
    )
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )

    def fail(phase):
        if phase == "partial_write":
            raise OSError("isolated write failure")

    monkeypatch.setattr(first_artifact, "_hook", fail)
    with pytest.raises(OSError, match="isolated write failure"):
        service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.record_delivery_failure(
        attempt.attempt_id, DeliveryStatus.SAVE_FAILED
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status,delivery_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("succeeded", "save_failed")
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
    assert not target.exists() and sends == [1]


@pytest.mark.parametrize("invalidate", [False, True])
def test_form_original_recovered_from_mock_frozen_input_after_anomaly(
    first_context, tmp_path, monkeypatch, invalidate
):
    from ecommerce_product_image_creator import preparation, recovery_intent
    from ecommerce_product_image_creator.domain import UpstreamStatus
    from ecommerce_product_image_creator.review import (
        FormReview,
        invalidate_review,
        save_review,
    )

    service, args, attempt, sends, locator, contents, digest, target = first_context
    snap = args[4]
    service.store.record_upstream(
        attempt.attempt_id, UpstreamStatus.SUCCEEDED, "fixture-upstream"
    )
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    monkeypatch.setattr(
        preparation,
        "_review_is_trusted",
        lambda source, task, review, sha, evidence: (
            source == "fixture" and review == "review" and sha == digest
        ),
    )
    save_review(
        service.store,
        FormReview(
            "review",
            snap.task_id,
            snap.revision,
            "first-artifact",
            digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    mock = preparation.prepare_mock(
        service.store,
        tmp_path,
        service.budget,
        snap.task_id,
        "first-artifact",
        "review",
        [
            {
                "ref_id": "artwork",
                "category": "artwork",
                "path": str(tmp_path / "artwork.png"),
                "mime": "image/png",
            }
        ],
        "mock prompt",
        snap.model,
        snap.endpoint,
        snap.group,
    )
    assert mock["revision"] != snap.revision
    target.unlink()
    service.store.reconcile_delivered_artifact(
        attempt.attempt_id, "first-artifact", tmp_path, service.budget
    )
    identity = (
        service.store,
        "restore-form",
        "op",
        "a",
        snap.task_id,
        snap.revision,
        "form",
        attempt.attempt_id,
        snap.execution_digest,
        locator.observation_id,
    )
    assert (
        recovery_intent.register_pending(
            *identity,
            artifact_id="first-artifact",
            artifact_root=tmp_path,
            budget=service.budget,
        )
        == "restore-form"
    )
    assert (
        recovery_intent.stage_pending(
            service.store, "restore-form", tmp_path, service.budget
        )
        == "staged"
    )
    if invalidate:
        invalidate_review(service.store, "review", "fixture-invalidated")
        with pytest.raises(ValueError):
            recovery_intent.publish_staged(
                service.store, "restore-form", tmp_path, service.budget
            )
        assert not target.exists() and sends == [1]
        with service.store._connect() as db:
            assert db.execute(
                "SELECT delivery_status FROM attempt WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone() == ("missing_or_corrupt",)
            assert db.execute(
                "SELECT count(*) FROM artifact_recovery_reconciliation"
            ).fetchone() == (0,)
        return
    assert recovery_intent.publish_staged(
        service.store, "restore-form", tmp_path, service.budget
    ) in ("published", "pending_reconciliation")
    assert (
        recovery_intent.reconcile_published(
            service.store, "restore-form", tmp_path, service.budget
        )
        == "reconciled"
    )
    assert (
        recovery_intent.reconcile_published(
            service.store, "restore-form", tmp_path, service.budget
        )
        == "reconciled"
    )
    assert target.read_bytes() == contents and sends == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT state,previous_status FROM artifact_integrity_observation ORDER BY observation_id"
        ).fetchall() == [
            ("missing_or_corrupt", "delivered"),
            ("delivered", "missing_or_corrupt"),
        ]
        assert db.execute(
            "SELECT count(*) FROM artifact_recovery_reconciliation"
        ).fetchone() == (1,)


def test_same_bytes_new_file_identity_is_not_accepted(first_context, tmp_path):
    service, args, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    target.unlink()
    target.write_bytes(contents)
    assert (
        service.store.reconcile_delivered_artifact(
            attempt.attempt_id, "first-artifact", tmp_path, service.budget
        )
        == "missing_or_corrupt"
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT reason FROM artifact_integrity_observation"
        ).fetchone() == ("identity_mismatch",)
    from ecommerce_product_image_creator.task_view import ReadOnlyStore, read_task_view

    view = read_task_view(
        ReadOnlyStore(service.store.path), args[0].task_id, tmp_path, service.budget
    )
    attempt_view = next(
        x for x in view["attempts"] if x["attempt_id"] == attempt.attempt_id
    )
    assert attempt_view["artifacts"][0]["file"]["state"] == "identity_unverified"
    assert sends == [1]


def test_wrong_attempt_and_database_digest_conflict_fail_closed(
    first_context, tmp_path
):
    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    with pytest.raises(ValueError, match="artifact_identity_unverified"):
        service.store.reconcile_delivered_artifact(
            "wrong-attempt", "first-artifact", tmp_path, service.budget
        )
    with service.store._connect() as db:
        db.execute(
            "UPDATE artifact SET sha256=? WHERE artifact_id=?",
            ("0" * 64, "first-artifact"),
        )
    with pytest.raises(ValueError, match="artifact_identity_unverified"):
        service.store.reconcile_delivered_artifact(
            attempt.attempt_id, "first-artifact", tmp_path, service.budget
        )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM artifact_integrity_observation"
        ).fetchone() == (0,)
        assert db.execute("SELECT delivery_status FROM attempt").fetchone() == (
            "delivered",
        )
    assert target.read_bytes() == contents and sends == [1]


@pytest.mark.parametrize("crash_phase", [None, "before_commit", "after_commit"])
def test_integrity_competing_processes_and_crash_replay(
    first_context, tmp_path, crash_phase
):
    service, _, attempt, sends, locator, _, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    target.unlink()
    ctx = multiprocessing.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    count = 2 if crash_phase is None else 1
    children = [
        ctx.Process(
            target=_integrity_worker,
            args=(
                service.store.path,
                tmp_path,
                attempt.attempt_id,
                ready,
                start,
                output,
                crash_phase,
            ),
        )
        for _ in range(count)
    ]
    for child in children:
        child.start()
    try:
        for _ in children:
            assert ready.get(timeout=30)
        start.set()
        for child in children:
            child.join(timeout=45)
            assert child.exitcode == (0 if crash_phase is None else 91)
        if crash_phase is None:
            assert [output.get(timeout=5) for _ in children] == [
                "missing_or_corrupt"
            ] * 2
        if crash_phase == "before_commit":
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT count(*) FROM artifact_integrity_observation"
                ).fetchone() == (0,)
                assert db.execute("SELECT delivery_status FROM attempt").fetchone() == (
                    "delivered",
                )
        assert (
            service.store.reconcile_delivered_artifact(
                attempt.attempt_id, "first-artifact", tmp_path, service.budget
            )
            == "missing_or_corrupt"
        )
        with service.store._connect() as db:
            assert db.execute(
                "SELECT count(*) FROM artifact_integrity_observation"
            ).fetchone() == (1,)
            assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (
                1,
            )
    finally:
        start.set()
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join()
    assert sends == [1]


def test_wrong_content_never_stages_or_registers(first_context, tmp_path, monkeypatch):
    service, _, attempt, sends, locator, _, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    monkeypatch.setattr(
        artifact_locator, "_fetch_fixture", lambda *_: (b"different", "image/png")
    )
    with pytest.raises(ValueError, match="first_content_unverified"):
        service.store.stage_first_artifact("first", tmp_path, service.budget)
    assert not target.exists() and sends == [1]
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM first_artifact_stage").fetchone() == (
            0,
        )
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == (1,)


def test_wrong_identity_and_untrusted_digest_stop(first_context, tmp_path, monkeypatch):
    service, _, attempt, sends, locator, _, digest, target = first_context
    monkeypatch.setattr(first_artifact, "_verify_digest_source", lambda *_: False)
    with pytest.raises(ValueError, match="first_source_unverified"):
        service.store.register_first_artifact(
            "first",
            "first-artifact",
            locator.locator_id,
            digest,
            "image/png",
            tmp_path,
            target,
        )
    monkeypatch.setattr(first_artifact, "_verify_digest_source", lambda *_: True)
    with pytest.raises(ValueError, match="first_identity_invalid"):
        service.store.register_first_artifact(
            "first",
            "first-artifact",
            locator.locator_id,
            "",
            "image/png",
            tmp_path,
            target,
        )
    with pytest.raises(ValueError):
        artifact_locator.register(
            service.store,
            replace(locator, locator_id="wrong", revision=locator.revision + 1),
        )
    with pytest.raises(ValueError):
        artifact_locator.register(
            service.store,
            replace(locator, locator_id="wrong-candidate", candidate_id="wrong"),
        )
    with pytest.raises(ValueError):
        artifact_locator.register(
            service.store,
            replace(locator, locator_id="wrong-attempt", attempt_id="wrong"),
        )
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM first_artifact_anchor").fetchone() == (
            0,
        )
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == (1,)
    assert sends == [1]


def test_empty_new_tables_upgrade_preserves_nonempty_history(first_context):
    service, _, attempt, sends, locator, _, digest, target = first_context
    with service.store._connect() as db:
        before = db.execute(
            "SELECT attempt_id,upstream_status,upstream_id,delivery_status FROM attempt"
        ).fetchall()
        old_locator = db.execute("SELECT * FROM artifact_locator_evidence").fetchall()
        db.execute("DROP TABLE first_artifact_stage")
        db.execute("DROP TABLE first_artifact_anchor")
    reopened = Store(service.store.path)
    Store(service.store.path)
    with reopened._connect() as db:
        assert (
            db.execute(
                "SELECT attempt_id,upstream_status,upstream_id,delivery_status FROM attempt"
            ).fetchall()
            == before
        )
        assert (
            db.execute("SELECT * FROM artifact_locator_evidence").fetchall()
            == old_locator
        )
        assert db.execute("SELECT count(*) FROM first_artifact_anchor").fetchone() == (
            0,
        )
        assert db.execute("SELECT count(*) FROM first_artifact_stage").fetchone() == (
            0,
        )
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
    assert (
        attempt.attempt_id == locator.attempt_id
        and not target.exists()
        and sends == [1]
        and len(digest) == 64
    )


@pytest.mark.parametrize("same_content", [False, True])
def test_target_conflict_and_repeated_concurrent_registration(
    first_context, tmp_path, same_content
):
    service, _, attempt, sends, locator, contents, digest, target = first_context

    def register(_):
        return service.store.register_first_artifact(
            "first",
            "first-artifact",
            locator.locator_id,
            digest,
            "image/png",
            tmp_path,
            target,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(register, range(2))) == ["first", "first"]
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    existing = contents if same_content else b"different existing content"
    target.write_bytes(existing)
    with pytest.raises(ValueError):
        service.store.reconcile_first_artifact("first", tmp_path, service.budget)
    assert target.read_bytes() == existing
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM first_artifact_anchor").fetchone() == (
            1,
        )
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == (1,)
    assert sends == [1]


def test_two_processes_register_once(first_context, tmp_path):
    service, _, attempt, sends, locator, contents, digest, target = first_context
    service.store.register_first_artifact(
        "first",
        "first-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    service.store.stage_first_artifact("first", tmp_path, service.budget)
    ctx = multiprocessing.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [
        ctx.Process(
            target=_reconcile_worker,
            args=(service.store.path, tmp_path, ready, start, output),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=30) and ready.get(timeout=30)
        start.set()
        outcomes = [output.get(timeout=45), output.get(timeout=45)]
        for process in processes:
            process.join(timeout=45)
            assert process.exitcode == 0
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
    assert outcomes == ["delivered", "delivered"]
    assert target.read_bytes() == contents
    with service.store._connect() as db:
        assert db.execute(
            "SELECT artifact_id,attempt_id,path,sha256 FROM artifact"
        ).fetchall() == [("first-artifact", attempt.attempt_id, str(target), digest)]
        assert db.execute("SELECT count(*) FROM first_artifact_anchor").fetchone() == (
            1,
        )
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
    assert sends == [1]
