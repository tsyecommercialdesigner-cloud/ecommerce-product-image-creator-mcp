"""Nonempty offline history; no provider access or linked generation."""

import dataclasses
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ecommerce_product_image_creator import (
    business_choices,
    mock_workflow,
    offline_submission,
    original_query,
    preparation,
    risk_reservation,
)
from ecommerce_product_image_creator.business_choices import BusinessChoice
from ecommerce_product_image_creator.domain import ExecutionSnapshot
from ecommerce_product_image_creator.execution_candidates import register_root_candidate
from ecommerce_product_image_creator.preparation import read_prepared
from ecommerce_product_image_creator.storage import Store
from ecommerce_product_image_creator.tasks import EligibilityError

from .test_mock_workflow import mock_fixture


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def setup_history(tmp_path, monkeypatch):
    from .authorized_query_fixture import install

    install(monkeypatch)

    def forbid_generation(_request):
        raise AssertionError("generation transport must not be called")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", forbid_generation)
    store, budget, form, mock, path, digest = mock_fixture(tmp_path, monkeypatch)
    task_id, revision = form["task_id"], mock["revision"]
    snap = ExecutionSnapshot.from_payload(
        read_prepared(store, task_id, revision)["snapshot_payload"]
    )
    form_snap = ExecutionSnapshot.from_payload(
        read_prepared(store, task_id, 1)["snapshot_payload"]
    )
    # Construct a historical successful Mock attempt in the isolated fixture.
    store.claim_operation(
        "historic-mock",
        task_id,
        revision,
        snap.execution_digest,
        "historic-mock-attempt",
    )
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded' WHERE attempt_id='historic-mock-attempt'"
        )
    mock_workflow.register_mock_artifact(
        store,
        task_id,
        revision,
        "historic-mock-attempt",
        "historic-mock-artifact",
        tmp_path,
        path,
        "image/png",
        digest,
        budget,
    )
    mock_workflow.save_mock_review(
        store,
        mock_workflow.MockReview(
            "historic-mock-review",
            task_id,
            revision,
            "historic-mock-artifact",
            digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    saved = read_prepared(store, task_id, revision)
    choice = BusinessChoice(
        "historical-choice",
        task_id,
        revision,
        saved["input_digest"],
        snap.execution_digest,
        "subset",
        {"suggested": saved["stage_references"], "selected": saved["stage_references"]},
        "fixture",
        time.time(),
    )
    business_choices.record_choice(store, choice)
    monkeypatch.setattr(
        business_choices,
        "_source_is_trusted",
        lambda c: c.choice_id == "historical-choice",
    )
    with store._connect() as db:
        db.execute("UPDATE task SET status='ready' WHERE task_id=?", (task_id,))
        for root, attempt, image_snap in [
            ("form-op", "form-attempt", form_snap),
            ("mock-op", "mock-attempt", snap),
        ]:
            db.execute(
                "INSERT INTO operation_attempt VALUES(?,?,?,?,?,?,?)",
                (
                    root,
                    1,
                    attempt,
                    None,
                    image_snap.model,
                    image_snap.endpoint,
                    image_snap.execution_digest,
                ),
            )
            db.execute(
                "INSERT INTO attempt_counter VALUES(?,?,?,?)",
                (root, image_snap.model, image_snap.endpoint, 1),
            )
        db.execute(
            "UPDATE attempt SET upstream_id='historical-upstream' WHERE attempt_id='form-attempt'"
        )
        db.execute(
            "INSERT INTO attempt_transport_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "mock-attempt",
                "mock-op",
                "mock-op",
                task_id,
                revision,
                snap.execution_digest,
                "entered",
                "dispatch_boundary",
                "server_pre_dispatch",
                time.time(),
            ),
        )
    register_root_candidate(store, "form-op", "form-candidate")
    register_root_candidate(store, "mock-op", "mock-candidate")
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.root == "form-op" and i.upstream_id == "historical-upstream",
    )
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    obs = original_query.query_once(
        store,
        "form-op",
        "form-candidate",
        task_id,
        1,
        "form-attempt",
        form_snap.execution_digest,
    )
    assert obs.state == "succeeded" and obs.observation_id is not None
    disclosure = risk_reservation.RiskDisclosure(
        "mock-op",
        "mock-candidate",
        "mock-attempt",
        snap.execution_digest,
        "linked-mock",
        snap.execution_digest,
        "unknown",
        True,
        True,
        True,
        "unknown",
        1,
    )
    evidence = risk_reservation.RiskEvidence(
        "risk-mock",
        "mock-op",
        "mock-candidate",
        "mock-attempt",
        snap.execution_digest,
        "linked-mock",
        snap.to_payload(),
        "notice",
        "acceptance",
        "execution",
        time.time() + 300,
        disclosure,
    )
    monkeypatch.setattr(
        risk_reservation,
        "_verify_risk_acceptance",
        lambda _db, e: e.risk_acceptance_reference == "acceptance",
    )
    monkeypatch.setattr(
        risk_reservation,
        "_verify_execution_authorization",
        lambda _db, e: e.execution_authorization_reference == "execution",
    )
    monkeypatch.setattr(
        risk_reservation,
        "_verify_revocation_source",
        lambda _db, auth, ref: auth == "risk-mock" and ref == "revoke",
    )
    return store, evidence, path, digest


