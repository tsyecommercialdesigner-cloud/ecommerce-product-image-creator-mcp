"""Persist submission attempts; Team Form dispatch is explicitly opt-in."""

import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecommerce_product_image_creator import faults
from ecommerce_product_image_creator.business_choices import (
    evaluate_choices,
    read_choice,
    subset_gate_locked,
)
from ecommerce_product_image_creator.domain import (
    Confirmation,
    ExecutionSnapshot,
    PromptRevision,
    Reference,
    Revision,
    Task,
)
from ecommerce_product_image_creator.input_revision import input_revision_gate_locked
from ecommerce_product_image_creator.preparation import (
    read_prepare_response,
    read_prepared,
)
from ecommerce_product_image_creator.preview_evidence import (
    persist_preview,
    read_preview,
)
from ecommerce_product_image_creator.provider import (
    ProviderResult,
    SerializedRequest,
    build_request,
    serialize_request,
)
from ecommerce_product_image_creator.send_evidence import (
    verified_pre_send,
    verified_recoverability,
)
from ecommerce_product_image_creator.storage import read_reference
from ecommerce_product_image_creator.tasks import (
    EligibilityError,
    LocalEligibility,
    OperationScope,
)


def _clock_now() -> float:
    """Runtime wall clock; replace only in isolated tests."""
    return time.time()


class SubmissionError(ValueError):
    """Sanitized denial or local state conflict."""


@dataclass(frozen=True)
class OfflineSubmission:
    operation_id: str
    attempt_id: str
    state: str
    local_delivery: str = "not_available"
    replayed: bool = False
    preview: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "unknown",
            "structure_verified": False,
            "current_decision": "unknown",
        }
    )


def _verify_authorization(snapshot: ExecutionSnapshot) -> bool:
    """No production Host authorization evidence verifier is configured."""
    return False


def _verify_linked_fee(
    db: sqlite3.Connection,
    evidence: object,
    snapshot: ExecutionSnapshot,
    confirmation_id: str,
) -> bool:
    """No production verifier for the linked operation's separate fee scope exists."""
    return False


def _verify_business_choices(snapshot: ExecutionSnapshot) -> bool:
    """No production business-choice evidence verifier is configured."""
    return False


def _verify_retry_evidence(
    db: sqlite3.Connection,
    root: str,
    attempt: str,
    snapshot: ExecutionSnapshot,
    confirmation_id: str,
) -> bool:
    """No production verifier for safe remote retry evidence is configured."""
    return False


def _verify_retry_authorization(
    db: sqlite3.Connection,
    root: str,
    attempt: str,
    snapshot: ExecutionSnapshot,
    confirmation_id: str,
) -> bool:
    """No production verifier for retry-specific fee authorization is configured."""
    return False


def _verify_cross_authorization(
    db: sqlite3.Connection, candidate: Any, operation_id: str, confirmation_id: str
) -> bool:
    """No production verifier for attempt-specific cross-candidate authorization."""
    return False


def _offline_dispatch(request: SerializedRequest) -> ProviderResult:
    """No production sender exists; only isolated tests can replace this function."""
    raise SubmissionError("No offline transport configured")


def _after_intent_recorded() -> None:
    """Fault-injection seam; has no runtime inputs or authorization effect."""


def _before_intent_commit() -> None:
    """Fault-injection seam after local writes and before transaction commit."""


def _before_offline_dispatch() -> None:
    """Fault-injection seam after the final local checks, before transport."""


def _after_team_response_received() -> None:
    """Fault-injection seam before an upstream response is persisted."""


def _pre_dispatch_validate(request: SerializedRequest) -> None:
    """Perform only local checks before the irreversible transport boundary."""
    if request.method != "POST":
        raise faults.DeterministicConfigurationError("Local request method invalid")
    if not isinstance(request.body, bytes) or not request.body:
        raise faults.DeterministicInputError("Local request body invalid")


