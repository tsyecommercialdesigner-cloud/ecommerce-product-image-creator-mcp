"""Fail-closed offline retry coordination; no real transport or authorization."""

import math
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from ecommerce_product_image_creator import faults, offline_submission
from ecommerce_product_image_creator.domain import (
    Confirmation,
    ExecutionSnapshot,
    PromptRevision,
    Reference,
    Revision,
    Task,
)
from ecommerce_product_image_creator.send_evidence import (
    safe_failure_kind,
    verified_pre_send,
    verified_recoverability,
)
from ecommerce_product_image_creator.tasks import LocalEligibility, OperationScope


@dataclass(frozen=True)
class RetryObservation:
    """Decoded terminal response from an independently verified source."""

    task_id: str
    payload: dict[str, Any]
    failure_status: int
    retry_after: str | None
    observed_at: float


@dataclass(frozen=True)
class SchedulerResult:
    action: str
    reason: str
    result: offline_submission.OfflineSubmission | None = None


def _load_observation(
    db: sqlite3.Connection, root: str, attempt: str
) -> RetryObservation | None:
    """No production evidence adapter has been verified."""
    return None


def _combination_verified(snapshot: ExecutionSnapshot) -> bool:
    """Offline fixtures do not prove gateway compatibility."""
    return False


class OfflineRetryScheduler:
    """One explicit step only; no loop, sleep, network or model switching."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock

    def step(
        self,
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
        root: str,
        candidate_id: str | None = None,
        evaluate_only: bool = False,
        _require_retry_scope: bool = False,
    ) -> SchedulerResult:
        """Inspect persisted facts, then delegate any reservation to submit_offline."""
        with closing(service.store._connect()) as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_consumption'"
            ).fetchone()
            if (
                exists
                and db.execute(
                    "SELECT 1 FROM risk_consumption WHERE linked_operation=?", (root,)
                ).fetchone()
            ):
                return SchedulerResult("stop", "linked_risk_one_attempt_limit")
        if candidate_id is not None:
            return self._candidate_step(
                service,
                task,
                revision,
                refs,
                prompt,
                snapshot,
                confirmation,
                operation_id,
                scope,
                root,
                candidate_id,
                evaluate_only,
                _require_retry_scope,
            )
        if snapshot.digest_version != 2 or not confirmation.confirmation_id:
            return SchedulerResult("stop", "invalid_context")
        attempt: str | None = None
        observation: RetryObservation | None = None
        try:
            now = self._clock()
            if not math.isfinite(now):
                return SchedulerResult("stop", "invalid_clock")
            with closing(service.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute(
                    "SELECT a.attempt_id FROM operation o JOIN attempt a ON a.attempt_id=o.attempt_id JOIN operation_attempt c ON c.attempt_id=a.attempt_id WHERE o.operation_id=? AND c.operation_id=?",
                    (operation_id, root),
                ).fetchone()
                if prior:
                    # Existing request is read-only and submit_offline checks identity.
                    pass
                else:
                    if service.store.attempt_chain_status(root) not in {
                        "initial_recorded",
                        "second_recorded",
                    }:
                        return SchedulerResult("stop", "history_unknown_or_exhausted")
                    row = db.execute(
                        "SELECT o.task_id,o.revision,o.digest,o.attempt_count,c.attempt_id,c.model,c.endpoint,a.upstream_status,a.upstream_id FROM operation o JOIN operation_attempt c ON c.operation_id=o.operation_id AND c.sequence=o.attempt_count JOIN attempt a ON a.attempt_id=c.attempt_id WHERE o.operation_id=?",
                        (root,),
                    ).fetchone()
                    if (
                        not row
                        or row[:3]
                        != (task.task_id, revision.revision, snapshot.execution_digest)
                        or row[5:7] != (snapshot.model, snapshot.endpoint)
                    ):
                        return SchedulerResult("stop", "execution_mismatch")
                    attempt, state, upstream_id = row[4], row[7], row[8]
                    if not isinstance(attempt, str) or not attempt:
                        return SchedulerResult("stop", "attempt_identity_invalid")
                    pre_send = verified_pre_send(
                        db,
                        root,
                        attempt,
                        task.task_id,
                        revision.revision,
                        snapshot.execution_digest,
                    )
                    phase = db.execute(
                        "SELECT phase FROM attempt_transport_evidence WHERE attempt_id=?",
                        (attempt,),
                    ).fetchone()
                    if phase == ("pre_send_failure",) and not pre_send:
                        return SchedulerResult("stop", "conflicting_transport_evidence")
                    if pre_send:
                        if (
                            safe_failure_kind(
                                db,
                                root,
                                attempt,
                                task.task_id,
                                revision.revision,
                                snapshot.execution_digest,
                            )
                            != "pre_send"
                        ):
                            return SchedulerResult(
                                "stop", "recoverability_unverified_or_deterministic"
                            )
                        recoverability = verified_recoverability(
                            db,
                            root,
                            attempt,
                            task.task_id,
                            revision.revision,
                            snapshot.execution_digest,
                        )
                        if recoverability != "transient_local":
                            return SchedulerResult(
                                "stop", "recoverability_unverified_or_deterministic"
                            )
                        proof = db.execute(
                            "SELECT recorded_at FROM attempt_transport_evidence WHERE attempt_id=?",
                            (attempt,),
                        ).fetchone()
                        if not proof:
                            return SchedulerResult(
                                "stop", "failure_evidence_unavailable"
                            )
                        observation = RetryObservation("", {}, 0, None, proof[0])
                        classified = faults.classify_transport(
                            "not_sent", evidence="local_guard"
                        )
                    elif state in {
                        "unknown",
                        "submission_uncertain",
                        "accepted",
                        "pending",
                        "running",
                    }:
                        classified = faults.classify_transport(
                            "read_timeout", evidence="read_timeout", task_id=upstream_id
                        )
                        return SchedulerResult(
                            classified.next_action, "original_operation_unresolved"
                        )
                    else:
                        if state != "failed" or not upstream_id:
                            return SchedulerResult("stop", "original_not_retryable")
                        observation = _load_observation(db, root, attempt)
                        if observation is None or observation.task_id != upstream_id:
                            return SchedulerResult(
                                "stop", "failure_evidence_unavailable"
                            )
                        classified = faults.classify_query(
                            200, observation.payload, upstream_id
                        )
                        if classified.state != "failed":
                            return SchedulerResult(
                                "stop", "failure_evidence_inconsistent"
                            )
                        if not (
                            observation.failure_status == 429
                            or 500 <= observation.failure_status <= 599
                        ):
                            return SchedulerResult("stop", "unsupported_failure")
                        delay = (
                            faults.parse_retry_after(
                                observation.retry_after,
                                datetime.fromtimestamp(observation.observed_at, UTC),
                            )
                            if observation.failure_status == 429
                            else None
                        )
                        classified = replace(
                            classified,
                            http_status=observation.failure_status,
                            retry_after_seconds=delay,
                        )
                    service._current(db, task, revision)
                    service._current_review(db, snapshot)
                    scope_digest = service._validate(
                        task,
                        revision,
                        refs,
                        prompt,
                        snapshot,
                        confirmation,
                        operation_id,
                        scope,
                    )
                    offline_submission._check_business_gate(db, service, task, revision)
                    eligibility = db.execute(
                        "SELECT confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at,invalidated,consumed FROM local_eligibility WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    expected = (
                        confirmation.confirmation_id,
                        task.task_id,
                        revision.revision,
                        revision.input_digest,
                        prompt.prompt_revision,
                        prompt.prompt_digest,
                        snapshot.execution_digest,
                        scope_digest,
                    )
                    if (
                        not eligibility
                        or eligibility[:8] != expected
                        or eligibility[8] <= now
                        or eligibility[9]
                        or eligibility[10]
                    ):
                        return SchedulerResult("stop", "eligibility_unavailable")
                    if (
                        scope.max_attempts <= row[3]
                        or not offline_submission._verify_business_choices(snapshot)
                        or not offline_submission._verify_authorization(snapshot)
                    ):
                        return SchedulerResult(
                            "stop", "business_or_authorization_unverified"
                        )
                    safe = offline_submission._verify_retry_evidence(
                        db, root, attempt, snapshot, confirmation.confirmation_id
                    )
                    authorized = offline_submission._verify_retry_authorization(
                        db, root, attempt, snapshot, confirmation.confirmation_id
                    )
                    compatible = _combination_verified(snapshot)
                    facts = faults.RetryFacts(
                        root,
                        snapshot.model,
                        snapshot.endpoint,
                        row[3],
                        row[3],
                        safe,
                        authorized,
                        compatible,
                        True,
                        "transient_local" if pre_send else "unknown",
                    )
                    candidate = faults.evaluate_retry_candidate(classified, facts)
                    if candidate.outcome != "candidate_only":
                        return SchedulerResult("stop", candidate.reason)
                    # The outer transaction ends before independently persisted wait inspection.
            if not prior:
                if not attempt or observation is None:
                    return SchedulerResult("stop", "failure_evidence_unavailable")
                wait_status = service.store.retry_wait_status(
                    root, now, attempt_id=attempt
                )
                if wait_status == "unknown":
                    service.store.record_retry_wait(
                        root,
                        attempt,
                        observation.observed_at,
                        observation.retry_after,
                        observation.failure_status,
                    )
                    wait_status = service.store.retry_wait_status(
                        root, now, attempt_id=attempt
                    )
                if wait_status == "waiting":
                    return SchedulerResult("waiting", "deadline_not_reached")
                if wait_status != "elapsed_recheck_required":
                    return SchedulerResult("stop", "wait_unverified")
            if not prior and evaluate_only:
                return SchedulerResult(
                    "retry_candidate", "transaction_revalidation_required"
                )
            result = offline_submission.submit_offline(
                service,
                task,
                revision,
                refs,
                prompt,
                snapshot,
                confirmation,
                operation_id,
                scope,
                retry_of=root,
                require_retry_scope=_require_retry_scope,
            )
            return SchedulerResult(
                "existing" if prior else "submitted", "offline_result", result
            )
        except (ValueError, OSError, sqlite3.Error, OverflowError, TypeError):
            return SchedulerResult("stop", "local_checks_failed")

    def _candidate_step(
        self,
        service: LocalEligibility,
        task: Task,
        revision: Revision,
        refs: list[Reference],
        prompt: PromptRevision,
        snapshot: ExecutionSnapshot,
        confirmation: Confirmation,
        operation_id: str,
        scope: OperationScope,
        root: str,
        candidate_id: str,
        evaluate_only: bool,
        require_retry_scope: bool,
    ) -> SchedulerResult:
        """One explicit candidate step, never an independent attempt writer."""
        from ecommerce_product_image_creator import candidate_preflight
        from ecommerce_product_image_creator.execution_candidates import (
            CandidateError,
            read_candidate,
        )

        try:
            now = self._clock()
            if not math.isfinite(now):
                return SchedulerResult("stop", "invalid_clock")
            try:
                candidate = read_candidate(service.store, candidate_id)
            except CandidateError:
                return SchedulerResult("stop", "candidate_unavailable_or_invalid")
            if candidate.root_operation_id != root or candidate.snapshot != snapshot:
                return SchedulerResult("stop", "candidate_context_mismatch")
            with closing(service.store._connect()) as db:
                existing = db.execute(
                    "SELECT c.attempt_id FROM operation o JOIN operation_attempt c ON c.attempt_id=o.attempt_id JOIN candidate_eligibility ce ON ce.operation_id=o.operation_id WHERE o.operation_id=? AND c.operation_id=? AND ce.candidate_id=? AND ce.root_operation_id=? AND o.digest=?",
                    (operation_id, root, candidate_id, root, snapshot.execution_digest),
                ).fetchone()
                latest = db.execute(
                    "SELECT a.attempt_id,a.upstream_status,a.upstream_id,c.execution_digest FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id WHERE c.operation_id=? ORDER BY c.sequence DESC LIMIT 1",
                    (root,),
                ).fetchone()
                if (
                    not existing
                    and latest
                    and latest[1]
                    in {
                        "unknown",
                        "submission_uncertain",
                        "accepted",
                        "pending",
                        "running",
                    }
                ):
                    kind = safe_failure_kind(
                        db, root, latest[0], task.task_id, revision.revision, latest[3]
                    )
                    if kind != "pre_send":
                        classified = faults.classify_transport(
                            "read_timeout", evidence="read_timeout", task_id=latest[2]
                        )
                        return SchedulerResult(
                            classified.next_action, "original_operation_unresolved"
                        )
                if not existing:
                    eligible = db.execute(
                        "SELECT l.confirmation_id,l.task_id,l.revision,l.execution_digest,l.scope_digest,l.expires_at,l.invalidated,l.consumed,ce.candidate_id,ce.root_operation_id FROM local_eligibility l JOIN candidate_eligibility ce ON ce.operation_id=l.operation_id WHERE l.operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if (
                        not eligible
                        or eligible[:5]
                        != (
                            confirmation.confirmation_id,
                            task.task_id,
                            revision.revision,
                            snapshot.execution_digest,
                            scope.digest(),
                        )
                        or eligible[5] <= now
                        or eligible[6:8] != (0, 0)
                        or eligible[8:] != (candidate_id, root)
                    ):
                        return SchedulerResult("stop", "eligibility_unavailable")
            if existing:
                result = offline_submission.submit_offline(
                    service,
                    task,
                    revision,
                    refs,
                    prompt,
                    snapshot,
                    confirmation,
                    operation_id,
                    scope,
                    retry_of=root,
                    require_retry_scope=require_retry_scope,
                )
                return SchedulerResult("existing", "offline_result", result)
            if not latest:
                return SchedulerResult("stop", "history_unavailable")
            assessment = candidate_preflight.preflight(service, root, candidate_id)
            if not assessment.eligible:
                return SchedulerResult(
                    "stop",
                    assessment.reasons[0]
                    if assessment.reasons
                    else "candidate_unavailable",
                )
            wait_status = service.store.retry_wait_status(
                root, now, attempt_id=latest[0]
            )
            if wait_status == "waiting":
                return SchedulerResult("waiting", "deadline_not_reached")
            if wait_status != "elapsed_recheck_required":
                return SchedulerResult("stop", "wait_unverified")
            if evaluate_only:
                return SchedulerResult(
                    "retry_candidate", "transaction_revalidation_required"
                )
            result = offline_submission.submit_offline(
                service,
                task,
                revision,
                refs,
                prompt,
                snapshot,
                confirmation,
                operation_id,
                scope,
                retry_of=root,
                require_retry_scope=require_retry_scope,
            )
            return SchedulerResult("submitted", "offline_result", result)
        except (ValueError, OSError, sqlite3.Error, OverflowError, TypeError):
            return SchedulerResult("stop", "local_checks_failed")


def scoped_retry_once(
    clock: Callable[[], float],
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
    root: str,
    candidate_id: str,
    host_id: str,
    cost_id: str,
) -> SchedulerResult:
    """Reserve a concrete authorized retry and execute at most one offline step.

    The legacy scheduler remains available for historical isolated workflows;
    this entry never accepts a switch that disables the scope requirement.
    """
    from . import retry_scope
    from .execution_candidates import read_candidate

    try:
        candidate = read_candidate(service.store, candidate_id)
        if candidate.root_operation_id != root or candidate.snapshot != snapshot:
            return SchedulerResult("stop", "candidate_context_mismatch")
        with closing(service.store._connect()) as db:
            existing = db.execute(
                "SELECT attempt_id FROM operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing:
                table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retry_scope_reservation'"
                ).fetchone()
                reservation = (
                    db.execute(
                        "SELECT root,candidate_id,host_id,cost_id,attempt_id FROM retry_scope_reservation WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if table
                    else None
                )
                if reservation != (root, candidate_id, host_id, cost_id, existing[0]):
                    return SchedulerResult("stop", "retry_reservation_mismatch")
        if not existing:
            retry_scope.reserve_eligibility(
                service,
                task,
                revision,
                refs,
                prompt,
                snapshot,
                confirmation,
                operation_id,
                scope,
                root=root,
                candidate_id=candidate_id,
                host_id=host_id,
                cost_id=cost_id,
            )
        return OfflineRetryScheduler(clock).step(
            service,
            task,
            revision,
            refs,
            prompt,
            snapshot,
            confirmation,
            operation_id,
            scope,
            root=root,
            candidate_id=candidate_id
            if candidate.source_candidate_id is not None
            else None,
            _require_retry_scope=True,
        )
    except (ValueError, OSError, sqlite3.Error, OverflowError, TypeError):
        return SchedulerResult("stop", "scope_or_reservation_unverified")
