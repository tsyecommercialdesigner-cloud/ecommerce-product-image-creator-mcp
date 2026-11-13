"""Read-only candidate assessment; never reserves, authorizes or sends."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass

from . import offline_submission, provider
from .domain import Revision, Task, TaskStatus
from .execution_candidates import CandidateError, ExecutionCandidate, read_candidate
from .send_evidence import safe_failure_kind
from .storage import Store
from .tasks import LocalEligibility

SUNBURST = "gpt-image-2.5-sunburst"
FLARE = "gpt-image-2.5-flare"


@dataclass(frozen=True)
class BudgetView:
    root: str
    candidate_id: str
    candidate_digest: str
    model: str
    endpoint: str
    group: str
    combination_used: int | None
    combination_max: int
    total_used: int | None
    total_max: int
    source_used: int | None
    complete: bool
    conflict: bool
    allows_evaluation: bool


@dataclass(frozen=True)
class PreflightResult:
    eligible: bool
    reasons: tuple[str, ...]
    budget: BudgetView | None
    can_send: bool = False


def budget_view(store: Store, root: str, candidate_id: str) -> BudgetView:
    """Reconcile one SQLite read snapshot; a stale result is never a reservation."""
    candidate = read_candidate(store, candidate_id)
    if candidate.root_operation_id != root:
        raise CandidateError("Candidate root mismatch")
    snap = candidate.snapshot
    complete = False
    total = source_used = combination = None
    with closing(store._connect()) as db, db:
        db.execute("BEGIN")
        operation = db.execute(
            "SELECT task_id,revision,digest,attempt_id,attempt_count FROM operation WHERE operation_id=?",
            (root,),
        ).fetchone()
        chain = db.execute(
            "SELECT c.sequence,c.attempt_id,c.previous_attempt_id,c.model,c.endpoint,c.execution_digest,a.previous_attempt_id,a.task_id,a.revision,o.task_id,o.revision,o.digest FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id JOIN operation o ON o.attempt_id=c.attempt_id WHERE c.operation_id=? ORDER BY c.sequence",
            (root,),
        ).fetchall()
        counts = db.execute(
            "SELECT model,endpoint,count FROM attempt_counter WHERE operation_id=?",
            (root,),
        ).fetchall()
        if (
            operation
            and operation[4] in (1, 2, 3)
            and len(chain) == operation[4]
            and chain[0][1] == operation[3]
        ):
            model, endpoint = chain[0][3:5]
            previous = None
            complete = counts == [(model, endpoint, operation[4])]
            for i, item in enumerate(chain, 1):
                complete = (
                    complete
                    and item[0] == i
                    and item[2] == previous
                    and item[3:6] == (model, endpoint, operation[2])
                    and item[6] == previous
                    and item[7:9] == operation[:2]
                    and item[9:12] == operation[:3]
                )
                previous = item[1]
            if complete:
                total = source_used = operation[4]
                combination = (
                    operation[4]
                    if (snap.model, snap.endpoint) == (model, endpoint)
                    else 0
                )
        elif (
            operation
            and operation[4] in (4, 5, 6)
            and len(chain) == operation[4]
            and chain[0][1] == operation[3]
        ):
            mapping = db.execute(
                "SELECT candidate_id,root_operation_id,combination_sequence FROM candidate_attempt WHERE attempt_id=?",
                (chain[3][1],),
            ).fetchone()
            previous = None
            try:
                target = (
                    read_candidate(store, mapping[0])
                    if mapping and mapping[1:] == (root, 1)
                    else None
                )
            except CandidateError:
                target = None
            complete = bool(
                target
                and sorted(counts)
                == sorted(
                    [
                        (SUNBURST, provider.ENDPOINT, 3),
                        (FLARE, provider.ENDPOINT, operation[4] - 3),
                    ]
                )
            )
            for i, item in enumerate(chain, 1):
                expected_model = SUNBURST if i <= 3 else FLARE
                expected_digest = (
                    operation[2]
                    if i <= 3
                    else (target.snapshot.execution_digest if target else "")
                )
                complete = (
                    complete
                    and item[0] == i
                    and item[2] == previous
                    and item[3:6]
                    == (expected_model, provider.ENDPOINT, expected_digest)
                    and item[6] == previous
                    and item[7:9] == operation[:2]
                    and item[9:12] == (operation[0], operation[1], expected_digest)
                )
                if i > 3:
                    association = db.execute(
                        "SELECT candidate_id,root_operation_id,combination_sequence FROM candidate_attempt WHERE attempt_id=?",
                        (item[1],),
                    ).fetchone()
                    complete = (
                        complete
                        and bool(target)
                        and association == (target.candidate_id, root, i - 3)
                    )
                previous = item[1]
            if complete:
                total, source_used = operation[4], 3
                combination = (
                    3
                    if (snap.model, snap.endpoint) == (SUNBURST, provider.ENDPOINT)
                    else operation[4] - 3
                    if (snap.model, snap.endpoint) == (FLARE, provider.ENDPOINT)
                    else 0
                )
    supported = snap.model in (SUNBURST, FLARE) and snap.endpoint == provider.ENDPOINT
    return BudgetView(
        root,
        candidate_id,
        snap.execution_digest,
        snap.model,
        snap.endpoint,
        snap.group,
        combination,
        3,
        total,
        6,
        source_used,
        complete,
        not complete,
        bool(
            complete
            and supported
            and combination is not None
            and combination < 3
            and total is not None
            and total < 6
        ),
    )


def _verify_terminal_recoverable(
    db: sqlite3.Connection, root: str, attempt: str, candidate: ExecutionCandidate
) -> bool:
    """No production adapter for verified terminal recovery evidence exists."""
    return False


def _verify_candidate_authorization(
    db: sqlite3.Connection, candidate: ExecutionCandidate
) -> bool:
    """No production verifier for cross-candidate fee authorization exists."""
    return False


def _verify_candidate_capability(
    db: sqlite3.Connection, candidate: ExecutionCandidate
) -> bool:
    """Offline serialization does not verify real gateway capability."""
    return False


def _verify_business(db: sqlite3.Connection, candidate: ExecutionCandidate) -> bool:
    """No production adapter verifies business decisions for alternate candidates."""
    return False


def verified_chain_failure(
    db: sqlite3.Connection,
    root: str,
    candidate: ExecutionCandidate,
    attempt: str,
    digest: str,
) -> str:
    """Shared candidate-aware failure evidence check; no authorization is implied."""
    return safe_failure_kind(
        db,
        root,
        attempt,
        candidate.snapshot.task_id,
        candidate.snapshot.revision,
        digest,
        remote_verified=_verify_terminal_recoverable(db, root, attempt, candidate),
    )


def preflight(
    service: LocalEligibility, root: str, candidate_id: str
) -> PreflightResult:
    """Advisory only: a future transaction must recheck every gate."""
    try:
        candidate = read_candidate(service.store, candidate_id)
        budget = budget_view(service.store, root, candidate_id)
        if not candidate.source_candidate_id:
            return PreflightResult(False, ("candidate_source_missing",), budget)
        source = read_candidate(service.store, candidate.source_candidate_id)
    except (CandidateError, ValueError, sqlite3.Error):
        return PreflightResult(False, ("candidate_or_root_invalid",), None)
    reasons: list[str] = []
    if (
        source.snapshot.model,
        candidate.snapshot.model,
        source.snapshot.endpoint,
        candidate.snapshot.endpoint,
    ) != (
        SUNBURST,
        FLARE,
        provider.ENDPOINT,
        provider.ENDPOINT,
    ) or source.source_candidate_id is not None:
        reasons.append("switch_sequence_invalid")
    if not budget.complete:
        reasons.append("budget_history_incomplete")
    elif budget.source_used != 3:
        reasons.append("source_budget_not_exhausted")
    if budget.complete and not budget.allows_evaluation:
        reasons.append("target_budget_unavailable")
    with closing(service.store._connect()) as db, db:
        db.execute("BEGIN")
        snap = candidate.snapshot
        current = db.execute(
            "SELECT t.status,r.input_digest,(SELECT MAX(revision) FROM revision WHERE task_id=t.task_id),t.instructions FROM task t JOIN revision r ON r.task_id=t.task_id AND r.revision=? WHERE t.task_id=?",
            (snap.revision, snap.task_id),
        ).fetchone()
        if not current or current[:3] != ("ready", snap.input_digest, snap.revision):
            reasons.append("task_revision_not_current")
        else:
            try:
                task = Task(
                    snap.task_id, snap.input_digest, current[3], TaskStatus.READY
                )
                revision = Revision(snap.task_id, snap.revision, snap.input_digest)
                service._current_review(db, snap)
                offline_submission._check_business_gate(db, service, task, revision)
            except (ValueError, sqlite3.Error):
                reasons.append("business_or_review_invalid")
        chain = db.execute(
            "SELECT c.attempt_id,c.execution_digest FROM operation_attempt c WHERE c.operation_id=? ORDER BY c.sequence",
            (root,),
        ).fetchall()
        if not chain:
            reasons.append("source_evidence_missing")
        else:
            for index, (attempt, digest) in enumerate(chain):
                kind = verified_chain_failure(db, root, candidate, attempt, digest)
                if kind not in {"pre_send", "remote_terminal"}:
                    if index < len(chain) - 1:
                        reasons.append(
                            "prior_evidence_conflict"
                            if kind == "conflict"
                            else "prior_attempt_unresolved"
                        )
                    else:
                        reasons.append(
                            {
                                "conflict": "source_evidence_conflict",
                                "not_recoverable": "source_fault_not_recoverable",
                                "query_original": "source_unresolved",
                                "unverified": "source_unresolved",
                                "remote_unverified": "recovery_evidence_unverified",
                            }.get(kind, "recovery_evidence_unverified")
                        )
                    break
        if not _verify_business(db, candidate):
            reasons.append("business_or_review_unverified")
        if not _verify_candidate_authorization(db, candidate):
            reasons.append("authorization_unverified")
        if not _verify_candidate_capability(db, candidate):
            reasons.append("capability_unverified")
    return PreflightResult(not reasons, tuple(reasons), budget)