def _record_transport_boundary(
    service: LocalEligibility,
    operation_id: str,
    attempt_id: str,
    phase: str,
    *,
    category: str | None = None,
) -> None:
    """Record one immutable boundary fact, bound to the already reserved attempt."""
    reason = "local_validation" if phase == "pre_send_failure" else "dispatch_boundary"
    with closing(service.store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        inserted = db.execute(
            "INSERT INTO attempt_transport_evidence "
            "SELECT a.attempt_id,o.operation_id,c.operation_id,o.task_id,o.revision,o.digest,?,?,?,? "
            "FROM operation o JOIN attempt a ON a.attempt_id=o.attempt_id "
            "JOIN operation_attempt c ON c.attempt_id=a.attempt_id "
            "WHERE o.operation_id=? AND a.attempt_id=? AND a.upstream_status='submission_uncertain' "
            "AND a.upstream_id IS NULL AND a.task_id=o.task_id AND a.revision=o.revision AND c.execution_digest=o.digest",
            (
                phase,
                reason,
                "server_pre_dispatch",
                _clock_now(),
                operation_id,
                attempt_id,
            ),
        )
        if inserted.rowcount != 1:
            raise SubmissionError("Attempt boundary cannot be recorded")
        if phase == "pre_send_failure" and category is not None:
            db.execute(
                "INSERT INTO attempt_fault_evidence "
                "SELECT attempt_id,logical_operation_id,task_id,revision,execution_digest,?,source,recorded_at "
                "FROM attempt_transport_evidence WHERE attempt_id=?",
                (category, attempt_id),
            )


def _check_mock_source_file(
    db: sqlite3.Connection, service: LocalEligibility, task: Task, revision: Revision
) -> None:
    saved = read_prepared(service.store, task.task_id, revision.revision)
    if saved["stage"] == "mock":
        source = db.execute(
            "SELECT a.path,s.artifact_sha256,r.mime FROM prepared_mock_source s "
            "JOIN artifact a ON a.artifact_id=s.artifact_id "
            "JOIN prepared_reference r ON r.task_id=s.task_id AND r.revision=s.revision AND r.ref_id=s.artifact_id "
            "WHERE s.task_id=? AND s.revision=?",
            (task.task_id, revision.revision),
        ).fetchone()
        try:
            if (
                not source
                or read_reference(
                    service.store.path.parent.resolve(strict=True),
                    Path(source[0]),
                    source[2],
                    service.budget,
                ).sha256
                != source[1]
            ):
                raise ValueError("source mismatch")
        except (ValueError, OSError, MemoryError):
            raise SubmissionError("FORM_ARTIFACT_INVALID") from None


def _check_business_gate(
    db: sqlite3.Connection, service: LocalEligibility, task: Task, revision: Revision
) -> dict[str, Any] | None:
    saved = read_prepared(service.store, task.task_id, revision.revision)
    if not input_revision_gate_locked(
        db, service.store, task.task_id, revision.revision, saved["execution_digest"]
    ):
        raise SubmissionError("Input revision confirmation is unverified or invalid")
    if not subset_gate_locked(db, service.store, task.task_id, revision.revision):
        raise SubmissionError("Business subset clarification is required")
    summary = read_prepare_response(service.store, task.task_id, revision.revision)
    if summary["stage_context"]["prompt_semantic_conflicts"]:
        raise SubmissionError("Prompt brand conflict requires clarification")
    choices = []
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_choice'"
    ).fetchone():
        choices = [
            row[0]
            for row in db.execute(
                "SELECT choice_id FROM business_choice WHERE task_id=? AND revision=?",
                (task.task_id, revision.revision),
            ).fetchall()
        ]
    if (
        summary["structure_conflicts"]["reported"]
        or summary["packaging_versions"]["status"] == "multiple_declared_versions"
        or choices
    ):
        evaluation = evaluate_choices(
            service.store, task.task_id, revision.revision, choice_ids=choices
        )
        if evaluation["status"] == "pending":
            raise SubmissionError("Business clarification is required")
        if evaluation["status"] == "preview":
            decision = read_choice(service.store, evaluation["preview_decision_id"])
            return {
                "status": "preview",
                "task_id": task.task_id,
                "revision": revision.revision,
                "execution_digest": saved["execution_digest"],
                "decision_id": decision.choice_id,
                "conflicts": decision.payload["conflicts"],
                "assumptions": decision.payload["assumptions"],
                "mode": "preview",
                "operation_scope": saved["operation_scope"],
                "source": decision.source,
                "source_trust": "unverified",
                "local_source_check": "accepted_by_configured_verifier",
                "expires_at": decision.expires_at,
                "structure_verified": False,
            }
    return None


