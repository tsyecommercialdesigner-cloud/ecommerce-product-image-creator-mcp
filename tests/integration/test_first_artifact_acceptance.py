"""T047/P1: first delivery across business identities and reserved cost."""

import hashlib
import socket
import time
from io import BytesIO

import pytest
from PIL import Image

from ecommerce_product_image_creator import (
    artifact_locator,
    first_artifact,
    offline_submission,
    original_query,
    preparation,
    recovery,
    retry_scope,
)
from ecommerce_product_image_creator.artifact_locator import LocatorEvidence
from ecommerce_product_image_creator.execution_candidates import register_root_candidate
from ecommerce_product_image_creator.preparation import prepare_mock, read_prepared
from ecommerce_product_image_creator.review import (
    FormReview,
    invalidate_review,
    save_review,
)
from ecommerce_product_image_creator.task_view import ReadOnlyStore, read_task_view

from .authorized_query_fixture import install
from .test_multiview_completion import prepare_eligibility
from .test_offline_submission import setup_context
from .test_retry_scopes import make_scopes
from .test_task_revision_roots import grant_fixture


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def trusted_locator(
    monkeypatch, service, snapshot, attempt, root, candidate, locator_id
):
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.upstream_id in {"up-1", "up-2", "second-upstream"},
    )
    monkeypatch.setattr(
        recovery,
        "_verify_observation_source",
        lambda i, row: row[6] == i.attempt_id,
    )
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    monkeypatch.setattr(
        artifact_locator,
        "_verify_locator_source",
        lambda i, e: i.attempt_id == e.attempt_id and i.upstream_id == e.upstream_id,
    )
    install(monkeypatch)
    identity = (
        root,
        candidate,
        snapshot.task_id,
        snapshot.revision,
        snapshot.stage,
        attempt.attempt_id,
        snapshot.execution_digest,
    )
    result = recovery.recover_once(service.store, *identity)
    assert result.state == "succeeded" and result.observation_id is not None
    with service.store._connect() as db:
        upstream_id = db.execute(
            "SELECT upstream_id FROM attempt WHERE attempt_id=?", (attempt.attempt_id,)
        ).fetchone()[0]
    locator = LocatorEvidence(
        locator_id,
        *identity,
        "candidate_gateway",
        snapshot.model,
        snapshot.endpoint,
        snapshot.group,
        "fixture-account",
        upstream_id,
        result.observation_id,
        "image-1",
        "fixture_handle",
        "isolated-source",
        locator_id,
        time.time(),
        time.time() + 1200,
    )
    artifact_locator.register(service.store, locator)
    return locator


def install_bytes(monkeypatch, data):
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(
        first_artifact,
        "_verify_digest_source",
        lambda _, value, mime: value == digest and mime == "image/png",
    )
    monkeypatch.setattr(artifact_locator, "_verify_fixture_access", lambda _: True)
    monkeypatch.setattr(
        artifact_locator, "_verify_fixture_content", lambda _, value: value == data
    )
    monkeypatch.setattr(
        artifact_locator, "_fetch_fixture", lambda *_: (data, "image/png")
    )
    return digest


