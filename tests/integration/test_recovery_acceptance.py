"""Offline acceptance of evaluation; fixture setup and evaluation are separate."""

import hashlib
import time

import pytest
from PIL import Image

from ecommerce_product_image_creator import (
    original_query,
    recovery,
    recovery_evaluation,
)
from ecommerce_product_image_creator.domain import UpstreamStatus
from ecommerce_product_image_creator.form_workflow import register_form_artifact
from ecommerce_product_image_creator.review import FormReview, save_review

from .test_original_query import setup_query


def make_form(tmp_path, monkeypatch, approved=False):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(
        recovery, "_verify_observation_source", lambda i, row: row[6] == i.attempt_id
    )
    queries = []

    def query(i):
        queries.append(i.upstream_id)
        return {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    snap = args[4]
    identity = (
        "op",
        "a",
        snap.task_id,
        1,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
    )
    assert (
        original_query.query_once(
            service.store,
            identity[0],
            identity[1],
            identity[2],
            identity[3],
            identity[5],
            identity[6],
        ).state
        == "succeeded"
    )
    service.store.record_upstream(
        attempt.attempt_id, UpstreamStatus.SUCCEEDED, "fixture-upstream"
    )
    path = tmp_path / "result.png"
    Image.new("RGB", (5, 5), "blue").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    register_form_artifact(
        service.store,
        snap.task_id,
        1,
        attempt.attempt_id,
        "artifact",
        tmp_path,
        path,
        "image/png",
        digest,
        service.budget,
    )
    if approved:
        save_review(
            service.store,
            FormReview(
                "review",
                snap.task_id,
                1,
                "artifact",
                digest,
                "accept",
                "fixture",
                time.time(),
                None,
            ),
        )
    return service, identity, sends, queries, path


def evaluate_unchanged(service, identity, sends, queries, root):
    with service.store._connect() as db:
        before = list(db.iterdump())
    before_sends, before_queries = len(sends), len(queries)
    result = recovery_evaluation.evaluate(
        service.store, *identity, artifact_root=root, budget=service.budget
    )
    with service.store._connect() as db:
        assert list(db.iterdump()) == before
    assert (len(sends), len(queries)) == (before_sends, before_queries)
    assert result["authorization"] == "not_granted"
    return result


@pytest.mark.parametrize("approved", [False, True])
def test_form_artifact_keeps_review_evidence(tmp_path, monkeypatch, approved):
    service, identity, sends, queries, path = make_form(tmp_path, monkeypatch, approved)
    result = evaluate_unchanged(service, identity, sends, queries, tmp_path)
    artifact = result["artifacts"][0]
    assert artifact["artifact_id"] == "artifact"
    assert artifact["delivery_status"] == "delivered"
    assert artifact["file"]["state"] == "verified"
    assert artifact["review"]["state"] == ("accept" if approved else "pending")
    assert artifact["review"]["trust_state"] == "unverified"
    assert result["reason"] == (
        "review_source_unverified" if approved else "artifact_requires_review"
    )
    if approved:
        path.unlink()
        missing = evaluate_unchanged(service, identity, sends, queries, tmp_path)
        assert missing["reason"] == "artifact_missing_or_invalid"
        assert missing["artifacts"][0]["file"]["state"] == "missing"
        assert missing["artifacts"][0]["review"]["state"] == "accept"
        path.write_bytes(b"bad")
        corrupt = evaluate_unchanged(service, identity, sends, queries, tmp_path)
        assert corrupt["artifacts"][0]["file"]["state"] == "invalid_or_unreadable"
        assert corrupt["artifacts"][0]["review"]["state"] == "accept"


def test_old_optional_evidence_tables_absent(tmp_path, monkeypatch):
    service, identity, sends, queries, _ = make_form(tmp_path, monkeypatch, True)
    with service.store._connect() as db:
        for name in (
            "attempt_timing_evidence",
            "generation_cost_evidence",
            "query_billing_event",
            "query_usage_evidence",
        ):
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone():
                db.execute(f"DROP TABLE {name}")
    result = evaluate_unchanged(service, identity, sends, queries, tmp_path)
    assert result["artifacts"][0]["review"]["state"] == "accept"
    assert result["generation_cost"]["billing_completeness"] == "unverified"
    assert result["query_cost"]["billing_completeness"] == "unverified"


def test_wal_evaluation_uses_single_snapshot_across_concurrent_observation(
    tmp_path, monkeypatch
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    with service.store._connect() as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(
        recovery, "_verify_observation_source", lambda i, row: row[6] == i.attempt_id
    )
    queries = []

    def query(i):
        queries.append(i.upstream_id)
        return {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    snap = args[4]
    identity = (
        "op",
        "a",
        snap.task_id,
        1,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
    )
    read_candidate = recovery_evaluation.read_candidate
    interleaved = []

    def after_snapshot(*values):
        candidate = read_candidate(*values)
        if not interleaved:
            interleaved.append(True)
            result = original_query.query_once(
                service.store,
                identity[0],
                identity[1],
                identity[2],
                identity[3],
                identity[5],
                identity[6],
            )
            assert result.state == "succeeded"
        return candidate

    monkeypatch.setattr(recovery_evaluation, "read_candidate", after_snapshot)
    first = recovery_evaluation.evaluate(
        service.store, *identity, artifact_root=tmp_path, budget=service.budget
    )
    assert first["recovery"]["state"] == "unknown"
    assert first["action"] == "query_original"
    second = evaluate_unchanged(service, identity, sends, queries, tmp_path)
    assert second["recovery"]["state"] == "succeeded"
    assert second["action"] == "assess_original_artifact"
    assert interleaved == [True] and queries == ["fixture-upstream"] and sends == [1]


from .test_linked_atomic_submission import linked_context  # noqa: F401


@pytest.mark.parametrize("linked_mode", ["reservation", "stopped", "unknown"])
def test_original_later_success_keeps_linked_history(
    linked_context,  # noqa: F811
    tmp_path,
    monkeypatch,
    linked_mode,
):
    from ecommerce_product_image_creator import offline_submission, risk_reservation

    from .authorized_query_fixture import install

    install(monkeypatch)

    service, args, source, sends = linked_context
    target = None
    if linked_mode == "stopped":
        monkeypatch.setattr(
            risk_reservation, "_verify_revocation_source", lambda *_: True
        )
        monkeypatch.setattr(
            offline_submission,
            "_after_intent_recorded",
            lambda: risk_reservation.revoke(service.store, "risk-1", "fixture revoke"),
        )
    if linked_mode != "reservation":
        target = offline_submission.submit_offline(
            service, *args, risk_authorization_id="risk-1"
        )
    # The fixture simulates a separately verified late ID; it does not claim a real Host.
    service.store.record_upstream(source.attempt_id, UpstreamStatus.UNKNOWN, "late-id")
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == source.attempt_id and i.upstream_id == "late-id",
    )
    monkeypatch.setattr(
        recovery, "_verify_observation_source", lambda i, row: row[6] == i.attempt_id
    )
    queries = []

    def query(i):
        queries.append(i.upstream_id)
        return {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    snap = args[4]
    identity = (
        "op",
        "source-candidate",
        snap.task_id,
        1,
        snap.stage,
        source.attempt_id,
        snap.execution_digest,
    )
    assert (
        original_query.query_once(
            service.store,
            identity[0],
            identity[1],
            identity[2],
            identity[3],
            identity[5],
            identity[6],
        ).state
        == "succeeded"
    )
    original = evaluate_unchanged(service, identity, sends, queries, tmp_path)
    assert original["recovery"]["state"] == "succeeded"
    assert original["action"] == "assess_original_artifact"
    assert original["historical_upstream_status"] == "unknown"
    if target:
        with service.store._connect() as db:
            candidate = db.execute(
                "SELECT linked_candidate FROM risk_consumption WHERE linked_attempt=?",
                (target.attempt_id,),
            ).fetchone()[0]
        other_identity = (
            "linked-op",
            candidate,
            snap.task_id,
            1,
            snap.stage,
            target.attempt_id,
            snap.execution_digest,
        )
        other = evaluate_unchanged(service, other_identity, sends, queries, tmp_path)
        assert other["linked_source"] == {"root": "op", "attempt_id": source.attempt_id}
        assert other["historical_upstream_status"] in (
            "unknown",
            "submission_uncertain",
        )
        assert other["recovery"]["state"] != "succeeded"
        assert len(original["linked_operations"]) == 1
        assert original["duplicate_generation_risk"]
        if linked_mode == "stopped":
            assert other["local_stop"] == "entered_then_stopped"
    else:
        assert original["linked_operations"] == []
    assert queries == ["late-id"]


def test_form_approval_does_not_approve_mock_or_old_revision(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import (
        mock_workflow,
        offline_submission,
        preparation,
    )
    from ecommerce_product_image_creator.execution_candidates import (
        register_root_candidate,
    )

    from .test_multiview_completion import prepare_eligibility

    service, original, sends, queries, form_path = make_form(
        tmp_path, monkeypatch, True
    )
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    mock = preparation.prepare_mock(
        service.store,
        tmp_path,
        service.budget,
        original[2],
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
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
    )
    (tmp_path / "form-result.png").write_bytes(form_path.read_bytes())
    # The existing eligibility helper uses this conventional filename for the form artifact.
    (tmp_path / "artifact.png").write_bytes(form_path.read_bytes())
    mock_service, args = prepare_eligibility(
        service.store,
        tmp_path,
        service.budget,
        original[2],
        mock["revision"],
        "mock-op",
    )
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def dispatch(_):
        sends.append(1)
        return offline_submission.ProviderResult("succeeded", task_id="mock-id")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", dispatch)
    attempt = offline_submission.submit_offline(mock_service, *args)
    candidate = register_root_candidate(service.store, "mock-op", "mock-candidate")
    mock_path = tmp_path / "mock-result.png"
    Image.new("RGB", (5, 5), "green").save(mock_path)
    digest = hashlib.sha256(mock_path.read_bytes()).hexdigest()
    mock_workflow.register_mock_artifact(
        service.store,
        original[2],
        mock["revision"],
        attempt.attempt_id,
        "mock-artifact",
        tmp_path,
        mock_path,
        "image/png",
        digest,
        service.budget,
    )
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(
        recovery, "_verify_observation_source", lambda i, row: row[6] == i.attempt_id
    )

    def query(i):
        queries.append(i.upstream_id)
        return {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    identity = (
        "mock-op",
        "mock-candidate",
        original[2],
        mock["revision"],
        "mock",
        attempt.attempt_id,
        candidate.snapshot.execution_digest,
    )
    assert (
        original_query.query_once(
            service.store,
            identity[0],
            identity[1],
            identity[2],
            identity[3],
            identity[5],
            identity[6],
        ).state
        == "succeeded"
    )
    result = evaluate_unchanged(mock_service, identity, sends, queries, tmp_path)
    assert result["artifacts"][0]["review"]["state"] == "pending"
    assert result["reason"] == "artifact_requires_review"
    old = evaluate_unchanged(service, original, sends, queries, tmp_path)
    assert old["reason"] == "historical_revision_requires_review"
    assert old["artifacts"][0]["review"]["state"] == "accept"


def test_old_schema_server_task_and_single_artifact_readonly(tmp_path, monkeypatch):
    import shutil

    from ecommerce_product_image_creator import server
    from ecommerce_product_image_creator.task_view import ReadOnlyStore

    root, inputs = tmp_path / "state", tmp_path / "input"
    root.mkdir()
    inputs.mkdir()
    service, identity, sends, queries, _ = make_form(root, monkeypatch, True)
    with service.store._connect() as db:
        for name in (
            "attempt_timing_evidence",
            "generation_cost_evidence",
            "query_billing_event",
            "query_usage_evidence",
        ):
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone():
                db.execute(f"DROP TABLE {name}")
    target = root / "tasks.sqlite"
    shutil.copyfile(service.store.path, target)
    monkeypatch.setenv("PRODUCT_IMAGE_STATE_DIR", str(root))
    monkeypatch.setenv("PRODUCT_IMAGE_INPUT_ROOT", str(inputs))
    monkeypatch.setenv("PRODUCT_IMAGE_MAX_INPUT_BYTES", "100000")
    monkeypatch.setenv("PRODUCT_IMAGE_MAX_PIXELS", "10000")
    monkeypatch.setenv("PRODUCT_IMAGE_MAX_DECODED_BYTES", "100000")
    with ReadOnlyStore(target)._connect() as db:
        before = list(db.iterdump())
    view = server.get_product_image_task(identity[2])
    item = server.get_product_image_task(
        identity[2], identity[5], "form", 1, "artifact"
    )
    assert view["task_id"] == identity[2]
    assert item["review"]["state"] == "accept" and item["file"]["state"] == "verified"
    with ReadOnlyStore(target)._connect() as db:
        assert list(db.iterdump()) == before
    assert len(sends) == 1 and len(queries) == 1


def test_evaluate_after_original_artifact_reconciled(tmp_path, monkeypatch):
    from ecommerce_product_image_creator.storage import ResourceBudget
    from ecommerce_product_image_creator.task_view import ReadOnlyStore

    from .test_local_artifact_copy import (
        test_missing_form_artifact_has_verified_mock_copy_without_mutation,
    )

    test_missing_form_artifact_has_verified_mock_copy_without_mutation(
        tmp_path, monkeypatch, "valid_jpeg_observed", "recover_from_local_bytes"
    )
    store = ReadOnlyStore(tmp_path / "state.sqlite")
    with store._connect() as db:
        row = db.execute(
            "SELECT a.task_id,a.revision,a.attempt_id,c.execution_digest FROM attempt a JOIN operation_attempt c ON c.attempt_id=a.attempt_id WHERE c.operation_id='op'"
        ).fetchone()
        assert db.execute(
            "SELECT basis FROM artifact_recovery_reconciliation WHERE recovery_id='restore-1'"
        ).fetchone() == ("observed_link",)
        reviews = db.execute(
            "SELECT review_id FROM form_review WHERE artifact_id='artifact'"
        ).fetchall()
        before = list(db.iterdump())
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda *_: pytest.fail("evaluate must not query"),
    )
    result = recovery_evaluation.evaluate(
        store,
        "op",
        "a",
        row[0],
        row[1],
        "form",
        row[2],
        row[3],
        artifact_root=tmp_path,
        budget=ResourceBudget(100000, 10000, 100000),
    )
    assert result["artifacts"][0]["artifact_id"] == "artifact"
    assert result["artifacts"][0]["file"]["state"] == "verified"
    assert result["artifacts"][0]["review"]["state"] == "accept"
    assert result["reason"] == "historical_revision_requires_review"
    with store._connect() as db:
        assert (
            db.execute(
                "SELECT review_id FROM form_review WHERE artifact_id='artifact'"
            ).fetchall()
            == reviews
        )
        assert list(db.iterdump()) == before


def test_unknown_later_succeeded_without_linked_operation(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import offline_submission
    from ecommerce_product_image_creator.execution_candidates import (
        register_root_candidate,
    )

    from .authorized_query_fixture import install
    from .test_offline_submission import setup_context

    install(monkeypatch)

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    sends = []

    def timeout(_):
        sends.append(1)
        raise TimeoutError()

    monkeypatch.setattr(offline_submission, "_offline_dispatch", timeout)
    attempt = offline_submission.submit_offline(service, *args)
    register_root_candidate(service.store, "op", "a")
    assert attempt.state == "unknown"
    service.store.record_upstream(attempt.attempt_id, UpstreamStatus.UNKNOWN, "late-id")
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: (
            i.attempt_id == attempt.attempt_id and i.upstream_id == "late-id"
        ),
    )
    monkeypatch.setattr(
        recovery, "_verify_observation_source", lambda i, row: row[6] == i.attempt_id
    )
    queries = []

    def query(i):
        queries.append(i.upstream_id)
        return {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    snap = args[4]
    identity = (
        "op",
        "a",
        snap.task_id,
        1,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
    )
    assert (
        original_query.query_once(
            service.store,
            identity[0],
            identity[1],
            identity[2],
            identity[3],
            identity[5],
            identity[6],
        ).state
        == "succeeded"
    )
    result = evaluate_unchanged(service, identity, sends, queries, tmp_path)
    assert result["historical_upstream_status"] == "unknown"
    assert result["recovery"]["state"] == "succeeded"
    assert result["action"] == "assess_original_artifact"
    assert result["linked_operations"] == []
    assert sends == [1] and queries == ["late-id"]


def test_original_and_linked_keep_separate_artifacts(
    linked_context,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from ecommerce_product_image_creator import offline_submission

    from .authorized_query_fixture import install

    install(monkeypatch)

    service, args, source, sends = linked_context

    def dispatch(_):
        sends.append(1)
        return offline_submission.ProviderResult("succeeded", task_id="linked-id")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", dispatch)
    linked = offline_submission.submit_offline(
        service, *args, risk_authorization_id="risk-1"
    )
    service.store.record_upstream(source.attempt_id, UpstreamStatus.UNKNOWN, "late-id")
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: (
            (i.attempt_id, i.upstream_id)
            in {(source.attempt_id, "late-id"), (linked.attempt_id, "linked-id")}
        ),
    )
    monkeypatch.setattr(
        recovery, "_verify_observation_source", lambda i, row: row[6] == i.attempt_id
    )
    queries = []

    def query(i):
        queries.append(i.upstream_id)
        return {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}

    monkeypatch.setattr(original_query, "_query_adapter", query)
    snap = args[4]
    with service.store._connect() as db:
        candidate = db.execute(
            "SELECT linked_candidate FROM risk_consumption WHERE linked_attempt=?",
            (linked.attempt_id,),
        ).fetchone()[0]
    identities = [
        (
            "op",
            "source-candidate",
            snap.task_id,
            1,
            snap.stage,
            source.attempt_id,
            snap.execution_digest,
        ),
        (
            "linked-op",
            candidate,
            snap.task_id,
            1,
            snap.stage,
            linked.attempt_id,
            snap.execution_digest,
        ),
    ]
    for identity in identities:
        assert (
            original_query.query_once(
                service.store,
                identity[0],
                identity[1],
                identity[2],
                identity[3],
                identity[5],
                identity[6],
            ).state
            == "succeeded"
        )
    # The source transition is based on the independently recorded, fixture-verified observation.
    service.store.record_upstream(
        source.attempt_id, UpstreamStatus.SUCCEEDED, "late-id"
    )
    for attempt, artifact_id, color in [
        (source, "source-art", "blue"),
        (linked, "linked-art", "green"),
    ]:
        path = tmp_path / (artifact_id + ".png")
        Image.new("RGB", (5, 5), color).save(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        register_form_artifact(
            service.store,
            snap.task_id,
            1,
            attempt.attempt_id,
            artifact_id,
            tmp_path,
            path,
            "image/png",
            digest,
            service.budget,
        )
    original = evaluate_unchanged(service, identities[0], sends, queries, tmp_path)
    target = evaluate_unchanged(service, identities[1], sends, queries, tmp_path)
    assert [a["artifact_id"] for a in original["artifacts"]] == ["source-art"]
    assert [a["artifact_id"] for a in target["artifacts"]] == ["linked-art"]
    assert (
        original["artifacts"][0]["file"]["state"]
        == target["artifacts"][0]["file"]["state"]
        == "verified"
    )
    assert (
        original["artifacts"][0]["review"]["state"]
        == target["artifacts"][0]["review"]["state"]
        == "pending"
    )
    assert (
        original["duplicate_generation_risk"]
        and target["linked_source"]["attempt_id"] == source.attempt_id
    )
    assert len(sends) == 2 and queries == ["late-id", "linked-id"]
