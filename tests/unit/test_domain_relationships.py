from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator.domain import (
    Artifact,
    Attempt,
    Confirmation,
    DeliveryStatus,
    ExecutionSnapshot,
    FormRevision,
    PromptRevision,
    Reference,
    ReferenceCategory,
    Revision,
    Task,
    UpstreamStatus,
    ViewRevision,
    check_delivery_transition,
    digest_references,
    validate_artifact,
    validate_attempt,
    validate_form_revision,
    validate_revision_context,
    validate_view_revision,
)


def context():
    refs = [
        Reference(
            "a",
            ReferenceCategory.ARTWORK,
            Path("/a"),
            "image/png",
            1,
            "a" * 64,
            task_id="t",
        ),
        Reference(
            "f",
            ReferenceCategory.FORM,
            Path("/f"),
            "image/jpeg",
            1,
            "b" * 64,
            task_id="t",
        ),
    ]
    digest = digest_references(refs, "request")
    task = Task("t", digest, "request")
    revision = Revision("t", 1, digest)
    prompt = PromptRevision.from_text("t", 1, 1, "form", "draw six views")
    snapshot = ExecutionSnapshot.create(
        revision, prompt, "form", "model", "endpoint", "group"
    )
    confirmation = Confirmation(
        "t", 1, digest, prompt.prompt_digest, 1, snapshot.execution_digest
    )
    return task, revision, refs, prompt, snapshot, confirmation


def test_valid_context_and_stale_confirmation_rejected():
    task, revision, refs, prompt, snapshot, confirmation = context()
    validate_revision_context(task, revision, refs, prompt, snapshot, confirmation)
    newer = replace(revision, revision=2)
    newer_prompt = PromptRevision.from_text("t", 2, 2, "form", "draw six views")
    newer_snapshot = ExecutionSnapshot.create(
        newer, newer_prompt, "form", "model", "endpoint", "group"
    )
    with pytest.raises(ValueError):
        validate_revision_context(
            task, newer, refs, newer_prompt, newer_snapshot, confirmation
        )
    changed = PromptRevision.from_text("t", 1, 2, "form", "changed")
    new_snapshot = ExecutionSnapshot.create(
        revision, changed, "form", "model", "endpoint", "group"
    )
    with pytest.raises(ValueError):
        validate_revision_context(
            task, revision, refs, changed, new_snapshot, confirmation
        )


def test_reference_and_revision_cross_task_rejected():
    task, revision, refs, prompt, snapshot, confirmation = context()
    with pytest.raises(ValueError):
        validate_revision_context(
            task,
            replace(revision, task_id="other"),
            refs,
            prompt,
            snapshot,
            confirmation,
        )
    refs[0] = replace(refs[0], task_id="other")
    with pytest.raises(ValueError):
        validate_revision_context(task, revision, refs, prompt, snapshot, confirmation)


def test_changed_reference_or_prompt_digest_rejected():
    task, revision, refs, prompt, snapshot, confirmation = context()
    refs[0] = replace(refs[0], sha256="c" * 64)
    with pytest.raises(ValueError):
        validate_revision_context(task, revision, refs, prompt, snapshot, confirmation)
    with pytest.raises(ValueError):
        replace(prompt, text="different")
    with pytest.raises(ValueError):
        validate_revision_context(
            replace(task, instructions="changed"),
            revision,
            context()[2],
            prompt,
            snapshot,
            confirmation,
        )
    with pytest.raises(ValueError):
        validate_revision_context(
            task,
            revision,
            context()[2],
            prompt,
            replace(snapshot, prompt_digest="0" * 64),
            confirmation,
        )


def test_attempt_and_artifact_ownership():
    _, revision, _, _, snapshot, _ = context()
    attempt = Attempt(
        "attempt", "t", 1, operation_id="op", execution_digest=snapshot.execution_digest
    )
    validate_attempt(revision, snapshot, attempt)
    artifact = Artifact(
        "artifact", "attempt", Path("/output"), "c" * 64, task_id="t", revision=1
    )
    validate_artifact(attempt, artifact)
    for invalid in [
        replace(attempt, task_id="other"),
        replace(attempt, revision=2),
        replace(attempt, execution_digest="x"),
    ]:
        with pytest.raises(ValueError):
            validate_attempt(revision, snapshot, invalid)
    for invalid in [
        replace(artifact, task_id="other"),
        replace(artifact, revision=2),
        replace(artifact, attempt_id="other"),
    ]:
        with pytest.raises(ValueError):
            validate_artifact(attempt, invalid)
    previous = Attempt("previous", "other", 1)
    with pytest.raises(ValueError):
        validate_attempt(
            revision,
            snapshot,
            replace(attempt, previous_attempt_id="previous"),
            previous,
        )


def test_confirmation_cannot_assert_verified_authorization():
    *_, confirmation = context()
    assert confirmation.evidence_status == "unverified"
    with pytest.raises(ValueError):
        replace(confirmation, evidence_status="verified")


def test_delivery_transitions_require_evidence_and_upstream_success():
    check_delivery_transition(
        DeliveryStatus.NOT_AVAILABLE,
        DeliveryStatus.PENDING,
        UpstreamStatus.SUCCEEDED,
        evidence=True,
    )
    with pytest.raises(ValueError):
        check_delivery_transition(
            DeliveryStatus.PENDING,
            DeliveryStatus.DELIVERED,
            UpstreamStatus.SUCCEEDED,
            evidence=False,
        )
    with pytest.raises(ValueError):
        check_delivery_transition(
            DeliveryStatus.NOT_AVAILABLE,
            DeliveryStatus.DELIVERED,
            UpstreamStatus.SUCCEEDED,
            evidence=True,
        )
    with pytest.raises(ValueError):
        check_delivery_transition(
            DeliveryStatus.PENDING,
            DeliveryStatus.DELIVERED,
            UpstreamStatus.UNKNOWN,
            evidence=True,
        )


def test_form_and_view_revisions_keep_their_origin():
    attempt = Attempt("attempt", "t", 1)
    artifact = Artifact(
        "artifact", "attempt", Path("/output"), "c" * 64, task_id="t", revision=1
    )
    form = FormRevision("t", 1, "attempt", "c" * 64)
    validate_form_revision(form, attempt, artifact)
    with pytest.raises(ValueError):
        validate_form_revision(
            replace(form, artifact_sha256="d" * 64), attempt, artifact
        )
    view_attempt = Attempt("view-attempt", "t", 2)
    view = ViewRevision("t", 1, "F", 2, "view-attempt")
    validate_view_revision(view, form, view_attempt)
    with pytest.raises(ValueError):
        validate_view_revision(replace(view, form_revision=2), form, view_attempt)
    with pytest.raises(ValueError):
        validate_view_revision(replace(view, view="X"), form, view_attempt)