def png_bytes():
    out = BytesIO()
    Image.new("RGB", (5, 5), "blue").save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize(
    "invalidate_form_review",
    ["none", "before_anchor", "before_stage", "before_publish"],
)
def test_same_bytes_form_and_mock_remain_distinct(
    tmp_path, monkeypatch, invalidate_form_review
):
    service, form_args = setup_context(tmp_path)
    store, budget = service.store, service.budget
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: (
            calls.append(1)
            or offline_submission.ProviderResult(
                "succeeded", task_id=f"up-{len(calls)}"
            )
        ),
    )
    grant_fixture(service, form_args, monkeypatch)
    form_attempt = offline_submission.submit_offline(
        service, *form_args, require_first_authorizations=True
    )
    register_root_candidate(store, "op", "form-candidate")
    form_locator = trusted_locator(
        monkeypatch,
        service,
        form_args[4],
        form_attempt,
        "op",
        "form-candidate",
        "form-locator",
    )
    data = png_bytes()
    digest = install_bytes(monkeypatch, data)
    form_path = tmp_path / "delivered-form.png"
    store.register_first_artifact(
        "form-delivery",
        "form-artifact",
        form_locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        form_path,
    )
    store.stage_first_artifact("form-delivery", tmp_path, budget)
    assert (
        store.reconcile_first_artifact("form-delivery", tmp_path, budget) == "delivered"
    )
    save_review(
        store,
        FormReview(
            "form-review-p1",
            form_args[0].task_id,
            1,
            "form-artifact",
            digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    mock = prepare_mock(
        store,
        tmp_path,
        budget,
        form_args[0].task_id,
        "form-artifact",
        "form-review-p1",
        [
            {
                "ref_id": "artwork",
                "category": "artwork",
                "path": str(tmp_path / "artwork.png"),
                "mime": "image/png",
            }
        ],
        "mock F L R B T Q",
        form_args[4].model,
        form_args[4].endpoint,
        form_args[4].group,
    )
    (tmp_path / "form-artifact.png").write_bytes(data)
    mock_service, mock_args = prepare_eligibility(
        store, tmp_path, budget, form_args[0].task_id, mock["revision"], "mock-op-p1"
    )
    grant_fixture(mock_service, mock_args, monkeypatch)
    mock_attempt = offline_submission.submit_offline(
        mock_service, *mock_args, require_first_authorizations=True
    )
    register_root_candidate(store, "mock-op-p1", "mock-candidate")
    mock_locator = trusted_locator(
        monkeypatch,
        mock_service,
        mock_args[4],
        mock_attempt,
        "mock-op-p1",
        "mock-candidate",
        "mock-locator",
    )
    assert (
        mock_attempt.attempt_id != form_attempt.attempt_id
        and mock_args[4].stage == "mock"
    )
    saved = read_prepared(store, form_args[0].task_id, mock["revision"])
    assert saved["stage_references"] == ["form-artifact", "artwork"]
    assert (
        saved["form_source"]["artifact_id"] == "form-artifact"
        and saved["form_source"]["form_revision"] == 1
    )
    with pytest.raises(ValueError, match="first_identity_conflict"):
        store.register_first_artifact(
            "form-delivery",
            "form-artifact",
            mock_locator.locator_id,
            digest,
            "image/png",
            tmp_path,
            form_path,
        )
    with pytest.raises(ValueError, match="first_identity_conflict"):
        store.register_first_artifact(
            "mock-delivery",
            "mock-artifact",
            mock_locator.locator_id,
            digest,
            "image/png",
            tmp_path,
            form_path,
        )
    mock_path = tmp_path / "delivered-mock.png"
    if invalidate_form_review == "before_anchor":
        invalidate_review(store, "form-review-p1", "fixture revoked")
        with pytest.raises(ValueError):
            store.register_first_artifact(
                "mock-delivery",
                "mock-artifact",
                mock_locator.locator_id,
                digest,
                "image/png",
                tmp_path,
                mock_path,
            )
        assert not mock_path.exists() and len(calls) == 2
        return
    store.register_first_artifact(
        "mock-delivery",
        "mock-artifact",
        mock_locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        mock_path,
    )
    if invalidate_form_review == "before_stage":
        invalidate_review(store, "form-review-p1", "fixture revoked")
        with pytest.raises(ValueError):
            store.stage_first_artifact("mock-delivery", tmp_path, budget)
        assert not mock_path.exists() and len(calls) == 2
        return
    store.stage_first_artifact("mock-delivery", tmp_path, budget)
    if invalidate_form_review == "before_publish":
        invalidate_review(store, "form-review-p1", "fixture revoked")
        with pytest.raises(ValueError):
            store.reconcile_first_artifact("mock-delivery", tmp_path, budget)
        assert not mock_path.exists() and len(calls) == 2
        return
    with store._connect() as db:
        stages = db.execute(
            "SELECT delivery_id,file_device,file_inode FROM first_artifact_stage ORDER BY delivery_id"
        ).fetchall()
        assert len(stages) == 2 and stages[0][1:] != stages[1][1:]
    assert store.stage_first_artifact("form-delivery", tmp_path, budget) == "staged"
    assert (
        store.reconcile_first_artifact("mock-delivery", tmp_path, budget) == "delivered"
    )
    assert form_path.read_bytes() == mock_path.read_bytes() == data
    with store._connect() as db:
        rows = db.execute(
            "SELECT artifact_id,attempt_id,sha256 FROM artifact ORDER BY artifact_id"
        ).fetchall()
        assert rows == [
            ("form-artifact", form_attempt.attempt_id, digest),
            ("mock-artifact", mock_attempt.attempt_id, digest),
        ]
        assert db.execute("SELECT count(*) FROM first_artifact_stage").fetchone() == (
            2,
        )
    view = read_task_view(
        ReadOnlyStore(store.path), form_args[0].task_id, tmp_path, budget
    )
    attempts = {a["attempt_id"]: a for a in view["attempts"]}
    assert (
        attempts[form_attempt.attempt_id]["artifacts"][0]["review"]["decision"]
        == "accept"
    )
    assert (
        attempts[mock_attempt.attempt_id]["artifacts"][0]["review"]["state"]
        == "pending"
    )
    assert len(calls) == 2


@pytest.mark.parametrize(
    "scenario", ["success", "stage_failure", "target_conflict", "db_rollback"]
)
def test_first_delivery_preserves_nonempty_retry_risk(tmp_path, monkeypatch, scenario):
    service, args, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    retry_scope.reserve_eligibility(
        service, *args, root="op", candidate_id="source", host_id="host", cost_id="cost"
    )
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: (
            calls.append(1)
            or offline_submission.ProviderResult("succeeded", task_id="second-upstream")
        ),
    )
    second = offline_submission.submit_offline(
        service, *args, retry_of="op", require_retry_scope=True
    )
    before = retry_scope.read_reservation(service.store, args[6])
    assert (
        before["reserved_minor"] == 20
        and before["actual_cost"] is None
        and before["attempt_id"] == second.attempt_id
    )
    with service.store._connect() as db:
        reservation_rows = db.execute(
            "SELECT * FROM retry_scope_reservation"
        ).fetchall()
        counter_rows = db.execute("SELECT * FROM attempt_counter").fetchall()
        upstream_fact = db.execute(
            "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
            (second.attempt_id,),
        ).fetchone()
        has_cost = bool(
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_cost_evidence'"
            ).fetchone()
        )
        cost_rows = (
            db.execute("SELECT * FROM generation_cost_evidence").fetchall()
            if has_cost
            else None
        )

    def assert_risk_unchanged():
        assert retry_scope.read_reservation(service.store, args[6]) == before
        with service.store._connect() as db:
            assert (
                db.execute("SELECT * FROM retry_scope_reservation").fetchall()
                == reservation_rows
            )
            assert (
                db.execute("SELECT * FROM attempt_counter").fetchall() == counter_rows
            )
            assert (
                db.execute(
                    "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
                    (second.attempt_id,),
                ).fetchone()
                == upstream_fact
            )
            has_now = bool(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_cost_evidence'"
                ).fetchone()
            )
            assert (
                db.execute("SELECT * FROM generation_cost_evidence").fetchall()
                if has_now
                else None
            ) == cost_rows
            assert db.execute(
                "SELECT count(*) FROM operation_attempt WHERE operation_id='op'"
            ).fetchone() == (2,)
        assert len(calls) == 2

    locator = trusted_locator(
        monkeypatch, service, args[4], second, "op", "source", "risk-locator"
    )
    data = png_bytes()
    digest = install_bytes(monkeypatch, data)
    target = tmp_path / "risk-delivery.png"
    service.store.register_first_artifact(
        "risk-delivery",
        "risk-artifact",
        locator.locator_id,
        digest,
        "image/png",
        tmp_path,
        target,
    )
    if scenario == "stage_failure":
        monkeypatch.setattr(
            artifact_locator, "_fetch_fixture", lambda *_: (b"bad", "image/png")
        )
        with pytest.raises(ValueError, match="first_content_unverified"):
            service.store.stage_first_artifact(
                "risk-delivery", tmp_path, service.budget
            )
        assert not target.exists()
        assert_risk_unchanged()
        return
    service.store.stage_first_artifact("risk-delivery", tmp_path, service.budget)
    if scenario == "target_conflict":
        target.write_bytes(b"other")
        with pytest.raises(ValueError):
            service.store.reconcile_first_artifact(
                "risk-delivery", tmp_path, service.budget
            )
        assert target.read_bytes() == b"other"
        assert_risk_unchanged()
        return
    if scenario == "db_rollback":

        def fail(phase):
            if phase == "before_commit":
                raise RuntimeError("fixture rollback")

        monkeypatch.setattr(first_artifact, "_hook", fail)
        with pytest.raises(RuntimeError, match="fixture rollback"):
            service.store.reconcile_first_artifact(
                "risk-delivery", tmp_path, service.budget
            )
        with service.store._connect() as db:
            assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert target.read_bytes() == data
        assert_risk_unchanged()
        monkeypatch.setattr(first_artifact, "_hook", lambda _: None)
    assert (
        service.store.reconcile_first_artifact(
            "risk-delivery", tmp_path, service.budget
        )
        == "delivered"
    )
    assert (
        service.store.reconcile_first_artifact(
            "risk-delivery", tmp_path, service.budget
        )
        == "delivered"
    )
    assert_risk_unchanged()
    target.unlink()
    assert (
        service.store.reconcile_delivered_artifact(
            second.attempt_id, "risk-artifact", tmp_path, service.budget
        )
        == "missing_or_corrupt"
    )
    assert_risk_unchanged()
    assert (
        service.store.reconcile_first_artifact(
            "risk-delivery", tmp_path, service.budget
        )
        == "delivered"
    )
    assert_risk_unchanged()
    assert first.attempt_id != second.attempt_id and len(calls) == 2
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE operation_id='op'"
        ).fetchone() == (2,)