def submit_offline(
    service: LocalEligibility,
    task: Task,
    revision: Revision,
    refs: list[Reference],
    prompt: PromptRevision,
    snapshot: ExecutionSnapshot,
    confirmation: Confirmation,
    operation_id: str,
    scope: OperationScope,
    *,
    retry_of: str | None = None,
    risk_authorization_id: str | None = None,
    require_first_authorizations: bool = False,
    require_retry_scope: bool = False,
    team_live: bool = False,
) -> OfflineSubmission:
    """Reserve once atomically and dispatch only through an isolated test replacement."""
    if snapshot.digest_version != 2:
        raise SubmissionError("A complete v2 snapshot is required")
    if team_live:
        from .team_transport import TeamSettings

        settings = TeamSettings.from_environment()
        if (
            not settings.enabled
            or not settings.generation_enabled
            or retry_of is not None
            or risk_authorization_id is not None
            or require_first_authorizations
            or snapshot.stage not in {"form", "mock"}
        ):
            raise SubmissionError("Team submission unavailable")
    if require_retry_scope and (retry_of is None or risk_authorization_id is not None):
        raise SubmissionError("Scoped retry requires a technical retry root")
    confirmation_id = confirmation.confirmation_id
    if not confirmation_id:
        raise SubmissionError("Confirmation identifier is required")
    authorization_ids: tuple[str, str] | None = None
    try:
        with closing(service.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            from . import risk_reservation

            if risk_authorization_id is not None:
                risk_reservation._schema(db)
            risk_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_consumption'"
            ).fetchone()
            if (
                retry_of is not None
                and risk_table
                and db.execute(
                    "SELECT 1 FROM risk_consumption WHERE linked_operation=?",
                    (retry_of,),
                ).fetchone()
            ):
                raise SubmissionError("Linked operation permits only one attempt")
            if risk_authorization_id is not None and retry_of is not None:
                raise SubmissionError("Risk operation is not a technical retry")
            association = db.execute(
                "SELECT candidate_id,root_operation_id FROM candidate_eligibility WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            candidate_id = association[0] if association else None
            if (
                association
                and risk_authorization_id is None
                and association[1] != retry_of
            ):
                raise SubmissionError("Candidate root mismatch")
            prior = db.execute(
                "SELECT o.task_id,o.revision,o.digest,o.attempt_id,a.upstream_status FROM operation o JOIN attempt a ON a.attempt_id=o.attempt_id WHERE o.operation_id=?",
                (operation_id,),
            ).fetchone()
            if prior:
                if prior[:3] != (
                    task.task_id,
                    revision.revision,
                    snapshot.execution_digest,
                ):
                    raise SubmissionError(
                        "Operation identifier conflicts with execution"
                    )
                if (
                    retry_of is not None
                    and db.execute(
                        "SELECT 1 FROM operation_attempt WHERE operation_id=? AND sequence IN (2,3,4,5,6) AND attempt_id=?",
                        (retry_of, prior[3]),
                    ).fetchone()
                    is None
                ):
                    raise SubmissionError("Retry identity conflicts with operation")
                if require_retry_scope:
                    from . import retry_scope

                    if not retry_scope.has_consumed_reservation(
                        db, operation_id, retry_of, prior[3]
                    ):
                        raise SubmissionError("Retry risk reservation is required")
                if (
                    risk_authorization_id is not None
                    and db.execute(
                        "SELECT 1 FROM risk_consumption WHERE authorization_id=? AND linked_operation=? AND linked_attempt=? AND execution_digest=?",
                        (
                            risk_authorization_id,
                            operation_id,
                            prior[3],
                            snapshot.execution_digest,
                        ),
                    ).fetchone()
                    is None
                ):
                    raise SubmissionError("Risk consumption identity mismatch")
                return OfflineSubmission(
                    operation_id,
                    prior[3],
                    "unknown" if prior[4] == "submission_uncertain" else prior[4],
                    replayed=True,
                    preview=read_preview(db, prior[3]),
                )
            # Until linked retry and explicit new-risk authorization exist, a
            # fresh ID must not reset the budget for this execution snapshot.
            risk_evidence = None
            if risk_authorization_id is not None:
                risk_evidence = risk_reservation.verify_consumption(
                    db, risk_authorization_id, operation_id, snapshot
                )
                if db.execute(
                    "SELECT 1 FROM risk_consumption WHERE authorization_id=?",
                    (risk_authorization_id,),
                ).fetchone():
                    raise SubmissionError("Risk authorization already consumed")
                if not _verify_linked_fee(db, risk_evidence, snapshot, confirmation_id):
                    raise SubmissionError("Independent fee authorization unverified")
                if association and association != (
                    risk_evidence.source_candidate,
                    risk_evidence.source_root,
                ):
                    raise SubmissionError("Risk source candidate mismatch")
                if association:
                    from . import candidate_preflight

                    source_candidate = candidate_preflight.read_candidate(
                        service.store, association[0]
                    )
                    if (
                        source_candidate.snapshot != snapshot
                        or not candidate_preflight._verify_candidate_capability(
                            db, source_candidate
                        )
                        or not candidate_preflight._verify_business(
                            db, source_candidate
                        )
                    ):
                        raise SubmissionError("Risk candidate evidence unverified")
            if (
                retry_of is None
                and risk_evidence is None
                and db.execute(
                    "SELECT 1 FROM operation WHERE task_id=? AND revision=? AND digest=?",
                    (task.task_id, revision.revision, snapshot.execution_digest),
                ).fetchone()
            ):
                raise SubmissionError("Execution already has a logical operation")
            service._current(db, task, revision)
            service._current_review(db, snapshot)
            _check_mock_source_file(db, service, task, revision)
            scope_digest = service._validate(
                task,
                revision,
                refs,
                prompt,
                snapshot,
                confirmation,
                operation_id,
                scope,
                candidate_id=candidate_id,
            )
            persisted = read_prepared(service.store, task.task_id, revision.revision)
            if (
                candidate_id is None
                and persisted["snapshot_payload"] != snapshot.to_payload()
            ):
                raise SubmissionError("Execution snapshot changed")
            preview = _check_business_gate(db, service, task, revision)
            from . import retry_scope

            scoped_retry = retry_scope.verify_reserved(
                db, operation_id, retry_of, snapshot
            )
            if (
                require_retry_scope or retry_scope.requires_reservation(db, retry_of)
            ) and not scoped_retry:
                raise SubmissionError("Retry risk reservation is required")
            if require_first_authorizations:
                if (
                    retry_of is not None
                    or risk_authorization_id is not None
                    or candidate_id is not None
                ):
                    raise SubmissionError("Only first submissions are supported")
                from .first_authorization import check

                authorization_ids = check(db, operation_id, snapshot)
            if not team_live and (
                not _verify_business_choices(snapshot)
                or not _verify_authorization(snapshot)
            ):
                raise SubmissionError(
                    "Business confirmation or authorization is unverified"
                )
            base_request = build_request(service.store, task.task_id, revision.revision)
            if candidate_id is not None:
                from dataclasses import replace

                base_request = replace(
                    base_request,
                    model=snapshot.model,
                    endpoint=snapshot.endpoint,
                    group=snapshot.group,
                )
            request = serialize_request(base_request)
            row = db.execute(
                "SELECT confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at,invalidated,consumed FROM local_eligibility WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            expected = (
                confirmation_id,
                task.task_id,
                revision.revision,
                revision.input_digest,
                prompt.prompt_revision,
                prompt.prompt_digest,
                snapshot.execution_digest,
                scope_digest,
            )
            if (
                row is None
                or row[:8] != expected
                or row[8] <= _clock_now()
                or row[9]
                or row[10]
            ):
                raise SubmissionError(
                    "Local eligibility is missing, changed or consumed"
                )
            previous_attempt = None
            previous_count = 0
            wait_table = "retry_wait"
            if candidate_id is not None and risk_evidence is None:
                from ecommerce_product_image_creator import candidate_preflight
                from ecommerce_product_image_creator.execution_candidates import (
                    read_candidate,
                )

                if not retry_of or scope.max_attempts != 3:
                    raise SubmissionError("Candidate retry scope invalid")
                candidate = read_candidate(service.store, candidate_id)
                assessment = candidate_preflight.preflight(
                    service, retry_of, candidate_id
                )
                if (
                    candidate.snapshot != snapshot
                    or not assessment.eligible
                    or not assessment.budget
                    or assessment.budget.total_used not in (3, 4, 5)
                    or assessment.budget.combination_used
                    != assessment.budget.total_used - 3
                ):
                    raise SubmissionError("Candidate retry conditions unverified")
                if not _verify_cross_authorization(
                    db, candidate, operation_id, confirmation_id
                ):
                    raise SubmissionError("Candidate attempt authorization unverified")
                chain = db.execute(
                    "SELECT attempt_id FROM operation_attempt WHERE operation_id=? ORDER BY sequence",
                    (retry_of,),
                ).fetchall()
                if len(chain) != assessment.budget.total_used:
                    raise SubmissionError("Candidate retry history invalid")
                previous_attempt = chain[-1][0]
                previous_count = assessment.budget.total_used
                if previous_count > 3 and db.execute(
                    "SELECT candidate_id,root_operation_id,combination_sequence FROM candidate_attempt WHERE attempt_id=?",
                    (previous_attempt,),
                ).fetchone() != (candidate_id, retry_of, previous_count - 3):
                    raise SubmissionError("Candidate retry history changed")
                wait_table = "retry_wait_attempt"
                wait = db.execute(
                    "SELECT started,deadline,observed FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
                    (retry_of, previous_attempt),
                ).fetchone()
                now = _clock_now()
                if not wait or now < max(wait):
                    raise SubmissionError("Candidate retry wait unavailable")
                db.execute(
                    "UPDATE retry_wait_attempt SET observed=? WHERE operation_id=? AND attempt_id=?",
                    (now, retry_of, previous_attempt),
                )
            elif retry_of is not None:
                if retry_of == operation_id or scope.max_attempts < 2:
                    raise SubmissionError("Retry scope is invalid")
                root = db.execute(
                    "SELECT task_id,revision,digest,attempt_id,attempt_count FROM operation WHERE operation_id=?",
                    (retry_of,),
                ).fetchone()
                chain = db.execute(
                    "SELECT c.sequence,c.attempt_id,c.previous_attempt_id,c.model,c.endpoint,c.execution_digest,"
                    "a.previous_attempt_id,a.upstream_status,a.task_id,a.revision,o.task_id,o.revision,o.digest "
                    "FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id "
                    "JOIN operation o ON o.attempt_id=c.attempt_id WHERE c.operation_id=? ORDER BY c.sequence",
                    (retry_of,),
                ).fetchall()
                if not root or root[:3] != (
                    task.task_id,
                    revision.revision,
                    snapshot.execution_digest,
                ):
                    raise SubmissionError("Retry root does not match the execution")
                previous_count = root[4]
                if (
                    previous_count not in (1, 2)
                    or scope.max_attempts <= previous_count
                    or len(chain) != previous_count
                ):
                    raise SubmissionError("Retry budget is unknown or exhausted")
                prior_id = None
                for index, item in enumerate(chain, 1):
                    evidence_phase = db.execute(
                        "SELECT phase FROM attempt_transport_evidence WHERE attempt_id=?",
                        (item[1],),
                    ).fetchone()
                    if evidence_phase == (
                        "pre_send_failure",
                    ) and not verified_pre_send(
                        db,
                        retry_of,
                        item[1],
                        task.task_id,
                        revision.revision,
                        snapshot.execution_digest,
                    ):
                        raise SubmissionError(
                            "Transport evidence conflicts with attempt history"
                        )
                    if (
                        item[0] != index
                        or item[2] != prior_id
                        or item[3:6]
                        != (
                            snapshot.model,
                            snapshot.endpoint,
                            snapshot.execution_digest,
                        )
                        or item[6] != prior_id
                        or (
                            item[7] != "failed"
                            and verified_recoverability(
                                db,
                                retry_of,
                                item[1],
                                task.task_id,
                                revision.revision,
                                snapshot.execution_digest,
                            )
                            != "transient_local"
                        )
                        or item[8:10] != (task.task_id, revision.revision)
                        or item[10:13]
                        != (task.task_id, revision.revision, snapshot.execution_digest)
                    ):
                        raise SubmissionError("Retry chain is incomplete or unsafe")
                    prior_id = item[1]
                if chain[0][1] != root[3]:
                    raise SubmissionError("Retry root attempt does not match")
                if not isinstance(prior_id, str) or not prior_id:
                    raise SubmissionError("Retry predecessor is invalid")
                previous_attempt = prior_id
                if db.execute(
                    "SELECT COUNT(*),SUM(count) FROM attempt_counter WHERE operation_id=?",
                    (retry_of,),
                ).fetchone() != (1, previous_count) or db.execute(
                    "SELECT count FROM attempt_counter WHERE operation_id=? AND model=? AND endpoint=?",
                    (retry_of, snapshot.model, snapshot.endpoint),
                ).fetchone() != (previous_count,):
                    raise SubmissionError("Retry history is incomplete or inconsistent")
                wait_table = (
                    "retry_wait" if previous_count == 1 else "retry_wait_attempt"
                )
                wait = db.execute(
                    "SELECT attempt_id,started,deadline,observed FROM retry_wait WHERE operation_id=?"
                    if previous_count == 1
                    else "SELECT attempt_id,started,deadline,observed FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
                    (retry_of,)
                    if previous_count == 1
                    else (retry_of, previous_attempt),
                ).fetchone()
                now = _clock_now()
                if (
                    not wait
                    or wait[0] != previous_attempt
                    or now < wait[1]
                    or now < wait[2]
                    or now < wait[3]
                ):
                    raise SubmissionError(
                        "Retry waiting period is incomplete or uncertain"
                    )
                if not _verify_retry_evidence(
                    db,
                    retry_of,
                    previous_attempt,
                    snapshot,
                    confirmation_id,
                ) or not _verify_retry_authorization(
                    db,
                    retry_of,
                    previous_attempt,
                    snapshot,
                    confirmation_id,
                ):
                    raise SubmissionError(
                        "Retry evidence or authorization is unverified"
                    )
                db.execute(
                    "UPDATE retry_wait SET observed=? WHERE operation_id=?"
                    if previous_count == 1
                    else "UPDATE retry_wait_attempt SET observed=? WHERE operation_id=? AND attempt_id=?",
                    (now, retry_of)
                    if previous_count == 1
                    else (now, retry_of, previous_attempt),
                )
            attempt_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO attempt(attempt_id,task_id,revision,upstream_status,delivery_status,previous_attempt_id) VALUES(?,?,?,?,?,?)",
                (
                    attempt_id,
                    task.task_id,
                    revision.revision,
                    "submission_uncertain",
                    "not_available",
                    previous_attempt,
                ),
            )
            db.execute(
                "INSERT INTO operation VALUES(?,?,?,?,?,?)",
                (
                    operation_id,
                    task.task_id,
                    revision.revision,
                    snapshot.execution_digest,
                    attempt_id,
                    1,
                ),
            )
            if scoped_retry:
                changed = db.execute(
                    "UPDATE retry_scope_reservation SET attempt_id=? WHERE operation_id=? AND attempt_id IS NULL",
                    (attempt_id, operation_id),
                )
                if changed.rowcount != 1:
                    raise SubmissionError("Retry risk reservation changed")
            # First attempt consumes the combination budget in the same local
            # transaction as the intent and eligibility consumption.
            if retry_of is None:
                db.execute(
                    "INSERT INTO attempt_counter(operation_id,model,endpoint,count) VALUES(?,?,?,1)",
                    (operation_id, snapshot.model, snapshot.endpoint),
                )
                db.execute(
                    "INSERT INTO operation_attempt VALUES(?,1,?,NULL,?,?,?)",
                    (
                        operation_id,
                        attempt_id,
                        snapshot.model,
                        snapshot.endpoint,
                        snapshot.execution_digest,
                    ),
                )
                if risk_evidence is not None:
                    linked_candidate = uuid.uuid4().hex
                    db.execute(
                        "INSERT INTO execution_candidate VALUES(?,?,?,?,?,?,?,?)",
                        (
                            linked_candidate,
                            operation_id,
                            task.task_id,
                            revision.revision,
                            None,
                            snapshot.to_payload(),
                            snapshot.execution_digest,
                            _clock_now(),
                        ),
                    )
                    db.execute(
                        "INSERT INTO risk_consumption VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            risk_authorization_id,
                            risk_evidence.source_root,
                            risk_evidence.source_candidate,
                            risk_evidence.source_attempt,
                            risk_evidence.source_digest,
                            operation_id,
                            attempt_id,
                            linked_candidate,
                            snapshot.execution_digest,
                            _clock_now(),
                        ),
                    )
            elif candidate_id is not None:
                if db.execute(
                    "SELECT COUNT(*),SUM(count) FROM attempt_counter WHERE operation_id=?",
                    (retry_of,),
                ).fetchone() != (1 if previous_count == 3 else 2, previous_count):
                    raise SubmissionError("Candidate budget changed")
                if previous_count == 3:
                    db.execute(
                        "INSERT INTO attempt_counter VALUES(?,?,?,1)",
                        (retry_of, snapshot.model, snapshot.endpoint),
                    )
                elif (
                    db.execute(
                        "UPDATE attempt_counter SET count=count+1 WHERE operation_id=? AND model=? AND endpoint=? AND count=?",
                        (
                            retry_of,
                            snapshot.model,
                            snapshot.endpoint,
                            previous_count - 3,
                        ),
                    ).rowcount
                    != 1
                ):
                    raise SubmissionError("Candidate combination budget changed")
                if (
                    db.execute(
                        "UPDATE operation SET attempt_count=? WHERE operation_id=? AND attempt_count=?",
                        (previous_count + 1, retry_of, previous_count),
                    ).rowcount
                    != 1
                ):
                    raise SubmissionError("Root budget changed")
                db.execute(
                    "INSERT INTO operation_attempt VALUES(?,?,?,?,?,?,?)",
                    (
                        retry_of,
                        previous_count + 1,
                        attempt_id,
                        previous_attempt,
                        snapshot.model,
                        snapshot.endpoint,
                        snapshot.execution_digest,
                    ),
                )
                db.execute(
                    "INSERT INTO candidate_attempt VALUES(?,?,?,?)",
                    (attempt_id, candidate_id, retry_of, previous_count - 2),
                )
            else:
                updated_budget = db.execute(
                    "UPDATE attempt_counter SET count=? WHERE operation_id=? AND model=? AND endpoint=? AND count=?",
                    (
                        previous_count + 1,
                        retry_of,
                        snapshot.model,
                        snapshot.endpoint,
                        previous_count,
                    ),
                )
                if updated_budget.rowcount != 1:
                    raise SubmissionError("Retry budget changed")
                if (
                    db.execute(
                        "UPDATE operation SET attempt_count=? WHERE operation_id=? AND attempt_count=?",
                        (previous_count + 1, retry_of, previous_count),
                    ).rowcount
                    != 1
                ):
                    raise SubmissionError("Retry operation changed")
                db.execute(
                    "INSERT INTO operation_attempt VALUES(?,?,?,?,?,?,?)",
                    (
                        retry_of,
                        previous_count + 1,
                        attempt_id,
                        previous_attempt,
                        snapshot.model,
                        snapshot.endpoint,
                        snapshot.execution_digest,
                    ),
                )
            if preview is not None:
                persist_preview(
                    db,
                    operation_id,
                    attempt_id,
                    {**preview, "operation_id": operation_id, "attempt_id": attempt_id},
                )
            if require_first_authorizations:
                from .first_authorization import consume

                if authorization_ids is None:
                    raise SubmissionError("Independent authorization is unavailable")
                consume(db, operation_id, attempt_id, authorization_ids)
            updated = db.execute(
                "UPDATE local_eligibility SET consumed=1 WHERE operation_id=? AND consumed=0 AND invalidated=0",
                (operation_id,),
            )
            if updated.rowcount != 1:
                raise SubmissionError("Local eligibility was already consumed")
            _before_intent_commit()
    except SubmissionError:
        raise
    except EligibilityError as error:
        if str(error) == "Form review is no longer eligible":
            raise SubmissionError("FORM_REVIEW_UNVERIFIED_OR_INVALID") from None
        raise SubmissionError("Execution cannot be safely prepared") from None
    except (sqlite3.Error, ValueError, OSError, MemoryError):
        raise SubmissionError("Execution cannot be safely prepared") from None
    entered = False
    dispatch_started = False
    try:
        _after_intent_recorded()
        try:
            _pre_dispatch_validate(request)
        except Exception as exc:  # noqa: BLE001  # No transport boundary has been entered.
            _record_transport_boundary(
                service,
                operation_id,
                attempt_id,
                "pre_send_failure",
                category=faults.local_failure_category(exc),
            )
            return OfflineSubmission(
                operation_id,
                attempt_id,
                "unknown",
                preview=preview
                or {
                    "status": "unknown",
                    "structure_verified": False,
                    "current_decision": "unknown",
                },
            )
        # Commit before any possible transport call. A crash after this point is unknown.
        _record_transport_boundary(service, operation_id, attempt_id, "entered")
        entered = True
        # Hold the local write lock across the isolated fake dispatch. Revocation
        # either precedes this check or follows the dispatch boundary; this is
        # not a transaction with any remote service.
        with closing(service.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            service._current(db, task, revision)
            service._current_review(db, snapshot)
            _check_mock_source_file(db, service, task, revision)
            if candidate_id is None and (
                read_prepared(service.store, task.task_id, revision.revision)[
                    "snapshot_payload"
                ]
                != snapshot.to_payload()
            ):
                raise SubmissionError("Execution snapshot changed")
            current_preview = _check_business_gate(db, service, task, revision)
            if scoped_retry and not retry_scope.verify_reserved(
                db, operation_id, retry_of, snapshot, attempt_id
            ):
                raise SubmissionError("Retry scope changed before dispatch")
            if (
                require_retry_scope or retry_scope.requires_reservation(db, retry_of)
            ) and not scoped_retry:
                raise SubmissionError("Retry risk reservation is required")
            if current_preview != preview:
                raise SubmissionError("Preview decision changed")
            if require_first_authorizations:
                from .first_authorization import check

                check(db, operation_id, snapshot, attempt_id=attempt_id)
            if not team_live and (
                not _verify_business_choices(snapshot)
                or not _verify_authorization(snapshot)
            ):
                raise SubmissionError(
                    "Business confirmation or authorization is unverified"
                )
            if risk_authorization_id is not None:
                current_risk = risk_reservation.verify_consumption(
                    db, risk_authorization_id, operation_id, snapshot
                )
                current_eligibility = db.execute(
                    "SELECT confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at,invalidated,consumed FROM local_eligibility WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                consumption = db.execute(
                    "SELECT linked_operation,linked_attempt,execution_digest FROM risk_consumption WHERE authorization_id=?",
                    (risk_authorization_id,),
                ).fetchone()
                if (
                    not _verify_linked_fee(db, current_risk, snapshot, confirmation_id)
                    or not current_eligibility
                    or current_eligibility[:8] != expected
                    or current_eligibility[8] <= _clock_now()
                    or current_eligibility[9:] != (0, 1)
                    or consumption
                    != (operation_id, attempt_id, snapshot.execution_digest)
                ):
                    raise SubmissionError("Independent fee authorization changed")
                if candidate_id is not None:
                    from . import candidate_preflight

                    source_candidate = candidate_preflight.read_candidate(
                        service.store, candidate_id
                    )
                    current_association = db.execute(
                        "SELECT candidate_id,root_operation_id FROM candidate_eligibility WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if (
                        current_association
                        != (current_risk.source_candidate, current_risk.source_root)
                        or source_candidate.snapshot != snapshot
                        or not candidate_preflight._verify_candidate_capability(
                            db, source_candidate
                        )
                        or not candidate_preflight._verify_business(
                            db, source_candidate
                        )
                    ):
                        raise SubmissionError("Risk candidate evidence changed")
            if candidate_id is not None and risk_authorization_id is None:
                from ecommerce_product_image_creator import candidate_preflight
                from ecommerce_product_image_creator.execution_candidates import (
                    read_candidate,
                )

                candidate = read_candidate(service.store, candidate_id)
                linked = db.execute(
                    "SELECT candidate_id,root_operation_id FROM candidate_eligibility WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                eligibility = db.execute(
                    "SELECT execution_digest,expires_at,invalidated,consumed FROM local_eligibility WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                wait = db.execute(
                    "SELECT started,deadline,observed FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
                    (retry_of, previous_attempt),
                ).fetchone()
                original = db.execute(
                    "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
                    (previous_attempt,),
                ).fetchone()
                view = candidate_preflight.budget_view(
                    service.store, retry_of or "", candidate_id
                )
                previous_rows = db.execute(
                    "SELECT c.attempt_id,c.execution_digest FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id WHERE c.operation_id=? AND c.sequence<=? ORDER BY c.sequence",
                    (retry_of, previous_count),
                ).fetchall()
                if (
                    candidate.snapshot != snapshot
                    or linked != (candidate_id, retry_of)
                    or not eligibility
                    or eligibility[0] != snapshot.execution_digest
                    or eligibility[1] <= _clock_now()
                    or eligibility[2:] != (0, 1)
                    or not wait
                    or _clock_now() < max(wait)
                    or not original
                    or not view.complete
                    or (view.total_used, view.source_used, view.combination_used)
                    != (previous_count + 1, 3, previous_count - 2)
                    or len(previous_rows) != previous_count
                    or any(
                        candidate_preflight.verified_chain_failure(
                            db, retry_of or "", candidate, prior_id, prior_digest
                        )
                        not in {"pre_send", "remote_terminal"}
                        for prior_id, prior_digest in previous_rows
                    )
                    or not candidate_preflight._verify_candidate_authorization(
                        db, candidate
                    )
                    or not _verify_cross_authorization(
                        db, candidate, operation_id, confirmation_id
                    )
                    or not candidate_preflight._verify_candidate_capability(
                        db, candidate
                    )
                    or not candidate_preflight._verify_business(db, candidate)
                ):
                    raise SubmissionError("Candidate evidence changed before dispatch")
            if (
                retry_of is not None
                and candidate_id is None
                and (
                    previous_attempt is None
                    or not _verify_retry_evidence(
                        db,
                        retry_of,
                        previous_attempt,
                        snapshot,
                        confirmation_id,
                    )
                    or not _verify_retry_authorization(
                        db,
                        retry_of,
                        previous_attempt,
                        snapshot,
                        confirmation_id,
                    )
                )
            ):
                raise SubmissionError("Retry evidence or authorization changed")
            if retry_of is not None and candidate_id is None:
                latest_eligibility = db.execute(
                    "SELECT confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at,invalidated,consumed FROM local_eligibility WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                wait = db.execute(
                    "SELECT attempt_id,started,deadline,observed FROM retry_wait WHERE operation_id=?"
                    if wait_table == "retry_wait"
                    else "SELECT attempt_id,started,deadline,observed FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
                    (retry_of,)
                    if wait_table == "retry_wait"
                    else (retry_of, previous_attempt),
                ).fetchone()
                previous_status = db.execute(
                    "SELECT upstream_status FROM attempt WHERE attempt_id=?",
                    (previous_attempt,),
                ).fetchone()
                now = _clock_now()
                if (
                    not latest_eligibility
                    or latest_eligibility[:8] != expected
                    or latest_eligibility[8] <= now
                    or latest_eligibility[9]
                    or latest_eligibility[10] != 1
                    or not wait
                    or wait[0] != previous_attempt
                    or now < max(wait[1:])
                    or (
                        previous_status != ("failed",)
                        and verified_recoverability(
                            db,
                            retry_of,
                            previous_attempt or "",
                            task.task_id,
                            revision.revision,
                            snapshot.execution_digest,
                        )
                        != "transient_local"
                    )
                ):
                    raise SubmissionError(
                        "Retry eligibility, wait, or original status changed"
                    )
            _before_offline_dispatch()
            dispatch_started = True
            if team_live:
                from .team_transport import TeamRequest, TeamSettings, send_once

                response = send_once(
                    TeamRequest(
                        operation_id,
                        attempt_id,
                        snapshot.execution_digest,
                        snapshot.model,
                        request,
                        db,
                    ),
                    TeamSettings.from_environment(),
                )
                _after_team_response_received()
            else:
                response = _offline_dispatch(request)
        if (
            not isinstance(response, ProviderResult)
            or response.state
            not in {"accepted", "pending", "running", "failed", "succeeded"}
            or not response.task_id
        ):
            state, upstream_id = "unknown", None
        else:
            state = {"pending": "accepted"}.get(response.state, response.state)
            upstream_id = response.task_id
    except Exception:  # noqa: BLE001  # Any transport failure may have sent the request.
        if risk_authorization_id is not None and not dispatch_started:
            try:
                from .send_evidence import _record_linked_stop

                with closing(service.store._connect()) as stop_db, stop_db:
                    stop_db.execute("BEGIN IMMEDIATE")
                    _record_linked_stop(stop_db, operation_id, attempt_id, entered)
            except (sqlite3.Error, ValueError, OSError):
                # Failure to record proof never creates proof of non-dispatch.
                pass
        state, upstream_id = "unknown", None
    try:
        with closing(service.store._connect()) as db, db:
            changed = db.execute(
                "UPDATE attempt SET upstream_status=?,upstream_id=? WHERE attempt_id=? AND upstream_status='submission_uncertain'",
                (state, upstream_id, attempt_id),
            )
            if changed.rowcount != 1:
                return OfflineSubmission(
                    operation_id,
                    attempt_id,
                    "unknown",
                    preview=read_preview(db, attempt_id),
                )
    except sqlite3.Error:
        return OfflineSubmission(operation_id, attempt_id, "unknown")
    with closing(service.store._connect()) as db:
        return OfflineSubmission(
            operation_id, attempt_id, state, preview=read_preview(db, attempt_id)
        )
