"""Candidate contract: local v2 facts and explicit unimplemented gates."""

import time
from dataclasses import replace
from pathlib import Path

import pytest
from integration.test_offline_submission import setup_context

from ecommerce_product_image_creator.domain import ExecutionSnapshot
from ecommerce_product_image_creator.preparation import read_prepared
from ecommerce_product_image_creator.tasks import EligibilityError


def alternate(args):
    _task, revision, _refs, prompt, original, _confirmation, _operation, _scope = args
    return ExecutionSnapshot.create(
        revision,
        prompt,
        original.stage,
        "gpt-image-2.5-flare",
        original.endpoint,
        original.group,
        references=original.references,
        operation_scope=original.operation_scope,
    )


def test_model_change_produces_distinct_v2_digest_without_modifying_frozen_input(
    tmp_path: Path,
):
    service, args = setup_context(tmp_path)
    before = read_prepared(service.store, args[0].task_id, 1)
    with service.store._connect() as db:
        blobs = db.execute(
            "SELECT ref_id,sha256,data FROM prepared_reference WHERE task_id=? AND revision=? ORDER BY position",
            (args[0].task_id, 1),
        ).fetchall()
    candidate = alternate(args)
    assert (
        candidate.digest_version == 2
        and candidate.execution_digest != args[4].execution_digest
    )
    assert (
        candidate.task_id,
        candidate.revision,
        candidate.stage,
        candidate.prompt_revision,
        candidate.prompt_digest,
        candidate.references,
        candidate.group,
        candidate.operation_scope,
    ) == (
        args[4].task_id,
        args[4].revision,
        args[4].stage,
        args[4].prompt_revision,
        args[4].prompt_digest,
        args[4].references,
        args[4].group,
        args[4].operation_scope,
    )
    assert (
        read_prepared(service.store, args[0].task_id, 1)["prompt"] == before["prompt"]
    )
    with service.store._connect() as db:
        assert (
            db.execute(
                "SELECT ref_id,sha256,data FROM prepared_reference WHERE task_id=? AND revision=? ORDER BY position",
                (args[0].task_id, 1),
            ).fetchall()
            == blobs
        )


def test_old_confirmation_cannot_authorize_alternate_candidate(tmp_path: Path):
    service, args = setup_context(tmp_path)
    candidate = alternate(args)
    with pytest.raises(EligibilityError):
        service._validate(*args[:4], candidate, args[5], "alternate", args[7])


def test_locally_matching_alternate_candidate_needs_persisted_context(
    tmp_path: Path,
):
    service, args = setup_context(tmp_path)
    candidate = alternate(args)
    confirmation = replace(
        args[5],
        confirmation_id="alternate-confirmation",
        execution_digest=candidate.execution_digest,
    )
    with pytest.raises(EligibilityError):
        service.prepare(
            *args[:4],
            candidate,
            confirmation,
            "alternate",
            args[7],
            expires_at=time.time() + 120,
        )
    with service.store._connect() as db:
        row = db.execute(
            "SELECT execution_digest FROM local_eligibility WHERE operation_id=?",
            ("alternate",),
        ).fetchone()
    assert row is None


def test_deterministic_parameter_failure_is_not_a_retryable_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from integration.test_retry_scheduler import FakeClock

    from ecommerce_product_image_creator import (
        faults,
        offline_submission,
        retry_scheduler,
    )
    from ecommerce_product_image_creator.send_evidence import verified_pre_send

    service, args = setup_context(tmp_path)
    clock = FakeClock()
    monkeypatch.setattr(offline_submission, "_clock_now", clock)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda _: calls.append(1)
    )

    def deterministic_parameter_error(_):
        raise faults.DeterministicInputError("invalid parameter")

    monkeypatch.setattr(
        offline_submission, "_pre_dispatch_validate", deterministic_parameter_error
    )
    first = offline_submission.submit_offline(service, *args)
    assert first.state == "unknown" and calls == []
    confirmation = replace(args[5], confirmation_id="retry-confirmation")
    retry_args = (*args[:5], confirmation, "retry-request", args[7])
    service.prepare(*retry_args, expires_at=clock() + 120)
    monkeypatch.setattr(
        offline_submission,
        "_verify_retry_evidence",
        lambda db, root, attempt, snapshot, _confirmation: verified_pre_send(
            db,
            root,
            attempt,
            snapshot.task_id,
            snapshot.revision,
            snapshot.execution_digest,
        ),
    )
    monkeypatch.setattr(
        offline_submission, "_verify_retry_authorization", lambda *_: True
    )
    monkeypatch.setattr(retry_scheduler, "_combination_verified", lambda *_: True)
    result = retry_scheduler.OfflineRetryScheduler(clock).step(
        service, *retry_args, root="op"
    )
    assert result.action == "stop"
    assert calls == []