def business_state(store):
    tables = (
        "task",
        "revision",
        "attempt",
        "operation",
        "operation_attempt",
        "attempt_counter",
        "attempt_transport_evidence",
        "execution_candidate",
        "prepared_execution",
        "prepared_reference",
        "prepared_mock_source",
        "artifact",
        "form_review",
        "form_review_detail",
        "form_review_invalidation",
        "mock_review",
        "business_choice",
        "business_choice_detail",
        "recovery_observation",
        "query_invocation",
        "query_billing_event",
        "query_usage_evidence",
    )
    with store._connect() as db:
        present = {
            x[0]
            for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            name: db.execute("SELECT * FROM " + name + " ORDER BY rowid").fetchall()
            for name in tables
            if name in present
        }


def test_nonempty_mock_history_survives_all_reservation_paths(
    tmp_path: Path, monkeypatch
):
    store, evidence, path, digest = setup_history(tmp_path, monkeypatch)
    before = business_state(store)
    for name in (
        "form_review",
        "form_review_detail",
        "mock_review",
        "artifact",
        "business_choice",
        "recovery_observation",
    ):
        assert before[name], name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert evidence.disclosure is not None
    for bad in (
        dataclasses.replace(evidence, source_root="other-root"),
        dataclasses.replace(evidence, source_candidate="form-candidate"),
        dataclasses.replace(
            evidence,
            source_attempt="form-attempt",
            disclosure=dataclasses.replace(
                evidence.disclosure, source_attempt="form-attempt"
            ),
        ),
        dataclasses.replace(
            evidence,
            target_operation="mock-op",
            disclosure=dataclasses.replace(
                evidence.disclosure, target_operation="mock-op"
            ),
        ),
    ):
        with pytest.raises(ValueError):
            risk_reservation.reserve(store, bad)
        assert business_state(store) == before
    with store._connect() as db:
        risk_reservation._schema(db)
        db.execute(
            "CREATE TRIGGER fail_risk_history BEFORE INSERT ON risk_reservation WHEN NEW.authorization_id='risk-failure' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        risk_reservation.reserve(
            store, dataclasses.replace(evidence, authorization_id="risk-failure")
        )
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM risk_reservation").fetchone() == (0,)
    assert business_state(store) == before
    assert risk_reservation.reserve(store, evidence).can_send is False
    assert business_state(store) == before
    # A reservation is not an operation or a candidate root and cannot be a parent.
    with pytest.raises(ValueError):
        risk_reservation.reserve(
            store,
            dataclasses.replace(
                evidence, source_root="linked-mock", authorization_id="child"
            ),
        )
    assert business_state(store) == before
    assert risk_reservation.reserve(store, evidence).can_send is False
    assert business_state(store) == before
    assert evidence.disclosure is not None
    invalid = dataclasses.replace(
        evidence,
        disclosure=dataclasses.replace(evidence.disclosure, snapshot_digest="wrong"),
    )
    with pytest.raises(ValueError, match="risk_disclosure_mismatch"):
        risk_reservation.reserve(store, invalid)
    assert business_state(store) == before
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: False)
    with pytest.raises(EligibilityError):
        risk_reservation.reserve(store, evidence)
    assert business_state(store) == before
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    assert evidence.disclosure is not None
    second = dataclasses.replace(
        evidence,
        authorization_id="risk-other",
        target_operation="linked-other",
        disclosure=dataclasses.replace(
            evidence.disclosure, target_operation="linked-other"
        ),
    )

    def compete(e):
        try:
            return risk_reservation.reserve(store, e).operation_id
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(compete, [evidence, second]))
    assert result == ["linked-mock", "reservation_conflict"]
    assert business_state(store) == before
    risk_reservation.revoke(store, "risk-mock", "revoke")
    risk_reservation.revoke(store, "risk-mock", "revoke")
    with pytest.raises(ValueError, match="authorization_revoked"):
        risk_reservation.reserve(store, evidence)
    assert business_state(store) == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    code = """
import json, socket, sys
from pathlib import Path
from ecommerce_product_image_creator import risk_reservation as r
from ecommerce_product_image_creator.storage import Store
def deny(*a, **k): raise AssertionError("network forbidden")
socket.socket.connect=deny
socket.create_connection=deny
data=json.loads(sys.argv[2])
data["disclosure"]=r.RiskDisclosure(**data["disclosure"])
r._verify_risk_acceptance=lambda *a: True
r._verify_execution_authorization=lambda *a: True
try:
    r.reserve(Store(Path(sys.argv[1])), r.RiskEvidence(**data))
except ValueError as error:
    print(str(error))
else:
    raise AssertionError("revoked authorization accepted")
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(store.path),
            json.dumps(dataclasses.asdict(evidence)),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "authorization_revoked"


def test_nonempty_pre_risk_schema_upgrade(tmp_path: Path, monkeypatch):
    store, evidence, path, digest = setup_history(tmp_path, monkeypatch)
    before = business_state(store)
    for name in (
        "task",
        "revision",
        "attempt",
        "operation",
        "execution_candidate",
        "artifact",
        "recovery_observation",
        "attempt_counter",
        "form_review",
        "mock_review",
    ):
        assert before[name], name
    legacy_path = tmp_path / "history-legacy.sqlite"
    with store._connect() as src, sqlite3.connect(legacy_path) as dst:
        assert (
            src.execute(
                "SELECT 1 FROM sqlite_master WHERE name='risk_reservation'"
            ).fetchone()
            is None
        )
        src.backup(dst)
    legacy = Store(legacy_path)
    assert business_state(legacy) == before
    with legacy._connect() as db:
        risk_reservation._schema(db)
        risk_reservation._schema(db)
        assert db.execute("SELECT count(*) FROM risk_reservation").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM risk_reservation_revocation"
        ).fetchone() == (0,)
    assert business_state(legacy) == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert risk_reservation.reserve(legacy, evidence).can_send is False
    with pytest.raises(ValueError, match="reservation_conflict"):
        assert evidence.disclosure is not None
        risk_reservation.reserve(
            legacy,
            dataclasses.replace(
                evidence,
                authorization_id="other",
                target_operation="other-op",
                disclosure=dataclasses.replace(
                    evidence.disclosure, target_operation="other-op"
                ),
            ),
        )
    assert business_state(legacy) == before


def test_invalidated_form_review_rejects_without_business_mutation(
    tmp_path: Path, monkeypatch
):
    store, evidence, _, _ = setup_history(tmp_path, monkeypatch)
    with store._connect() as db:
        db.execute(
            "INSERT INTO form_review_invalidation(review_id) VALUES('form-review')"
        )
    before = business_state(store)
    assert before["form_review_invalidation"]
    with pytest.raises(EligibilityError):
        risk_reservation.reserve(store, evidence)
    assert business_state(store) == before
