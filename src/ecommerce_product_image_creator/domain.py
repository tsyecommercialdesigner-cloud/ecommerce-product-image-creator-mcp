"""Typed, side-effect-free domain data and upstream status transitions."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ReferenceCategory(StrEnum):
    ARTWORK = "artwork"
    FORM = "form"


class InputValidationError(ValueError):
    """Input error with structured location; messages omit user-provided values."""

    def __init__(
        self,
        message: str,
        *,
        ref_id: str | None = None,
        category: str | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.ref_id = ref_id
        self.category = category
        self.field = field


class TaskStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY = "ready"
    ACTIVE = "active"
    CLOSED = "closed"


class UpstreamStatus(StrEnum):
    NOT_SUBMITTED = "not_submitted"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DeliveryStatus(StrEnum):
    NOT_AVAILABLE = "not_available"
    PENDING = "pending"
    DELIVERED = "delivered"
    DOWNLOAD_FAILED = "download_failed"
    SAVE_FAILED = "save_failed"
    MISSING_OR_CORRUPT = "missing_or_corrupt"


class ObservationSource(StrEnum):
    OPERATOR = "operator"
    AGENT = "agent"
    INDEPENDENT = "independent"


class ObservationStatus(StrEnum):
    UNKNOWN = "unknown"
    DESCRIBED = "described"
    ASSUMED = "assumed"
    VERIFIED = "verified"


@dataclass(frozen=True)
class Observation:
    """A supplied claim, an explicit assumption, or absence of evidence."""

    subject: str
    status: ObservationStatus
    source: ObservationSource | None = None
    source_ref_id: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.subject, str)
            or not self.subject.strip()
            or not isinstance(self.status, ObservationStatus)
            or (
                self.source is not None
                and not isinstance(self.source, ObservationSource)
            )
        ):
            raise ValueError("Invalid observation")
        if (
            self.status is ObservationStatus.VERIFIED
            or self.source is ObservationSource.INDEPENDENT
        ):
            raise ValueError("Independent verification is not available")
        if self.status is ObservationStatus.UNKNOWN:
            if (
                self.source is not None
                or self.source_ref_id is not None
                or self.description is not None
            ):
                raise ValueError("Unknown observations cannot assert evidence")
        elif (
            self.source not in {ObservationSource.OPERATOR, ObservationSource.AGENT}
            or not isinstance(self.source_ref_id, str)
            or not self.source_ref_id.strip()
            or not isinstance(self.description, str)
            or not self.description.strip()
        ):
            raise ValueError("Observation requires a source and reference")


@dataclass(frozen=True)
class Reference:
    ref_id: str
    category: ReferenceCategory
    path: Path
    mime: str
    byte_count: int
    sha256: str
    view: str | None = None
    region: str | None = None
    version: str | None = None
    importance: str | None = None
    task_id: str | None = None
    purpose: str | None = None
    observations: tuple[Observation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.observations, tuple) or any(
            not isinstance(item, Observation)
            or (item.source_ref_id is not None and item.source_ref_id != self.ref_id)
            for item in self.observations
        ):
            raise InputValidationError(
                "Observation reference mismatch",
                ref_id=self.ref_id,
                field="observations",
            )
        if not self.ref_id:
            raise InputValidationError("Invalid reference identity", field="ref_id")
        if not isinstance(self.category, ReferenceCategory):
            raise InputValidationError(
                "Invalid reference category", ref_id=self.ref_id, field="category"
            )
        if self.mime not in {"image/jpeg", "image/png"} or self.byte_count <= 0:
            raise InputValidationError(
                "Invalid reference format or size",
                ref_id=self.ref_id,
                field="mime_or_size",
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise InputValidationError(
                "Invalid SHA-256", ref_id=self.ref_id, field="sha256"
            )


def validate_references(refs: list[Reference]) -> None:
    if len({ref.ref_id for ref in refs}) != len(refs):
        raise InputValidationError("Duplicate reference ID", field="ref_id")
    art = sum(ref.category == ReferenceCategory.ARTWORK for ref in refs)
    form = sum(ref.category == ReferenceCategory.FORM for ref in refs)
    if not 1 <= art <= 2:
        raise InputValidationError(
            "Reference category count outside permitted range", category="artwork"
        )
    if not 1 <= form <= 6:
        raise InputValidationError(
            "Reference category count outside permitted range", category="form"
        )


def validate_task_input(refs: list[Reference], instructions: str) -> None:
    """Validate metadata; actual image bytes must also pass the file boundary."""
    validate_references(refs)
    if not isinstance(instructions, str) or not instructions.strip():
        raise InputValidationError(
            "Natural language instructions are required", field="instructions"
        )


@dataclass(frozen=True)
class Task:
    task_id: str
    input_digest: str
    instructions: str
    status: TaskStatus = TaskStatus.DRAFT


@dataclass(frozen=True)
class Revision:
    task_id: str
    revision: int
    input_digest: str


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    task_id: str
    revision: int
    upstream_status: UpstreamStatus = UpstreamStatus.NOT_SUBMITTED
    delivery_status: DeliveryStatus = DeliveryStatus.NOT_AVAILABLE
    upstream_id: str | None = None
    previous_attempt_id: str | None = None
    operation_id: str | None = None
    execution_digest: str | None = None


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    attempt_id: str
    path: Path
    sha256: str
    task_id: str | None = None
    revision: int | None = None


@dataclass(frozen=True)
class ExecutionSnapshot:
    task_id: str
    revision: int
    input_digest: str
    prompt_digest: str
    execution_digest: str
    prompt_revision: int
    stage: str
    model: str
    endpoint: str
    group: str
    references: tuple[tuple[str, str], ...] = ()
    operation_scope: str = ""
    digest_version: int = 1

    @classmethod
    def create(
        cls,
        revision: "Revision",
        prompt: "PromptRevision",
        stage: str,
        model: str,
        endpoint: str,
        group: str,
        *,
        references: tuple[tuple[str, str], ...] = (),
        operation_scope: str = "",
    ) -> "ExecutionSnapshot":
        if (revision.task_id, revision.revision) != (
            prompt.task_id,
            prompt.revision,
        ) or stage != prompt.stage:
            raise ValueError("Prompt does not belong to revision or stage")
        values = (
            revision.task_id,
            revision.revision,
            revision.input_digest,
            prompt.prompt_revision,
            prompt.prompt_digest,
            stage,
            model,
            endpoint,
            group,
        )
        if not all(values):
            raise ValueError("Incomplete execution snapshot")
        version = 2 if references or operation_scope else 1
        if version == 2:
            if (
                not references
                or not operation_scope
                or any(
                    not ref_id or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    for ref_id, digest in references
                )
            ):
                raise ValueError("Invalid execution references or scope")
            values = (2, *values, references, operation_scope)
        return cls(
            revision.task_id,
            revision.revision,
            revision.input_digest,
            prompt.prompt_digest,
            _digest(values),
            prompt.prompt_revision,
            stage,
            model,
            endpoint,
            group,
            references,
            operation_scope,
            version,
        )

    def to_payload(self) -> str:
        return json.dumps(
            {
                "task_id": self.task_id,
                "revision": self.revision,
                "input_digest": self.input_digest,
                "prompt_digest": self.prompt_digest,
                "execution_digest": self.execution_digest,
                "prompt_revision": self.prompt_revision,
                "stage": self.stage,
                "model": self.model,
                "endpoint": self.endpoint,
                "group": self.group,
                "references": self.references,
                "operation_scope": self.operation_scope,
                "digest_version": self.digest_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_payload(cls, payload: str) -> "ExecutionSnapshot":
        try:
            data = json.loads(payload)
            data["references"] = tuple(tuple(item) for item in data["references"])
            return cls(**data)
        except (ValueError, TypeError, KeyError):
            raise ValueError("Invalid execution snapshot payload") from None

    def __post_init__(self) -> None:
        values = (
            self.task_id,
            self.revision,
            self.input_digest,
            self.prompt_revision,
            self.prompt_digest,
            self.stage,
            self.model,
            self.endpoint,
            self.group,
        )
        if self.digest_version == 2:
            if (
                not self.references
                or not self.operation_scope
                or any(
                    not name or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    for name, digest in self.references
                )
            ):
                raise ValueError("Invalid execution references or scope")
            values = (2, *values, self.references, self.operation_scope)
        elif self.digest_version != 1 or self.references or self.operation_scope:
            raise ValueError("Unsupported execution digest version")
        if (
            not all(values)
            or self.revision < 1
            or self.prompt_revision < 1
            or self.execution_digest != _digest(values)
        ):
            raise ValueError("Invalid execution snapshot")


@dataclass(frozen=True)
class HostReviewInteraction:
    task_id: str
    revision: int
    artifact_digest: str
    evidence_status: str = "unverified"


@dataclass(frozen=True)
class PromptRevision:
    task_id: str
    revision: int
    prompt_digest: str
    prompt_revision: int
    stage: str
    text: str

    @classmethod
    def from_text(
        cls, task_id: str, revision: int, prompt_revision: int, stage: str, text: str
    ) -> "PromptRevision":
        return cls(
            task_id,
            revision,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            prompt_revision,
            stage,
            text,
        )

    def __post_init__(self) -> None:
        if (
            not self.task_id
            or self.revision < 1
            or self.prompt_revision < 1
            or self.stage not in {"form", "mock"}
            or not self.text.strip()
            or self.prompt_digest
            != hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        ):
            raise ValueError("Invalid prompt revision or digest")


@dataclass(frozen=True)
class FormRevision:
    task_id: str
    revision: int
    attempt_id: str
    artifact_sha256: str
    confirmed: bool = False


@dataclass(frozen=True)
class ViewRevision:
    """Legacy single-view record retained for historical reading."""

    task_id: str
    form_revision: int
    view: str
    revision: int
    attempt_id: str
    confirmed: bool = False


@dataclass(frozen=True)
class MockRevision:
    """One composite Mock revision and the exact form artifact it uses."""

    task_id: str
    revision: int
    attempt_id: str
    form_revision: int
    form_artifact_id: str
    form_artifact_digest: str


@dataclass(frozen=True)
class Confirmation:
    task_id: str
    revision: int
    input_digest: str
    prompt_digest: str
    prompt_revision: int
    execution_digest: str
    confirmation_id: str | None = None
    evidence_status: str = field(default="unverified", init=False)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts_per_combination: int = 3


def _digest(values: object) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def digest_references(refs: list[Reference], instructions: str) -> str:
    """Bind task instructions and ordered reference metadata and content."""
    validate_references(refs)
    if not instructions.strip():
        raise ValueError("Task instructions are required")
    reference_values = [
        (
            r.task_id,
            r.ref_id,
            r.category.value,
            r.mime,
            r.byte_count,
            r.sha256,
            r.view,
            r.region,
            r.version,
            r.importance,
        )
        for r in refs
    ]
    # Preserve existing digests for historical snapshots without purpose metadata.
    if any(r.purpose is not None for r in refs):
        reference_values = [
            (*values, r.purpose)
            for values, r in zip(reference_values, refs, strict=True)
        ]
    if any(r.observations for r in refs):
        reference_values = [
            (
                *values,
                [
                    (
                        o.subject,
                        o.status.value,
                        o.source.value if o.source else None,
                        o.source_ref_id,
                        o.description,
                    )
                    for o in r.observations
                ],
            )
            for values, r in zip(reference_values, refs, strict=True)
        ]
    return _digest(
        (
            instructions,
            reference_values,
        )
    )


def validate_revision_context(
    task: Task,
    revision: Revision,
    refs: list[Reference],
    prompt: PromptRevision,
    snapshot: ExecutionSnapshot,
    confirmation: Confirmation,
) -> None:
    """Check consistency only; matching confirmation is not proof of authorization."""
    if (
        not task.task_id
        or revision.revision < 1
        or task.task_id != revision.task_id
        or any(ref.task_id != task.task_id for ref in refs)
    ):
        raise ValueError("Cross-task or invalid revision reference")
    if (
        digest_references(refs, task.instructions) != revision.input_digest
        or task.input_digest != revision.input_digest
    ):
        raise ValueError("Input digest mismatch")
    if (prompt.task_id, prompt.revision) != (revision.task_id, revision.revision):
        raise ValueError("Prompt belongs to another revision")
    expected = ExecutionSnapshot.create(
        revision,
        prompt,
        snapshot.stage,
        snapshot.model,
        snapshot.endpoint,
        snapshot.group,
        references=snapshot.references,
        operation_scope=snapshot.operation_scope,
    )
    if snapshot != expected:
        raise ValueError("Execution snapshot mismatch")
    if (
        confirmation.task_id,
        confirmation.revision,
        confirmation.input_digest,
        confirmation.prompt_digest,
        confirmation.prompt_revision,
        confirmation.execution_digest,
    ) != (
        task.task_id,
        revision.revision,
        revision.input_digest,
        prompt.prompt_digest,
        prompt.prompt_revision,
        snapshot.execution_digest,
    ):
        raise ValueError("Confirmation does not match current execution")


def validate_attempt(
    revision: Revision,
    snapshot: ExecutionSnapshot,
    attempt: Attempt,
    previous: Attempt | None = None,
) -> None:
    if (
        (attempt.task_id, attempt.revision) != (revision.task_id, revision.revision)
        or (snapshot.task_id, snapshot.revision, snapshot.input_digest)
        != (revision.task_id, revision.revision, revision.input_digest)
        or not attempt.attempt_id
        or not attempt.operation_id
        or attempt.execution_digest != snapshot.execution_digest
    ):
        raise ValueError("Attempt does not match revision and operation")
    if attempt.previous_attempt_id is not None and (
        previous is None
        or previous.attempt_id != attempt.previous_attempt_id
        or previous.task_id != attempt.task_id
        or previous.attempt_id == attempt.attempt_id
    ):
        raise ValueError("Previous attempt does not belong to task")
    if attempt.previous_attempt_id is None and previous is not None:
        raise ValueError("Unexpected previous attempt")


def validate_artifact(attempt: Attempt, artifact: Artifact) -> None:
    if (
        not artifact.artifact_id
        or (artifact.attempt_id, artifact.task_id, artifact.revision)
        != (attempt.attempt_id, attempt.task_id, attempt.revision)
        or not re.fullmatch(r"[0-9a-f]{64}", artifact.sha256)
    ):
        raise ValueError("Artifact does not belong to attempt")


def validate_form_revision(
    form: FormRevision, attempt: Attempt, artifact: Artifact
) -> None:
    """Check origin linkage only; the confirmed flag is not authorization evidence."""
    validate_artifact(attempt, artifact)
    if (form.task_id, form.revision, form.attempt_id, form.artifact_sha256) != (
        attempt.task_id,
        attempt.revision,
        attempt.attempt_id,
        artifact.sha256,
    ):
        raise ValueError("Form revision does not match its artifact")


def validate_view_revision(
    view: ViewRevision, form: FormRevision, attempt: Attempt
) -> None:
    """Check view provenance; operational approval is a separate concern."""
    if (
        (view.task_id, view.revision, view.attempt_id)
        != (attempt.task_id, attempt.revision, attempt.attempt_id)
        or (view.task_id, view.form_revision) != (form.task_id, form.revision)
        or view.view not in {"F", "L", "R", "B", "T", "Q"}
    ):
        raise ValueError("View revision origin mismatch")


def validate_mock_revision(
    mock: MockRevision,
    form: FormRevision,
    form_attempt: Attempt,
    form_artifact: Artifact,
    mock_attempt: Attempt,
) -> None:
    """Validate provenance only; does not prove human approval."""
    validate_form_revision(form, form_attempt, form_artifact)
    if (mock.task_id, mock.revision, mock.attempt_id) != (
        mock_attempt.task_id,
        mock_attempt.revision,
        mock_attempt.attempt_id,
    ) or (
        mock.task_id,
        mock.form_revision,
        mock.form_artifact_id,
        mock.form_artifact_digest,
    ) != (form.task_id, form.revision, form_artifact.artifact_id, form_artifact.sha256):
        raise ValueError("Mock composite origin mismatch")


def check_delivery_transition(
    old: DeliveryStatus,
    new: DeliveryStatus,
    upstream: UpstreamStatus,
    *,
    evidence: bool,
) -> None:
    allowed = {
        DeliveryStatus.NOT_AVAILABLE: {DeliveryStatus.PENDING},
        DeliveryStatus.PENDING: {
            DeliveryStatus.DELIVERED,
            DeliveryStatus.DOWNLOAD_FAILED,
            DeliveryStatus.SAVE_FAILED,
        },
        DeliveryStatus.DOWNLOAD_FAILED: {DeliveryStatus.PENDING},
        DeliveryStatus.SAVE_FAILED: {DeliveryStatus.PENDING},
        DeliveryStatus.DELIVERED: {DeliveryStatus.MISSING_OR_CORRUPT},
        DeliveryStatus.MISSING_OR_CORRUPT: {DeliveryStatus.PENDING},
    }
    if (
        upstream != UpstreamStatus.SUCCEEDED
        or not evidence
        or (new != old and new not in allowed[old])
    ):
        raise ValueError("Delivery transition requires upstream success and evidence")


_TRANSITIONS = {
    UpstreamStatus.NOT_SUBMITTED: {UpstreamStatus.SUBMISSION_UNCERTAIN},
    UpstreamStatus.SUBMISSION_UNCERTAIN: {
        UpstreamStatus.ACCEPTED,
        UpstreamStatus.RUNNING,
        UpstreamStatus.SUCCEEDED,
        UpstreamStatus.FAILED,
        UpstreamStatus.UNKNOWN,
    },
    UpstreamStatus.ACCEPTED: {
        UpstreamStatus.RUNNING,
        UpstreamStatus.SUCCEEDED,
        UpstreamStatus.FAILED,
        UpstreamStatus.UNKNOWN,
    },
    UpstreamStatus.RUNNING: {
        UpstreamStatus.SUCCEEDED,
        UpstreamStatus.FAILED,
        UpstreamStatus.UNKNOWN,
    },
    UpstreamStatus.UNKNOWN: {
        UpstreamStatus.ACCEPTED,
        UpstreamStatus.RUNNING,
        UpstreamStatus.SUCCEEDED,
        UpstreamStatus.FAILED,
    },
    UpstreamStatus.SUCCEEDED: set(),
    UpstreamStatus.FAILED: set(),
}


def check_transition(old: UpstreamStatus, new: UpstreamStatus) -> None:
    """Only evidence-driven upstream transitions; no unknown-to-resubmit path."""
    if new != old and new not in _TRANSITIONS[old]:
        raise ValueError("Invalid upstream state transition")


def check_task_transition(old: TaskStatus, new: TaskStatus) -> None:
    allowed = {
        TaskStatus.DRAFT: {TaskStatus.AWAITING_CONFIRMATION},
        TaskStatus.AWAITING_CONFIRMATION: {TaskStatus.READY, TaskStatus.CLOSED},
        TaskStatus.READY: {
            TaskStatus.ACTIVE,
            TaskStatus.AWAITING_CONFIRMATION,
            TaskStatus.CLOSED,
        },
        TaskStatus.ACTIVE: {TaskStatus.AWAITING_CONFIRMATION, TaskStatus.CLOSED},
        TaskStatus.CLOSED: set(),
    }
    if new != old and new not in allowed[old]:
        raise ValueError("Invalid task state transition")
