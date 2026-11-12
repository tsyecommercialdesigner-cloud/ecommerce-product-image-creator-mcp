"""The frozen Mock input is a candidate, never a delivered artifact."""

import hashlib
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator import (
    artifact_locator,
    artifact_reader,
    mock_workflow,
    original_query,
    preparation,
    recovery,
    recovery_intent,
)
from ecommerce_product_image_creator.artifact_locator import assess_attempt
from ecommerce_product_image_creator.domain import UpstreamStatus
from ecommerce_product_image_creator.form_workflow import register_form_artifact
from ecommerce_product_image_creator.review import FormReview, save_review
from ecommerce_product_image_creator.storage import ResourceBudget

from .test_original_query import setup_query


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("valid", "recover_from_local_bytes"),
        ("valid_jpeg", "recover_from_local_bytes"),
        ("valid_jpeg_observed", "recover_from_local_bytes"),
        ("already_available", "already_available"),
        ("remote_candidate", "remote_retrieval_candidate"),
        ("remote_conflict", "remote_retrieval_candidate"),
        ("missing_source", "locator_unverified"),
        ("wrong_artifact", "artifact_conflict"),
        ("wrong_form_revision", "locator_unverified"),
        ("corrupt_bytes", "locator_unverified"),
        ("review_untrusted", "locator_unverified"),
        ("review_rejected", "locator_unverified"),
        ("review_conflict", "artifact_conflict"),
        ("wrong_candidate", "upstream_not_confirmed"),
        ("wrong_stage", "upstream_not_confirmed"),
    ],
)
def test_missing_form_artifact_has_verified_mock_copy_without_mutation(
    tmp_path, monkeypatch, mode, expected
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(
        recovery, "_verify_observation_source", lambda i, row: row[6] == i.attempt_id
    )
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    monkeypatch.setattr(
        preparation,
        "_review_is_trusted",
        lambda source, task, review, digest, evidence: (
            source == "fixture" and review == "review"
        ),
    )
    snap = args[4]
    resolution = recovery.recover_once(
        service.store,
        "op",
        "a",
        snap.task_id,
        snap.revision,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
    )
    assert resolution.observation_id is not None
    service.store.record_upstream(
        attempt.attempt_id, UpstreamStatus.SUCCEEDED, "fixture-upstream"
    )
    mime = (
        "image/jpeg" if mode in {"valid_jpeg", "valid_jpeg_observed"} else "image/png"
    )
    path = tmp_path / (
        "output.jpg" if mode in {"valid_jpeg", "valid_jpeg_observed"} else "output.png"
    )
    Image.new("RGB", (5, 5), "blue").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    budget = ResourceBudget(100000, 10000, 100000)
    register_form_artifact(
        service.store,
        snap.task_id,
        snap.revision,
        attempt.attempt_id,
        "artifact",
        tmp_path,
        path,
        mime,
        digest,
        budget,
    )
    save_review(
        service.store,
        FormReview(
            "review",
            snap.task_id,
            snap.revision,
            "artifact",
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
        budget,
        snap.task_id,
        "artifact",
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
    service.store.claim_operation(
        "mock-op",
        snap.task_id,
        mock["revision"],
        mock["execution_digest"],
        "mock-attempt",
    )
    service.store.record_upstream(
        "mock-attempt", UpstreamStatus.SUCCEEDED, "mock-fixture-id"
    )
    mock_path = tmp_path / "mock-output.png"
    Image.new("RGB", (5, 5), "green").save(mock_path)
    mock_digest = hashlib.sha256(mock_path.read_bytes()).hexdigest()
    mock_workflow.register_mock_artifact(
        service.store,
        snap.task_id,
        mock["revision"],
        "mock-attempt",
        "mock-artifact",
        tmp_path,
        mock_path,
        "image/png",
        mock_digest,
        budget,
    )
    mock_workflow.save_mock_review(
        service.store,
        mock_workflow.MockReview(
            "mock-review",
            snap.task_id,
            mock["revision"],
            "mock-artifact",
            mock_digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    if mode != "already_available":
        path.unlink()
    if mode == "valid":
        # This legacy registration has no first-artifact anchor. Do not invent
        # one: its independent Mock frozen-input evidence remains usable below.
        with pytest.raises(ValueError, match="artifact_identity_unverified"):
            service.store.reconcile_delivered_artifact(
                attempt.attempt_id, "artifact", tmp_path, budget
            )
        with service.store._connect() as db:
            assert db.execute(
                "SELECT count(*) FROM first_artifact_anchor"
            ).fetchone() == (0,)
    if mode == "review_conflict":
        save_review(
            service.store,
            FormReview(
                "other-review",
                snap.task_id,
                snap.revision,
                "artifact",
                digest,
                "reject",
                "fixture",
                time.time(),
                None,
            ),
        )
    with service.store._connect() as db:
        if mode in {"missing_source", "remote_candidate", "remote_conflict"}:
            db.execute(
                "DELETE FROM prepared_mock_source WHERE task_id=?", (snap.task_id,)
            )
        elif mode == "wrong_artifact":
            db.execute(
                "UPDATE prepared_mock_source SET artifact_id='other' WHERE task_id=?",
                (snap.task_id,),
            )
        elif mode == "wrong_form_revision":
            db.execute(
                "UPDATE prepared_mock_source SET form_revision=99 WHERE task_id=?",
                (snap.task_id,),
            )
        elif mode == "corrupt_bytes":
            db.execute(
                "UPDATE prepared_reference SET data=? WHERE task_id=? AND revision=? AND position=0",
                (b"corrupt", snap.task_id, mock["revision"]),
            )
        elif mode == "review_rejected":
            db.execute(
                "UPDATE form_review_detail SET decision='reject' WHERE review_id='review'"
            )
    if mode == "review_untrusted":
        monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: False)
    if mode in {"remote_candidate", "remote_conflict"}:
        monkeypatch.setattr(
            artifact_locator,
            "_verify_locator_source",
            lambda i, e: i.attempt_id == e.attempt_id and e.scope == "fixture-account",
        )
        evidence = artifact_locator.LocatorEvidence(
            "locator",
            "op",
            "a",
            snap.task_id,
            snap.revision,
            "form",
            attempt.attempt_id,
            snap.execution_digest,
            "candidate_gateway",
            snap.model,
            snap.endpoint,
            snap.group,
            "fixture-account",
            "fixture-upstream",
            resolution.observation_id,
            "image-1",
            "fixture_handle",
            "fixture-source",
            "fixture-handle",
            time.time(),
            time.time() + 100,
        )
        artifact_locator.register(service.store, evidence)
    with service.store._connect() as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        before = {
            table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        }
    result = assess_attempt(
        service.store,
        "op",
        "other" if mode == "wrong_candidate" else "a",
        snap.task_id,
        snap.revision,
        "mock" if mode == "wrong_stage" else "form",
        attempt.attempt_id,
        snap.execution_digest,
        resolution.observation_id,
        artifact_id="artifact",
        artifact_root=tmp_path,
        budget=budget,
    )
    assert result.state == expected
    code = ""
    original_validate = artifact_locator.validate_image_bytes

    def assert_no_writer_lock(*args):
        concurrent = sqlite3.connect(service.store.path, timeout=0)
        try:
            concurrent.execute("BEGIN IMMEDIATE")
            concurrent.rollback()
        finally:
            concurrent.close()
        return original_validate(*args)

    if mode in {"valid", "valid_jpeg", "valid_jpeg_observed"}:
        monkeypatch.setattr(
            artifact_locator, "validate_image_bytes", assert_no_writer_lock
        )
        acquired = artifact_locator.acquire_original_bytes(
            service.store,
            "op",
            "a",
            snap.task_id,
            snap.revision,
            "form",
            attempt.attempt_id,
            snap.execution_digest,
            resolution.observation_id,
            artifact_id="artifact",
            artifact_root=tmp_path,
            budget=budget,
        )
        monkeypatch.setattr(artifact_locator, "validate_image_bytes", original_validate)
        with service.store._connect() as transaction:
            transaction.execute("BEGIN IMMEDIATE")
            transaction_result = artifact_locator._acquire_in_transaction(
                transaction,
                service.store,
                "op",
                "a",
                snap.task_id,
                snap.revision,
                "form",
                attempt.attempt_id,
                snap.execution_digest,
                resolution.observation_id,
                artifact_id="artifact",
                artifact_root=tmp_path,
                budget=budget,
            )
            assert transaction_result.sha256 == digest
            competing = sqlite3.connect(service.store.path, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competing.execute(
                        "UPDATE form_review_detail SET decision='reject' WHERE review_id='review'"
                    )
            finally:
                competing.close()
            transaction.rollback()
        with service.store._connect() as transaction:
            transaction.execute("BEGIN IMMEDIATE")
            transaction.execute(
                "UPDATE form_review_detail SET decision='reject' WHERE review_id='review'"
            )
            with pytest.raises(ValueError):
                artifact_locator._acquire_in_transaction(
                    transaction,
                    service.store,
                    "op",
                    "a",
                    snap.task_id,
                    snap.revision,
                    "form",
                    attempt.attempt_id,
                    snap.execution_digest,
                    resolution.observation_id,
                    artifact_id="artifact",
                    artifact_root=tmp_path,
                    budget=budget,
                )
            transaction.rollback()
        assert acquired.sha256 == digest
        assert artifact_locator.verify_acquired_bytes(acquired)
        assert not artifact_locator.verify_acquired_bytes(
            replace(acquired, data=b"changed")
        )
        assert hashlib.sha256(acquired.data).hexdigest() == digest
        assert (acquired.mime, acquired.width, acquired.height) == (mime, 5, 5)
        assert (
            result.artifact_id == "artifact"
            and result.source_mock_revision == mock["revision"]
        )
        assert result.artifact_sha256 == digest and result.source_sha256 == digest
        assert (result.task_id, result.revision, result.stage) == (
            snap.task_id,
            snap.revision,
            "form",
        )
        assert (
            result.attempt_id == attempt.attempt_id
            and result.source_review_id == "review"
        )
        code = """
import socket,sys
from pathlib import Path
from ecommerce_product_image_creator import original_query,preparation,recovery
from ecommerce_product_image_creator.artifact_locator import assess_attempt,acquire_original_bytes,verify_acquired_bytes
from ecommerce_product_image_creator.storage import Store,ResourceBudget
def blocked(*a,**k): raise RuntimeError('network forbidden')
socket.socket.connect=blocked
socket.create_connection=blocked
original_query._verify_original_binding=lambda db,i: i.attempt_id==sys.argv[3]
recovery._verify_observation_source=lambda i,row: row[6]==i.attempt_id
preparation._review_is_trusted=lambda source,task,review,digest,evidence: source=='fixture' and review=='review'
r=assess_attempt(Store(Path(sys.argv[1])),'op','a',sys.argv[2],1,'form',sys.argv[3],sys.argv[4],int(sys.argv[5]),artifact_id='artifact',artifact_root=Path(sys.argv[6]),budget=ResourceBudget(100000,10000,100000))
b=acquire_original_bytes(Store(Path(sys.argv[1])),'op','a',sys.argv[2],1,'form',sys.argv[3],sys.argv[4],int(sys.argv[5]),artifact_id='artifact',artifact_root=Path(sys.argv[6]),budget=ResourceBudget(100000,10000,100000))
assert verify_acquired_bytes(b) and b.sha256 == r.artifact_sha256
print(r.state,r.source_mock_revision)
"""
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(service.store.path),
                snap.task_id,
                attempt.attempt_id,
                snap.execution_digest,
                str(resolution.observation_id),
                str(tmp_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert child.stdout.strip() == f"recover_from_local_bytes {mock['revision']}"
    calls: list[str] = []
    if mode in {"remote_candidate", "remote_conflict"}:
        with service.store._connect() as db:
            frozen = db.execute(
                "SELECT data FROM prepared_reference WHERE task_id=? AND revision=? AND position=0",
                (snap.task_id, mock["revision"]),
            ).fetchone()[0]

        def fetch(evidence, _budget):
            # A slow source must not own the SQLite write lock.
            concurrent = sqlite3.connect(service.store.path, timeout=0)
            try:
                concurrent.execute("BEGIN IMMEDIATE")
                concurrent.rollback()
            finally:
                concurrent.close()
            calls.append(evidence.locator_id)
            return frozen, "image/png"

        monkeypatch.setattr(artifact_locator, "_fetch_fixture", fetch)
        with pytest.raises(ValueError, match="fixture_access_unverified"):
            artifact_locator.acquire_original_bytes(
                service.store,
                "op",
                "a",
                snap.task_id,
                snap.revision,
                "form",
                attempt.attempt_id,
                snap.execution_digest,
                resolution.observation_id,
                artifact_id="artifact",
                artifact_root=tmp_path,
                budget=budget,
            )
        assert calls == []
        monkeypatch.setattr(
            artifact_locator,
            "_verify_fixture_access",
            lambda e: e.scope == "fixture-account",
        )
        monkeypatch.setattr(
            artifact_locator,
            "_verify_fixture_content",
            lambda e, data: e.artifact_identity == "image-1" and data == frozen,
        )
        acquired = artifact_locator.acquire_original_bytes(
            service.store,
            "op",
            "a",
            snap.task_id,
            snap.revision,
            "form",
            attempt.attempt_id,
            snap.execution_digest,
            resolution.observation_id,
            artifact_id="artifact",
            artifact_root=tmp_path,
            budget=budget,
        )
        assert acquired.data == frozen and acquired.sha256 == digest
        assert (
            acquired.expected_digest_verified and acquired.source == "fixture_locator"
        )
        assert calls == ["locator"]
        assert repr(frozen) not in repr(acquired)
        pending = artifact_locator.acquire_original_bytes(
            service.store,
            "op",
            "a",
            snap.task_id,
            snap.revision,
            "form",
            attempt.attempt_id,
            snap.execution_digest,
            resolution.observation_id,
            artifact_id="pending",
            artifact_root=tmp_path,
            budget=budget,
        )
        assert pending.sha256 == digest and not pending.expected_digest_verified
        monkeypatch.setattr(
            artifact_locator,
            "_verify_fixture_access",
            lambda e: e.scope == "wrong-account",
        )
        with pytest.raises(ValueError, match="fixture_access_unverified"):
            artifact_locator.acquire_original_bytes(
                service.store,
                "op",
                "a",
                snap.task_id,
                snap.revision,
                "form",
                attempt.attempt_id,
                snap.execution_digest,
                resolution.observation_id,
                artifact_id="artifact",
                artifact_root=tmp_path,
                budget=budget,
            )
        monkeypatch.setattr(
            artifact_locator,
            "_verify_fixture_access",
            lambda e: e.scope == "fixture-account",
        )
    if mode not in {
        "valid",
        "valid_jpeg",
        "valid_jpeg_observed",
        "remote_candidate",
        "remote_conflict",
    }:
        with pytest.raises(ValueError):
            artifact_locator.acquire_original_bytes(
                service.store,
                "op",
                "other" if mode == "wrong_candidate" else "a",
                snap.task_id,
                snap.revision,
                "mock" if mode == "wrong_stage" else "form",
                attempt.attempt_id,
                snap.execution_digest,
                resolution.observation_id,
                artifact_id="artifact",
                artifact_root=tmp_path,
                budget=budget,
            )
    with service.store._connect() as db:
        assert {
            table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        } == before
    assert path.exists() == (mode == "already_available") and len(sends) == 1
    if mode in {"valid", "valid_jpeg", "valid_jpeg_observed"}:
        pending_crash_code = (
            code
            + "\nimport os\nfrom ecommerce_product_image_creator import recovery_intent\ndef crash(phase):\n if phase == 'pending_before_commit': os._exit(82)\nrecovery_intent._stage_hook=crash\nrecovery_intent.register_pending(Store(Path(sys.argv[1])), 'restore-1', 'op', 'a', sys.argv[2], 1, 'form', sys.argv[3], sys.argv[4], int(sys.argv[5]), artifact_id='artifact', artifact_root=Path(sys.argv[6]), budget=ResourceBudget(100000,10000,100000))\n"
        )
        crash = subprocess.run(
            [
                sys.executable,
                "-c",
                pending_crash_code,
                str(service.store.path),
                snap.task_id,
                attempt.attempt_id,
                snap.execution_digest,
                str(resolution.observation_id),
                str(tmp_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert crash.returncode == 82
        with service.store._connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM artifact_recovery_intent"
            ).fetchone() == (0,)
        monkeypatch.setattr(
            artifact_locator, "validate_image_bytes", assert_no_writer_lock
        )
        pending = recovery_intent.register_pending(
            service.store,
            "restore-1",
            "op",
            "a",
            snap.task_id,
            snap.revision,
            "form",
            attempt.attempt_id,
            snap.execution_digest,
            resolution.observation_id,
            artifact_id="artifact",
            artifact_root=tmp_path,
            budget=budget,
        )
        monkeypatch.setattr(artifact_locator, "validate_image_bytes", original_validate)
        assert pending == "restore-1"

        observation_id = resolution.observation_id
        assert observation_id is not None

        def repeat_pending(name):
            return recovery_intent.register_pending(
                service.store,
                name,
                "op",
                "a",
                snap.task_id,
                snap.revision,
                "form",
                attempt.attempt_id,
                snap.execution_digest,
                observation_id,
                artifact_id="artifact",
                artifact_root=tmp_path,
                budget=budget,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(repeat_pending, ["restore-3", "restore-4"])) == [
                "restore-1",
                "restore-1",
            ]
        assert (
            recovery_intent.register_pending(
                service.store,
                "restore-2",
                "op",
                "a",
                snap.task_id,
                snap.revision,
                "form",
                attempt.attempt_id,
                snap.execution_digest,
                resolution.observation_id,
                artifact_id="artifact",
                artifact_root=tmp_path,
                budget=budget,
            )
            == "restore-1"
        )
        with service.store._connect() as db:
            assert db.execute(
                "SELECT recovery_id,state,expected_sha256 FROM artifact_recovery_intent"
            ).fetchall() == [("restore-1", "pending", digest)]
            assert db.execute(
                "SELECT delivery_status FROM attempt WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone() == ("delivered",)
        assert not path.exists() and len(sends) == 1
        recovered = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('SELECT recovery_id,state FROM artifact_recovery_intent').fetchone())",
                str(service.store.path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert recovered.stdout.strip() == "('restore-1', 'pending')"
        collision = tmp_path / (".recovery-" + "a" * 48 + ".tmp")
        collision.write_bytes(b"unchanged")
        with monkeypatch.context() as isolated:
            isolated.setattr(recovery_intent.secrets, "token_hex", lambda _: "a" * 48)
            with pytest.raises(ValueError, match="stage_create_failed"):
                recovery_intent.stage_pending(
                    service.store, "restore-1", tmp_path, budget
                )
        assert collision.read_bytes() == b"unchanged"
        for crash_phase in ("partial_write", "written", "verified", "before_commit"):
            crash_code = (
                code
                + "\nimport os\nfrom ecommerce_product_image_creator import recovery_intent\ndef crash(phase):\n if phase == sys.argv[7]: os._exit(81)\nrecovery_intent._stage_hook=crash\nrecovery_intent.stage_pending(Store(Path(sys.argv[1])), 'restore-1', Path(sys.argv[6]), ResourceBudget(100000,10000,100000))\n"
            )
            crashed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    crash_code,
                    str(service.store.path),
                    snap.task_id,
                    attempt.attempt_id,
                    snap.execution_digest,
                    str(resolution.observation_id),
                    str(tmp_path),
                    crash_phase,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            assert crashed.returncode == 81
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT state FROM artifact_recovery_intent"
                ).fetchone() == ("pending",)
                assert db.execute(
                    "SELECT COUNT(*) FROM artifact_recovery_stage"
                ).fetchone() == (0,)
            assert not path.exists() and len(sends) == 1

        def revoke_after_verification(phase):
            if phase == "verified":
                with service.store._connect() as db:
                    db.execute(
                        "UPDATE form_review_detail SET decision='reject' WHERE review_id='review'"
                    )

        monkeypatch.setattr(recovery_intent, "_stage_hook", revoke_after_verification)
        with pytest.raises(ValueError, match="source_changed"):
            recovery_intent.stage_pending(service.store, "restore-1", tmp_path, budget)
        with service.store._connect() as db:
            assert db.execute(
                "SELECT state FROM artifact_recovery_intent"
            ).fetchone() == ("pending",)
            assert db.execute(
                "SELECT COUNT(*) FROM artifact_recovery_stage"
            ).fetchone() == (0,)
            db.execute(
                "UPDATE form_review_detail SET decision='accept' WHERE review_id='review'"
            )

        def fail_before_commit(phase):
            if phase == "before_commit":
                raise RuntimeError("injected")

        monkeypatch.setattr(recovery_intent, "_stage_hook", fail_before_commit)
        with pytest.raises(RuntimeError, match="injected"):
            recovery_intent.stage_pending(service.store, "restore-1", tmp_path, budget)
        with service.store._connect() as db:
            assert db.execute(
                "SELECT state FROM artifact_recovery_intent"
            ).fetchone() == ("pending",)
            assert db.execute(
                "SELECT COUNT(*) FROM artifact_recovery_stage"
            ).fetchone() == (0,)
        barrier = threading.Barrier(2)

        def rendezvous(phase):
            if phase == "verified":
                barrier.wait(timeout=20)

        monkeypatch.setattr(recovery_intent, "_stage_hook", rendezvous)

        def compete(_):
            try:
                return recovery_intent.stage_pending(
                    service.store, "restore-1", tmp_path, budget
                )
            except ValueError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(compete, range(2)))
        assert sorted(outcomes) == ["conflict", "staged"]
        monkeypatch.setattr(recovery_intent, "_stage_hook", lambda _: None)
        assert (
            recovery_intent.stage_pending(service.store, "restore-1", tmp_path, budget)
            == "staged"
        )
        with service.store._connect() as db:
            stage_row = db.execute(
                "SELECT relative_path,sha256,mime,byte_count,width,height FROM artifact_recovery_stage WHERE recovery_id='restore-1'"
            ).fetchone()
            assert stage_row[1] == digest and stage_row[2] == mime
            assert stage_row[4:] == (5, 5)
            assert (tmp_path / stage_row[0]).is_file()
            assert db.execute(
                "SELECT state FROM artifact_recovery_intent"
            ).fetchone() == ("staged",)
            for table in tables:
                if table not in {"artifact_recovery_intent", "artifact_recovery_stage"}:
                    assert (
                        db.execute(f"SELECT * FROM {table}").fetchall() == before[table]
                    )
        assert not path.exists() and len(sends) == 1
        report = artifact_reader.read_artifact(
            service.store,
            snap.task_id,
            "form",
            snap.revision,
            attempt.attempt_id,
            "artifact",
            tmp_path,
            budget,
        )
        assert (
            report["file"]["state"] == "missing"
            and report["artifact_reference"] is None
        )
        stage_child_code = (
            code
            + "\nfrom ecommerce_product_image_creator.recovery_intent import stage_pending\nprint(stage_pending(Store(Path(sys.argv[1])), 'restore-1', Path(sys.argv[6]), ResourceBudget(100000,10000,100000)))\n"
        )
        stage_child = subprocess.run(
            [
                sys.executable,
                "-c",
                stage_child_code,
                str(service.store.path),
                snap.task_id,
                attempt.attempt_id,
                snap.execution_digest,
                str(resolution.observation_id),
                str(tmp_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert stage_child.stdout.splitlines()[-1] == "staged"
        if mode in {"valid_jpeg", "valid_jpeg_observed"}:
            with service.store._connect() as db:
                prior_source = db.execute(
                    "SELECT * FROM prepared_mock_source WHERE task_id=? AND revision=?",
                    (snap.task_id, mock["revision"]),
                ).fetchone()
                db.execute(
                    "DELETE FROM prepared_mock_source WHERE task_id=? AND revision=?",
                    (snap.task_id, mock["revision"]),
                )
            with pytest.raises(ValueError, match="source_changed"):
                recovery_intent.publish_staged(
                    service.store, "restore-1", tmp_path, budget
                )
            assert not path.exists()
            with service.store._connect() as db:
                db.execute(
                    "INSERT INTO prepared_mock_source VALUES (?,?,?,?,?,?)",
                    prior_source,
                )
            with (
                (tmp_path / stage_row[0]).open("r+b"),
                pytest.raises(ValueError, match="stage_file_unavailable"),
            ):
                recovery_intent.publish_staged(
                    service.store, "restore-1", tmp_path, budget
                )
            assert not path.exists()
            moved = tmp_path.with_name(tmp_path.name + "-moved")

            def attempt_directory_replacement(phase):
                if phase == "before_link":
                    try:
                        tmp_path.rename(moved)
                    except OSError:
                        raise RuntimeError("directory_locked") from None
                    else:
                        moved.rename(tmp_path)
                        raise AssertionError("directory replacement succeeded")

            monkeypatch.setattr(
                recovery_intent, "_publication_hook", attempt_directory_replacement
            )
            with pytest.raises(RuntimeError, match="directory_locked"):
                recovery_intent.publish_staged(
                    service.store, "restore-1", tmp_path, budget
                )
            monkeypatch.setattr(recovery_intent, "_publication_hook", lambda _: None)
            assert not path.exists()
            other_drive = Path("C:/" if tmp_path.drive.upper() != "C:" else "D:/")
            with pytest.raises(ValueError, match="target_path_unverified"):
                recovery_intent.publish_staged(
                    service.store, "restore-1", other_drive, budget
                )
            assert not path.exists()
            publish_code = """
import os, sys
from pathlib import Path
from ecommerce_product_image_creator import original_query, preparation, recovery, recovery_intent
from ecommerce_product_image_creator.storage import Store, ResourceBudget
original_query._verify_original_binding=lambda db,i: i.attempt_id==sys.argv[3]
recovery._verify_observation_source=lambda i,row: row[6]==i.attempt_id
preparation._review_is_trusted=lambda source,task,review,digest,evidence: source=='fixture' and review=='review'
def crash(phase):
 if phase==sys.argv[7]: os._exit(84)
recovery_intent._publication_hook=crash
recovery_intent.publish_staged(Store(Path(sys.argv[1])),'restore-1',Path(sys.argv[6]),ResourceBudget(100000,10000,100000))
"""

            def crash_publish(phase):
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        publish_code,
                        str(service.store.path),
                        snap.task_id,
                        attempt.attempt_id,
                        snap.execution_digest,
                        str(resolution.observation_id),
                        str(tmp_path),
                        phase,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            assert crash_publish("before_link").returncode == 84
            assert not path.exists()
            path.write_bytes(b"not a valid image")
            failing_call = "recovery_intent.publish_staged(Store(Path(sys.argv[1])),'restore-1',Path(sys.argv[6]),ResourceBudget(100000,10000,100000))"
            failure_code = publish_code.replace(
                failing_call,
                "try:\n " + failing_call + "\nexcept ValueError:\n os._exit(86)",
            )
            failure = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    failure_code,
                    str(service.store.path),
                    snap.task_id,
                    attempt.attempt_id,
                    snap.execution_digest,
                    str(resolution.observation_id),
                    str(tmp_path),
                    "none",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            assert (
                failure.returncode == 86 and path.read_bytes() == b"not a valid image"
            )
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT count(*) FROM artifact_recovery_publication"
                ).fetchone() == (0,)
            with pytest.raises(ValueError, match="target_conflict"):
                recovery_intent.publish_staged(
                    service.store, "restore-1", tmp_path, budget
                )
            assert path.read_bytes() == b"not a valid image"
            path.unlink()
            Image.new("RGB", (5, 5), "red").save(path)
            with pytest.raises(ValueError, match="target_conflict"):
                recovery_intent.publish_staged(
                    service.store, "restore-1", tmp_path, budget
                )
            assert path.read_bytes() != (tmp_path / stage_row[0]).read_bytes()
            path.unlink()
            path.write_bytes((tmp_path / stage_row[0]).read_bytes())
            assert (
                recovery_intent.publish_staged(
                    service.store, "restore-1", tmp_path, budget
                )
                == "pending_reconciliation"
            )
            with pytest.raises(ValueError, match="file_identity_or_digest_conflict"):
                recovery_intent.reconcile_published(
                    service.store, "restore-1", tmp_path, budget
                )
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT count(*) FROM artifact_recovery_reconciliation"
                ).fetchone() == (0,)
            path.unlink()
            if mode == "valid_jpeg_observed":
                assert crash_publish("after_link").returncode == 84
                assert (
                    path.is_file()
                    and hashlib.sha256(path.read_bytes()).hexdigest() == digest
                )
                with service.store._connect() as db:
                    assert db.execute(
                        "SELECT count(*) FROM artifact_recovery_publication"
                    ).fetchone() == (0,)

                def revoke_after_link(phase):
                    if phase == "after_link":
                        with service.store._connect() as db:
                            db.execute(
                                "UPDATE form_review_detail SET decision='reject' WHERE review_id='review'"
                            )

                monkeypatch.setattr(
                    recovery_intent, "_publication_hook", revoke_after_link
                )
                with pytest.raises(
                    ValueError, match="source_changed_after_publication"
                ):
                    recovery_intent.publish_staged(
                        service.store, "restore-1", tmp_path, budget
                    )
                assert (
                    path.exists()
                    and hashlib.sha256(path.read_bytes()).hexdigest() == digest
                )
                with service.store._connect() as db:
                    assert db.execute(
                        "SELECT count(*) FROM artifact_recovery_publication"
                    ).fetchone() == (0,)
                    db.execute(
                        "UPDATE form_review_detail SET decision='accept' WHERE review_id='review'"
                    )
                monkeypatch.setattr(
                    recovery_intent, "_publication_hook", lambda _: None
                )
                assert (
                    recovery_intent.publish_staged(
                        service.store, "restore-1", tmp_path, budget
                    )
                    == "pending_reconciliation"
                )
                reconcile_code = publish_code.replace(
                    "recovery_intent._publication_hook=crash",
                    "recovery_intent._reconciliation_hook=crash",
                ).replace(
                    "recovery_intent.publish_staged(",
                    "recovery_intent.reconcile_published(",
                )

                def crash_reconcile(phase):
                    return subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            reconcile_code,
                            str(service.store.path),
                            snap.task_id,
                            attempt.attempt_id,
                            snap.execution_digest,
                            str(resolution.observation_id),
                            str(tmp_path),
                            phase,
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )

                assert crash_reconcile("during_transaction").returncode == 84
                with service.store._connect() as db:
                    assert db.execute(
                        "SELECT count(*) FROM artifact_recovery_reconciliation"
                    ).fetchone() == (0,)
                    assert db.execute(
                        "SELECT count(*) FROM artifact_recovery_link_observation"
                    ).fetchone() == (0,)
                assert crash_reconcile("after_commit").returncode == 84
                assert (
                    recovery_intent.reconcile_published(
                        service.store, "restore-1", tmp_path, budget
                    )
                    == "reconciled"
                )
                assert (
                    recovery_intent.reconcile_published(
                        service.store, "restore-1", tmp_path, budget
                    )
                    == "reconciled"
                )
                with service.store._connect() as db:
                    assert db.execute(
                        "SELECT count(*) FROM artifact_recovery_publication"
                    ).fetchone() == (0,)
                    assert db.execute(
                        "SELECT reason FROM artifact_recovery_link_observation"
                    ).fetchone() == ("matching_ntfs_link_observed",)
                    assert db.execute(
                        "SELECT basis FROM artifact_recovery_reconciliation"
                    ).fetchone() == ("observed_link",)
                    for table in tables:
                        if table not in {
                            "artifact_recovery_intent",
                            "artifact_recovery_stage",
                            "artifact_recovery_publication",
                            "artifact_recovery_link_observation",
                            "artifact_recovery_reconciliation",
                        }:
                            assert (
                                db.execute(f"SELECT * FROM {table}").fetchall()
                                == before[table]
                            )
                assert (
                    artifact_reader.read_artifact(
                        service.store,
                        snap.task_id,
                        "form",
                        snap.revision,
                        attempt.attempt_id,
                        "artifact",
                        tmp_path,
                        budget,
                    )["file"]["state"]
                    == "verified"
                )
                assert len(sends) == 1
                return
            barrier = threading.Barrier(2)

            def pause_before_link(phase):
                if phase == "before_link":
                    barrier.wait(timeout=20)

            monkeypatch.setattr(recovery_intent, "_publication_hook", pause_before_link)

            def publish_concurrently(_):
                try:
                    return recovery_intent.publish_staged(
                        service.store, "restore-1", tmp_path, budget
                    )
                except ValueError:
                    return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                publication_outcomes = list(pool.map(publish_concurrently, range(2)))
            assert publication_outcomes.count("published") == 1
            assert set(publication_outcomes) <= {
                "published",
                "conflict",
                "pending_reconciliation",
            }
            monkeypatch.setattr(recovery_intent, "_publication_hook", lambda _: None)
            assert (
                recovery_intent.publish_staged(
                    service.store, "restore-1", tmp_path, budget
                )
                == "published"
            )
            assert crash_publish("after_commit").returncode == 84
            first_barrier = threading.Barrier(2)

            def synchronize_first_reconciliation(phase):
                if phase == "before_commit":
                    first_barrier.wait(timeout=20)

            monkeypatch.setattr(
                recovery_intent,
                "_reconciliation_hook",
                synchronize_first_reconciliation,
            )

            def reconcile_first(_):
                try:
                    return recovery_intent.reconcile_published(
                        service.store, "restore-1", tmp_path, budget
                    )
                except ValueError as error:
                    return str(error)

            with ThreadPoolExecutor(max_workers=2) as pool:
                first_outcomes = list(pool.map(reconcile_first, range(2)))
            assert "reconciled" in first_outcomes
            assert all(
                value in {"reconciled", "source_changed"} for value in first_outcomes
            )
            monkeypatch.setattr(recovery_intent, "_reconciliation_hook", lambda _: None)
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT count(*) FROM artifact_recovery_reconciliation"
                ).fetchone() == (1,)
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT count(*) FROM artifact_recovery_publication"
                ).fetchone() == (1,)
                for table in tables:
                    if table not in {
                        "artifact_recovery_intent",
                        "artifact_recovery_stage",
                        "artifact_recovery_publication",
                        "artifact_recovery_link_observation",
                        "artifact_recovery_reconciliation",
                    }:
                        assert (
                            db.execute(f"SELECT * FROM {table}").fetchall()
                            == before[table]
                        )
            with service.store._connect() as db:
                with pytest.raises(
                    sqlite3.IntegrityError, match="immutable publication"
                ):
                    db.execute(
                        "UPDATE artifact_recovery_publication SET sha256='changed' WHERE recovery_id='restore-1'"
                    )
                with pytest.raises(
                    sqlite3.IntegrityError, match="immutable reconciliation"
                ):
                    db.execute(
                        "DELETE FROM artifact_recovery_reconciliation WHERE recovery_id='restore-1'"
                    )
            reconciliation_barrier = threading.Barrier(2)

            def pause_reconciliation(phase):
                if phase == "before_commit":
                    reconciliation_barrier.wait(timeout=20)

            monkeypatch.setattr(
                recovery_intent, "_reconciliation_hook", pause_reconciliation
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(
                    pool.map(
                        lambda _: recovery_intent.reconcile_published(
                            service.store, "restore-1", tmp_path, budget
                        ),
                        range(2),
                    )
                )
            assert outcomes == ["reconciled", "reconciled"]
            monkeypatch.setattr(recovery_intent, "_reconciliation_hook", lambda _: None)
            with pytest.raises(OSError):
                (tmp_path / stage_row[0]).write_bytes(b"changed")
            assert (
                hashlib.sha256(path.read_bytes()).hexdigest() == digest
                and len(sends) == 1
            )
            os.chmod(path, stat.S_IWRITE)
            path.write_bytes(b"corrupt after reconciliation")
            assert (
                artifact_reader.read_artifact(
                    service.store,
                    snap.task_id,
                    "form",
                    snap.revision,
                    attempt.attempt_id,
                    "artifact",
                    tmp_path,
                    budget,
                )["file"]["state"]
                != "verified"
            )
            with pytest.raises(ValueError):
                recovery_intent.reconcile_published(
                    service.store, "restore-1", tmp_path, budget
                )
            with service.store._connect() as db:
                assert db.execute(
                    "SELECT count(*) FROM artifact_recovery_reconciliation"
                ).fetchone() == (1,)
            path.unlink()
            assert (
                artifact_reader.read_artifact(
                    service.store,
                    snap.task_id,
                    "form",
                    snap.revision,
                    attempt.attempt_id,
                    "artifact",
                    tmp_path,
                    budget,
                )["file"]["state"]
                == "missing"
            )
            with pytest.raises(ValueError):
                recovery_intent.reconcile_published(
                    service.store, "restore-1", tmp_path, budget
                )
            return
        staged_path = tmp_path / stage_row[0]
        displaced = tmp_path / "displaced-stage"
        content = staged_path.read_bytes()
        staged_path.rename(displaced)
        staged_path.write_bytes(content)
        with pytest.raises(ValueError, match="stage_file_changed"):
            recovery_intent.publish_staged(service.store, "restore-1", tmp_path, budget)
        assert not path.exists()
        staged_path.unlink()
        displaced.rename(staged_path)
        (tmp_path / stage_row[0]).write_bytes(b"corrupt")
        with pytest.raises(ValueError):
            recovery_intent.stage_pending(service.store, "restore-1", tmp_path, budget)
        with pytest.raises(ValueError):
            recovery_intent.publish_staged(service.store, "restore-1", tmp_path, budget)
        assert not path.exists()
        with service.store._connect() as db:
            db.execute(
                "UPDATE form_review_detail SET decision='reject' WHERE review_id='review'"
            )
        with pytest.raises(ValueError):
            artifact_locator.acquire_original_bytes(
                service.store,
                "op",
                "a",
                snap.task_id,
                snap.revision,
                "form",
                attempt.attempt_id,
                snap.execution_digest,
                resolution.observation_id,
                artifact_id="artifact",
                artifact_root=tmp_path,
                budget=budget,
            )
        with pytest.raises(ValueError):
            recovery_intent.register_pending(
                service.store,
                "restore-1",
                "op",
                "a",
                snap.task_id,
                snap.revision,
                "form",
                attempt.attempt_id,
                snap.execution_digest,
                resolution.observation_id,
                artifact_id="artifact",
                artifact_root=tmp_path,
                budget=budget,
            )
    if mode in {"remote_candidate", "remote_conflict"}:
        path.write_bytes(b"damaged")
        with pytest.raises(ValueError, match="target_not_missing"):
            recovery_intent.register_pending(
                service.store,
                "remote-restore",
                "op",
                "a",
                snap.task_id,
                snap.revision,
                "form",
                attempt.attempt_id,
                snap.execution_digest,
                resolution.observation_id,
                artifact_id="artifact",
                artifact_root=tmp_path,
                budget=budget,
            )
        path.unlink()
        pending = recovery_intent.register_pending(
            service.store,
            "remote-restore",
            "op",
            "a",
            snap.task_id,
            snap.revision,
            "form",
            attempt.attempt_id,
            snap.execution_digest,
            resolution.observation_id,
            artifact_id="artifact",
            artifact_root=tmp_path,
            budget=budget,
        )
        assert pending == "remote-restore" and not path.exists() and len(sends) == 1
        remote_crash_code = """
import os, sqlite3, sys
from pathlib import Path
from ecommerce_product_image_creator import artifact_locator, original_query, recovery, recovery_intent
from ecommerce_product_image_creator.storage import Store, ResourceBudget
original_query._verify_original_binding=lambda db,i: i.attempt_id==sys.argv[3]
recovery._verify_observation_source=lambda i,row: row[6]==i.attempt_id
artifact_locator._verify_locator_source=lambda i,e: i.attempt_id==e.attempt_id and e.scope=='fixture-account'
artifact_locator._verify_fixture_access=lambda e: e.scope=='fixture-account'
artifact_locator._verify_fixture_content=lambda e,data: e.artifact_identity=='image-1'
def fetch(e,budget):
 c=sqlite3.connect(sys.argv[1]); data=c.execute('SELECT data FROM prepared_reference WHERE task_id=? AND revision=? AND position=0',(sys.argv[2],int(sys.argv[4]))).fetchone()[0]; c.close(); return data,'image/png'
artifact_locator._fetch_fixture=fetch
def crash(phase):
 if phase=='after_commit': os._exit(83)
recovery_intent._stage_hook=crash
recovery_intent.stage_pending(Store(Path(sys.argv[1])),'remote-restore',Path(sys.argv[5]),ResourceBudget(100000,10000,100000))
"""
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                remote_crash_code,
                str(service.store.path),
                snap.task_id,
                attempt.attempt_id,
                str(mock["revision"]),
                str(tmp_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert crashed.returncode == 83
        with service.store._connect() as db:
            assert db.execute(
                "SELECT state FROM artifact_recovery_intent"
            ).fetchone() == ("staged",)
            assert db.execute(
                "SELECT COUNT(*) FROM artifact_recovery_stage"
            ).fetchone() == (1,)
        assert not path.exists() and len(sends) == 1
        if mode == "remote_conflict":
            original_locator = artifact_locator.read_locator(service.store, "locator")
            assert original_locator is not None
            artifact_locator.register(
                service.store,
                replace(
                    original_locator, locator_id="other-locator", handle="other-handle"
                ),
            )
            with pytest.raises(ValueError, match="source_changed"):
                recovery_intent.publish_staged(
                    service.store, "remote-restore", tmp_path, budget
                )
            assert not path.exists()
            return
        publication_crash_code = remote_crash_code.replace(
            "recovery_intent._stage_hook=crash",
            "recovery_intent._publication_hook=crash",
        ).replace("recovery_intent.stage_pending(", "recovery_intent.publish_staged(")
        publication_crash = subprocess.run(
            [
                sys.executable,
                "-c",
                publication_crash_code,
                str(service.store.path),
                snap.task_id,
                attempt.attempt_id,
                str(mock["revision"]),
                str(tmp_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert publication_crash.returncode == 83
        with service.store._connect() as db:
            assert db.execute(
                "SELECT count(*) FROM artifact_recovery_publication"
            ).fetchone() == (1,)
        assert (
            recovery_intent.publish_staged(
                service.store, "remote-restore", tmp_path, budget
            )
            == "published"
        )
        assert (
            hashlib.sha256(path.read_bytes()).hexdigest() == digest and len(sends) == 1
        )
        now = time.time()
        monkeypatch.setattr(artifact_locator.time, "time", lambda: now + 1000)
        with pytest.raises(ValueError, match="current_policy_unverified"):
            recovery_intent.reconcile_published(
                service.store, "remote-restore", tmp_path, budget
            )
        monkeypatch.setattr(
            recovery_intent,
            "_verify_reconciliation_policy",
            lambda e, staged_at, published_at: (
                e.scope == "fixture-account"
                and e.created <= staged_at <= e.expires
                and published_at is not None
                and published_at <= e.expires
            ),
        )
        assert (
            recovery_intent.reconcile_published(
                service.store, "remote-restore", tmp_path, budget
            )
            == "reconciled"
        )
        with pytest.raises(ValueError):
            artifact_locator.acquire_original_bytes(
                service.store,
                "op",
                "a",
                snap.task_id,
                snap.revision,
                "form",
                attempt.attempt_id,
                snap.execution_digest,
                resolution.observation_id,
                artifact_id="artifact",
                artifact_root=tmp_path,
                budget=budget,
            )
        assert calls == ["locator", "locator", "locator"]
