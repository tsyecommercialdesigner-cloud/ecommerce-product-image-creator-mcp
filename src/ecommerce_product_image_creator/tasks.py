"""Offline local eligibility bookkeeping. Never establishes business authorization."""

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ecommerce_product_image_creator.domain import (
    Confirmation,
    ExecutionSnapshot,
    PromptRevision,
    Reference,
    Revision,
    Task,
    validate_revision_context,
)
from ecommerce_product_image_creator.storage import (
    ResourceBudget,
    Store,
    read_reference,
)

if TYPE_CHECKING:
    from ecommerce_product_image_creator.business_choices import BusinessChoice
    from ecommerce_product_image_creator.mock_workflow import MockReview


def read_task_execution(
    service: "LocalEligibility", task_id: str, revision: int, stage: str, root: str
) -> dict[str, Any]:
    """Read persisted execution history; the result never grants permission."""
    from .candidate_preflight import budget_view
    from .task_view import ReadOnlyStore, _SnapshotStore, read_task_view

    if stage not in ("form", "mock") or not root:
        raise ValueError("execution_identity_invalid")
    with closing(ReadOnlyStore(service.store.path)._connect()) as db:
        db.execute("BEGIN")
        snapshot_store = _SnapshotStore(service.store.path, db)
        view = read_task_view(
            snapshot_store, task_id, service.input_root, service.budget, _connection=db
        )
        attempts = [a for a in view["attempts"] if a["root_operation_id"] == root]
        if not attempts or any(
            a["revision"] != revision or a["stage"] != stage for a in attempts
        ):
            raise ValueError("execution_identity_mismatch")
        attempts.sort(key=lambda a: a["sequence"] or 0)
        candidate_id = attempts[-1]["candidate_id"]
        budget = (
            budget_view(snapshot_store, root, candidate_id) if candidate_id else None
        )
        counts = db.execute(
            "SELECT model,endpoint,count FROM attempt_counter WHERE operation_id=? ORDER BY model,endpoint",
            (root,),
        ).fetchall()
        wait = db.execute(
            "SELECT started,deadline,observed FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
            (root, attempts[-1]["attempt_id"]),
        ).fetchone()
        if wait is None:
            wait = db.execute(
                "SELECT started,deadline,observed FROM retry_wait WHERE operation_id=? AND attempt_id=?",
                (root, attempts[-1]["attempt_id"]),
            ).fetchone()
    if (
        budget is not None
        and budget.complete
        and (
            budget.total_used != len(attempts)
            or budget.total_used != sum(row[2] for row in counts)
        )
    ):
        raise ValueError("execution_snapshot_changed")
    now = time.time()
    wait_status = "unknown"
    if wait and all(math.isfinite(value) for value in (now, *wait)):
        if now < wait[0] or now < wait[2]:
            wait_status = "clock_invalid"
        elif now < wait[1]:
            wait_status = "waiting"
        else:
            wait_status = "elapsed_recheck_required"
    return {
        "task_id": task_id,
        "revision": revision,
        "stage": stage,
        "root_operation_id": root,
        "current_revision": view["current_revision"],
        "candidate_id": candidate_id,
        "attempts": attempts,
        "budget": budget,
        "combination_counts": [
            {"model": model, "endpoint": endpoint, "count": count}
            for model, endpoint, count in counts
        ]
        if budget is not None and budget.complete
        else None,
        "wait": {
            "started": wait[0],
            "deadline": wait[1],
            "observed": wait[2],
            "status": wait_status,
        }
        if wait
        else None,
    }


def advance_task_execution(
    service: "LocalEligibility",
    task_id: str,
    revision: int,
    stage: str,
    root: str,
    tick_id: str,
    *,
    clock: Callable[[], float],
) -> dict[str, Any]:
    """Delegate a single step; the scheduler rechecks authorization and safety."""
    from .provider import recovery_tick

    before = read_task_execution(service, task_id, revision, stage, root)
    if before["candidate_id"] is None:
        raise ValueError("execution_candidate_unavailable")
    if before["current_revision"] != revision:
        raise ValueError("stale_revision")
    result = recovery_tick(service, root, tick_id, clock=clock)
    return {
        "action": result.action,
        "reason": result.reason,
        "candidate_id": result.candidate_id,
        "attempt_id": result.attempt_id,
        "deadline": result.deadline,
        "state": read_task_execution(service, task_id, revision, stage, root),
    }


def _business_identity(
    store: Store, task_id: str, revision: int, stage: str
) -> dict[str, Any]:
    from .preparation import read_prepared

    saved = read_prepared(store, task_id, revision)
    if (
        stage not in ("form", "mock")
        or saved["stage"] != stage
        or saved["digest_version"] != 2
    ):
        raise ValueError("business_identity_mismatch")
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT MAX(revision) FROM revision WHERE task_id=?", (task_id,)
        ).fetchone()
    if not row or row[0] != revision:
        raise ValueError("stale_revision")
    return saved


def evaluate_task_business(
    store: Store, task_id: str, revision: int, stage: str
) -> dict[str, Any]:
    """Evaluate persisted choices; advisory only, never execution authorization."""
    from . import business_choices, input_revision
    from .preparation import read_prepare_response

    try:
        saved = _business_identity(store, task_id, revision, stage)
    except ValueError as exc:
        if str(exc) == "stale_revision":
            return {
                "status": "stale_revision",
                "pending": ["stale_revision"],
                "authorization": "unverified",
            }
        raise
    with closing(store._connect()) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_choice'"
        ).fetchone()
        choices = (
            [
                r[0]
                for r in db.execute(
                    "SELECT choice_id FROM business_choice WHERE task_id=? AND revision=?",
                    (task_id, revision),
                )
            ]
            if exists
            else []
        )
        subset_ok = business_choices.subset_gate_locked(db, store, task_id, revision)
        input_ok = input_revision.input_revision_gate_locked(
            db, store, task_id, revision, saved["execution_digest"]
        )
    result = business_choices.evaluate_choices(
        store, task_id, revision, choice_ids=choices
    )
    summary = read_prepare_response(store, task_id, revision)
    pending = list(result["pending"])
    if not subset_ok:
        pending.append("subset_confirmation")
    if not input_ok:
        pending.append("input_revision_confirmation")
    if summary["stage_context"]["prompt_semantic_conflicts"]:
        pending.append("prompt_brand_clarification")
    result.update(
        {
            "task_id": task_id,
            "revision": revision,
            "stage": stage,
            "execution_digest": saved["execution_digest"],
            "stage_context": summary["stage_context"],
            "pending": list(dict.fromkeys(pending)),
            "status": "pending_confirmation"
            if pending
            else "preview_only"
            if result["status"] == "preview"
            else "ready",
        }
    )
    return result


def record_task_business_choice(
    store: Store, stage: str, choice: "BusinessChoice"
) -> dict[str, str]:
    from . import business_choices

    _business_identity(store, choice.task_id, choice.revision, stage)
    business_choices.record_choice(store, choice)
    return {
        "status": "recorded",
        "choice_id": choice.choice_id,
        "source_trust": "unverified",
    }


def _task_proposal_identity(
    store: Store, task_id: str, revision: int, stage: str, proposal_id: str
) -> None:
    from . import business_choices

    saved = _business_identity(store, task_id, revision, stage)
    proposal = business_choices.read_subset_proposal(store, proposal_id)
    if (
        proposal["task_id"],
        proposal["revision"],
        proposal["input_digest"],
        proposal["execution_digest"],
    ) != (task_id, revision, saved["input_digest"], saved["execution_digest"]):
        raise ValueError("proposal_identity_mismatch")


def propose_task_subset(
    store: Store,
    task_id: str,
    revision: int,
    stage: str,
    proposal_id: str,
    selected: list[str],
    source: str,
    reason: str,
) -> dict[str, Any]:
    from . import business_choices

    _business_identity(store, task_id, revision, stage)
    return business_choices.record_subset_proposal(
        store, proposal_id, task_id, revision, selected, source, reason
    )


def decide_task_subset(
    store: Store,
    task_id: str,
    revision: int,
    stage: str,
    proposal_id: str,
    decision_id: str,
    decision: str,
    source: str,
    expires_at: float,
) -> dict[str, Any]:
    from . import business_choices

    _task_proposal_identity(store, task_id, revision, stage, proposal_id)
    business_choices.record_subset_decision(
        store, proposal_id, decision_id, decision, source, expires_at
    )
    return business_choices.read_subset_proposal(store, proposal_id)


def apply_task_subset(
    store: Store, task_id: str, revision: int, stage: str, proposal_id: str
) -> dict[str, Any]:
    from . import business_choices

    _task_proposal_identity(store, task_id, revision, stage, proposal_id)
    return business_choices.apply_subset_decision(store, proposal_id)


def _task_mock_identity(
    store: Store,
    task_id: str,
    revision: int,
    attempt_id: str,
    artifact_id: str,
    form_revision: int,
    form_artifact_id: str,
) -> None:
    """Check the persisted stage and both artifact identities; never trust caller IDs."""
    from .preparation import read_prepared

    try:
        saved = read_prepared(store, task_id, revision)
        source = saved["form_source"]
        with closing(store._connect()) as db:
            artifact = db.execute(
                "SELECT a.task_id,a.revision,x.attempt_id FROM artifact x JOIN attempt a ON a.attempt_id=x.attempt_id WHERE x.artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if (
            saved["stage"] != "mock"
            or saved["digest_version"] != 2
            or not source
            or (source["form_revision"], source["artifact_id"])
            != (form_revision, form_artifact_id)
            or artifact != (task_id, revision, attempt_id)
        ):
            raise ValueError("mock_identity_mismatch")
    except (KeyError, sqlite3.Error, TypeError, ValueError):
        raise ValueError("mock_identity_mismatch") from None


def record_task_mock_review(
    store: Store,
    review: "MockReview",
    *,
    attempt_id: str,
    form_revision: int,
    form_artifact_id: str,
) -> dict[str, Any]:
    """Record a scoped review; recording does not verify a human or authorize fees."""
    from . import mock_workflow

    _task_mock_identity(
        store,
        review.task_id,
        review.revision,
        attempt_id,
        review.artifact_id,
        form_revision,
        form_artifact_id,
    )
    try:
        mock_workflow.save_mock_review(store, review)
    except mock_workflow.MockWorkflowError:
        raise ValueError("mock_review_record_rejected") from None
    return {
        "status": "recorded",
        "review_id": review.review_id,
        "source_trust": "unverified",
    }


def read_task_mock_review(
    store: Store,
    review_id: str,
    task_id: str,
    revision: int,
    attempt_id: str,
    artifact_id: str,
    form_revision: int,
    form_artifact_id: str,
) -> dict[str, Any]:
    from . import mock_workflow

    _task_mock_identity(
        store,
        task_id,
        revision,
        attempt_id,
        artifact_id,
        form_revision,
        form_artifact_id,
    )
    try:
        state = mock_workflow.read_mock_review_state(store, review_id)
        review = state["review"]
        if (review.task_id, review.revision, review.artifact_id) != (
            task_id,
            revision,
            artifact_id,
        ):
            raise ValueError("mock_identity_mismatch")
        return state
    except mock_workflow.MockWorkflowError:
        raise ValueError("mock_review_unavailable") from None


def request_task_mock_revision(
    store: Store,
    request_id: str,
    review_id: str,
    task_id: str,
    revision: int,
    attempt_id: str,
    artifact_id: str,
    form_revision: int,
    form_artifact_id: str,
    reason: str,
    source: str,
    created_at: float,
) -> dict[str, str]:
    from . import mock_workflow

    state = read_task_mock_review(
        store,
        review_id,
        task_id,
        revision,
        attempt_id,
        artifact_id,
        form_revision,
        form_artifact_id,
    )
    try:
        mock_workflow.record_mock_revision_request(
            store, request_id, state["review"], reason, source, created_at
        )
    except mock_workflow.MockWorkflowError:
        raise ValueError("mock_revision_request_rejected") from None
    return {
        "status": "recorded",
        "request_id": request_id,
        "source_trust": "unverified",
    }


def check_task_mock_approval(
    store: Store,
    review_id: str,
    task_id: str,
    revision: int,
    attempt_id: str,
    artifact_id: str,
    digest: str,
    form_revision: int,
    form_artifact_id: str,
    artifact_root: Path,
    budget: ResourceBudget,
) -> dict[str, str]:
    """Check applicability without treating a source declaration as human proof."""
    from . import mock_workflow

    _task_mock_identity(
        store,
        task_id,
        revision,
        attempt_id,
        artifact_id,
        form_revision,
        form_artifact_id,
    )
    try:
        mock_workflow.check_mock_approval(
            store,
            review_id,
            task_id,
            revision,
            artifact_id,
            digest,
            artifact_root,
            budget,
        )
    except mock_workflow.MockWorkflowError:
        raise ValueError("mock_approval_unverified_or_invalid") from None
    return {"status": "applicable", "source_trust": "verifier_accepted"}


def prepare_task_mock_revision(
    store: Store,
    request_id: str,
    prompt: str,
    artifact_root: Path,
    budget: ResourceBudget,
) -> dict[str, Any]:
    """Consume the scoped request through the existing atomic revision workflow."""
    from . import mock_workflow

    try:
        return mock_workflow.prepare_mock_revision(
            store, request_id, prompt, artifact_root, budget
        )
    except mock_workflow.MockWorkflowError:
        raise ValueError("mock_revision_prepare_rejected") from None


def recover_task_once(
    store: Store,
    root: str,
    candidate_id: str,
    task_id: str,
    revision: int,
    stage: str,
    attempt_id: str,
    digest: str,
    *,
    artifact_root: Path,
    budget: ResourceBudget,
    action: str = "evaluate",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate or perform one independently authorized original query.

    Evaluation is advisory; the existing query gate rechecks identity, source,
    authorization, revocation and observations immediately before dispatch.
    This interface cannot register a request or perform a generation.
    """
    from . import artifact_locator, recovery, recovery_evaluation
    from .task_view import ReadOnlyStore

    if action not in ("evaluate", "query_original"):
        raise ValueError("unsupported_recovery_action")
    identity = (root, candidate_id, task_id, revision, stage, attempt_id, digest)
    view = recovery_evaluation.evaluate(
        store, *identity, artifact_root=artifact_root, budget=budget
    )
    query: dict[str, Any] = {"request_id": request_id, "invocation_id": None}
    executed = False
    reason = view["reason"]
    if action == "query_original":
        if not request_id:
            reason = "query_request_required"
        else:
            with closing(ReadOnlyStore(store.path)._connect()) as db:
                try:
                    row = db.execute(
                        "SELECT i.invocation_id,i.status FROM query_request q JOIN query_invocation i ON i.invocation_id=q.invocation_id WHERE q.request_id=? AND i.root_operation_id=? AND i.candidate_id=? AND i.task_id=? AND i.revision=? AND i.stage=? AND i.attempt_id=? AND i.execution_digest=?",
                        (request_id, *identity),
                    ).fetchone()
                except sqlite3.Error:
                    row = None
            if not row:
                reason = "query_registration_or_scope_unverified"
            elif view["action"] != "query_original" and row[1] == "intent":
                reason = str(view["reason"])
            else:
                query["invocation_id"] = row[0]
                resolution = recovery.recover_once(
                    store, *identity, request_id=request_id
                )
                query.update(
                    {"state": resolution.query_state, "reason": resolution.reason}
                )
                # This describes this call, never a persisted remote receipt.
                executed = resolution.adapter_invoked
                view = recovery_evaluation.evaluate(
                    store, *identity, artifact_root=artifact_root, budget=budget
                )
                reason = resolution.reason

    artifact_assessment = None
    observation_id = view["recovery"]["observation_id"]
    if view["recovery"]["state"] == "succeeded" and observation_id is not None:
        artifact_options = (
            {
                "artifact_id": view["artifacts"][0]["artifact_id"],
                "artifact_root": artifact_root,
                "budget": budget,
            }
            if len(view["artifacts"]) == 1
            else {}
        )
        artifact_assessment = asdict(
            artifact_locator.assess_attempt(
                store, *identity, observation_id, **artifact_options
            )
        )

    links: list[dict[str, Any]] = []
    with closing(ReadOnlyStore(store.path)._connect()) as db:
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_consumption'"
        ).fetchone():
            rows = db.execute(
                "SELECT source_root,source_candidate,source_attempt,source_digest,linked_operation,linked_candidate,linked_attempt,execution_digest,authorization_id FROM risk_consumption WHERE (source_root=? AND source_attempt=?) OR (linked_operation=? AND linked_attempt=?) ORDER BY rowid",
                (root, attempt_id, root, attempt_id),
            ).fetchall()
            names = (
                "source_root",
                "source_candidate",
                "source_attempt",
                "source_digest",
                "linked_root",
                "linked_candidate",
                "linked_attempt",
                "linked_digest",
                "risk_authorization_id",
            )
            links = [dict(zip(names, row, strict=True)) for row in rows]
    return {
        **view,
        "requested_action": action,
        "action_executed": executed,
        "execution_evidence": "adapter_invoked_this_call"
        if executed
        else "not_invoked_this_call",
        "query": query,
        "next_action": view["action"],
        "reason": reason,
        "artifact_assessment": artifact_assessment,
        "linked_evidence": links,
        "operation_role": "linked" if view["linked_source"] else "original",
        "original_may_still_run": view["recovery"]["state"]
        not in ("succeeded", "failed"),
        "human_intervention_required": view["action"]
        in ("manual_risk_review", "stop", "review_artifact", "verify_review"),
    }


class EligibilityError(ValueError):
    """Fail-closed error without user-supplied content."""


FORM_OUTPUT_CONTRACT = (
    "Generate one composite image containing F/L/R/B/T/Q views.",
    "Place each view label at the top left of its corresponding view.",
    "F front, L left, R right, B rear, T top; Q upper-right 45-degree orthographic with parallel lines parallel.",
    "Use approved form references and revision text; do not include the previous candidate as an input.",
)


def _explicit_brand_substitution(prompt: str) -> bool:
    """Recognize only a standalone, affirmative, explicitly labelled substitution.

    This is not brand extraction, identity verification, or general NLP.
    """
    identifier = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
    pattern = (
        rf"(?:please\s+)?(?:use|make)\s+(?:the\s+)?competitor\s+brand\s+"
        rf"{identifier}\s+(?:(?:as\s+)?the\s+target\s+brand)"
        rf"(?:\s+instead\s+of\s+{identifier})?[.!]?"
    )
    return re.fullmatch(pattern, prompt.strip(), flags=re.IGNORECASE) is not None


@dataclass(frozen=True)
class PromptContext:
    """Verified local prompt data; never proof of a human decision or authorization."""

    prompt: PromptRevision
    snapshot: ExecutionSnapshot
    form_source: tuple[int, str, str] | None
    stage_references: list[dict[str, Any]] = field(default_factory=list)
    brand_constraints: dict[str, Any] = field(default_factory=dict)
    unobserved_structure: dict[str, str] = field(default_factory=dict)
    structure_conflicts: dict[str, Any] = field(default_factory=dict)
    source_form_observations: list[dict[str, Any]] = field(default_factory=list)
    confirmation_status: str = field(default="unverified", init=False)
    authorization_status: str = field(default="unverified", init=False)

    @property
    def form_output_contract(self) -> tuple[str, ...]:
        if self.prompt.stage != "form":
            return ()
        return FORM_OUTPUT_CONTRACT

    def host_context(self) -> dict[str, Any]:
        conflict = _explicit_brand_substitution(self.prompt.text)
        return {
            "stage": self.snapshot.stage,
            "references": self.stage_references,
            "form_source": {
                "revision": self.form_source[0],
                "artifact_id": self.form_source[1],
                "sha256": self.form_source[2],
            }
            if self.form_source
            else None,
            "source_form_observations": self.source_form_observations,
            "brand_constraints": self.brand_constraints,
            "unobserved_structure": self.unobserved_structure,
            "structure_conflicts": self.structure_conflicts,
            "output": "six-view-composite",
            "output_views": ["F", "L", "R", "B", "T", "Q"],
            "q_projection": "upper-right 45-degree orthographic; parallel lines remain parallel",
            "form_output_contract": list(self.form_output_contract),
            "prompt_semantic_consistency": "conflict" if conflict else "unverified",
            "prompt_semantic_conflicts": [
                {"code": "competitor_as_target", "status": "pending_clarification"}
            ]
            if conflict
            else [],
            "visual_verification": "unverified",
        }


def read_prompt_context(store: Store, task_id: str, revision: int) -> PromptContext:
    """Recover the stage-specific prompt and its existing frozen execution context."""
    from ecommerce_product_image_creator.preparation import read_prepared

    saved = read_prepared(store, task_id, revision)
    if saved["digest_version"] != 2:
        raise EligibilityError("Prompt context requires a v2 snapshot")
    snapshot = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
    prompt = PromptRevision.from_text(
        task_id, revision, snapshot.prompt_revision, saved["stage"], saved["prompt"]
    )
    if (
        snapshot.prompt_revision != revision
        or prompt.prompt_digest != snapshot.prompt_digest
    ):
        raise EligibilityError("Prompt version or digest does not match snapshot")
    refs = saved["references"]
    selected = [r for r in refs if r["ref_id"] in saved["stage_references"]]
    form = [r for r in selected if r["category"] == "form"]
    artwork = [r for r in selected if r["category"] == "artwork"]
    source = saved["form_source"]
    scope = saved["operation_scope"]
    if (
        not scope
        or scope["target_view"] is not None
        or scope["output"] != "six-view-composite"
    ):
        raise EligibilityError("Invalid composite output scope")
    if snapshot.stage == "form":
        if not 1 <= len(form) <= 6 or artwork or source is not None:
            raise EligibilityError("Invalid form prompt context")
        form_source = None
    elif snapshot.stage == "mock":
        if (
            len(form) != 1
            or not 1 <= len(artwork) <= 2
            or not source
            or form[0]["ref_id"] != source["artifact_id"]
            or form[0]["sha256"] != source["sha256"]
        ):
            raise EligibilityError("Invalid mock prompt context")
        form_source = (source["form_revision"], source["artifact_id"], source["sha256"])
    else:
        raise EligibilityError("Invalid prompt stage")

    def observations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**o, "reference_id": item["ref_id"], "verification": "unverified"}
            for item in items
            for o in item.get("observations", [])
        ]

    stages = [{**r, "observations": observations([r])} for r in selected]
    source_observations: list[dict[str, Any]] = []
    if source:
        previous = read_prepared(store, task_id, source["form_revision"])
        source_observations = observations(
            [r for r in previous["references"] if r["category"] == "form"]
        )
    unknown = {"rear": "unknown"}
    for obs in observations(selected) + source_observations:
        if obs["status"] in {"unknown", "assumed"}:
            unknown[obs["subject"]] = "unknown"
    with closing(store._connect()) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prepared_conflicts'"
        ).fetchone()
        row = (
            db.execute(
                "SELECT reported FROM prepared_conflicts WHERE task_id=? AND revision=?",
                (task_id, revision),
            ).fetchone()
            if table
            else None
        )
    reported = json.loads(row[0]) if row else []
    brand = {
        "target_brand_source": "artwork",
        "exclude_form_branding": True,
        "target_brand_identifiers": [],
        "artwork_ids": [r["ref_id"] for r in artwork],
        "source_status": "unverified" if artwork else "unknown",
    }
    conflicts = {
        "status": "pending_clarification" if reported else "unknown",
        "reported": reported,
        "confirmation": "unverified",
    }
    return PromptContext(
        prompt,
        snapshot,
        form_source,
        stages,
        brand,
        unknown,
        conflicts,
        source_observations,
    )


def validate_prompt_context(store: Store, prompt: PromptRevision) -> PromptContext:
    """Reject cross-stage, cross-task and same-version content mismatches."""
    context = read_prompt_context(store, prompt.task_id, prompt.revision)
    if context.prompt != prompt:
        raise EligibilityError("Prompt does not match the frozen revision")
    return context


@dataclass(frozen=True)
class OperationScope:
    operation: str
    target_view: str | None
    output: str
    max_attempts: int

    def digest(self) -> str:
        if (
            self.operation != "generate"
            or not self.output
            or not 1 <= self.max_attempts <= 6
        ):
            raise EligibilityError("Invalid operation scope")
        if self.target_view not in {None, "F", "L", "R", "B", "T", "Q"}:
            raise EligibilityError("Invalid operation scope")
        value = [self.operation, self.target_view, self.output, self.max_attempts]
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class LocalEligibilityResult:
    local_eligible: bool = True
    evidence_status: str = "unverified"
    host_confirmation_verified: bool = False
    business_authorized: bool = False


class LocalEligibility:
    """Checks local consistency and consumption; does not call a Host or provider."""

    def __init__(
        self,
        store: Store,
        input_root: Path,
        budget: ResourceBudget,
        *,
        team_live: bool = False,
    ) -> None:
        self.store = store
        self.input_root = input_root
        self.budget = budget
        self.team_live = team_live
        with closing(self.store._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS local_eligibility (
                operation_id TEXT PRIMARY KEY, confirmation_id TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL,
                revision INTEGER NOT NULL, input_digest TEXT NOT NULL,
                prompt_revision INTEGER NOT NULL, prompt_digest TEXT NOT NULL,
                execution_digest TEXT NOT NULL, scope_digest TEXT NOT NULL,
                expires_at REAL NOT NULL, invalidated INTEGER NOT NULL DEFAULT 0 CHECK(invalidated IN (0,1)),
                consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)),
                evidence_status TEXT NOT NULL DEFAULT 'unverified' CHECK(evidence_status='unverified'),
                FOREIGN KEY(task_id,revision) REFERENCES revision(task_id,revision))""")
            db.execute("""CREATE TABLE IF NOT EXISTS candidate_eligibility (
                operation_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                root_operation_id TEXT NOT NULL,
                FOREIGN KEY(operation_id) REFERENCES local_eligibility(operation_id),
                FOREIGN KEY(candidate_id) REFERENCES execution_candidate(candidate_id),
                FOREIGN KEY(root_operation_id) REFERENCES operation(operation_id))""")

    def _validate(
        self,
        task: Task,
        revision: Revision,
        refs: list[Reference],
        prompt: PromptRevision,
        snapshot: ExecutionSnapshot,
        confirmation: Confirmation,
        operation_id: str,
        scope: OperationScope,
        *,
        candidate_id: str | None = None,
    ) -> str:
        if snapshot.digest_version != 2:
            raise EligibilityError("Unsupported execution snapshot version")
        try:
            if not operation_id or not confirmation.confirmation_id:
                raise ValueError("missing operation")
            validate_revision_context(
                task, revision, refs, prompt, snapshot, confirmation
            )
            if scope.target_view is not None or (
                snapshot.stage == "mock" and scope.output != "six-view-composite"
            ):
                raise ValueError("invalid stage scope")
            scope_digest = scope.digest()
            if snapshot.digest_version == 2:
                if snapshot.operation_scope != scope_digest:
                    raise ValueError("Execution scope mismatch")
                if snapshot.references != tuple(
                    (ref.ref_id, ref.sha256)
                    for ref in refs
                    if snapshot.stage == "mock" or ref.category.value == "form"
                ):
                    raise ValueError("Execution references mismatch")
            if snapshot.digest_version == 2:
                from ecommerce_product_image_creator.preparation import read_prepared

                persisted = read_prepared(self.store, task.task_id, revision.revision)
                if candidate_id is None:
                    if persisted["snapshot_payload"] != snapshot.to_payload():
                        raise ValueError("Stored execution snapshot mismatch")
                else:
                    from ecommerce_product_image_creator.execution_candidates import (
                        read_candidate,
                    )

                    candidate = read_candidate(self.store, candidate_id)
                    if (
                        candidate.snapshot != snapshot
                        or candidate.source_candidate_id is None
                    ):
                        raise ValueError("Candidate execution snapshot mismatch")
            else:
                for ref in refs:
                    frozen = read_reference(
                        self.input_root, ref.path, ref.mime, self.budget
                    )
                    if (frozen.sha256, len(frozen.data)) != (
                        ref.sha256,
                        ref.byte_count,
                    ):
                        raise ValueError("reference changed")
            return scope_digest
        except (ValueError, OSError, MemoryError):
            raise EligibilityError("Execution context is invalid or changed") from None

    def _current(self, db: sqlite3.Connection, task: Task, revision: Revision) -> None:
        row = db.execute(
            "SELECT input_digest,instructions,status FROM task WHERE task_id=?",
            (task.task_id,),
        ).fetchone()
        latest = db.execute(
            "SELECT MAX(revision) FROM revision WHERE task_id=?", (task.task_id,)
        ).fetchone()
        stored = db.execute(
            "SELECT input_digest FROM revision WHERE task_id=? AND revision=?",
            (task.task_id, revision.revision),
        ).fetchone()
        if (
            (
                not row
                or row[:2] != (task.input_digest, task.instructions)
                or row[2]
                not in (
                    {"ready", "draft", "awaiting_confirmation"}
                    if self.team_live
                    else {"ready"}
                )
            )
            or not latest
            or latest[0] != revision.revision
            or stored != (revision.input_digest,)
        ):
            raise EligibilityError("Task or current revision is not eligible")

    @staticmethod
    def _current_review(db: sqlite3.Connection, snapshot: ExecutionSnapshot) -> None:
        if snapshot.stage == "mock":
            from ecommerce_product_image_creator.preparation import (
                check_mock_review_current,
            )

            try:
                check_mock_review_current(db, snapshot.task_id, snapshot.revision)
            except ValueError:
                raise EligibilityError("Form review is no longer eligible") from None

    def prepare(
        self,
        task: Task,
        revision: Revision,
        refs: list[Reference],
        prompt: PromptRevision,
        snapshot: ExecutionSnapshot,
        confirmation: Confirmation,
        operation_id: str,
        scope: OperationScope,
        *,
        expires_at: float,
        candidate_id: str | None = None,
    ) -> None:
        scope_digest = self._validate(
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
        if not isinstance(expires_at, (int, float)) or not 0 < expires_at < float(
            "inf"
        ):
            raise EligibilityError("Invalid expiry")
        values = (
            confirmation.confirmation_id,
            task.task_id,
            revision.revision,
            revision.input_digest,
            prompt.prompt_revision,
            prompt.prompt_digest,
            snapshot.execution_digest,
            scope_digest,
        )
        try:
            with closing(self.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._current(db, task, revision)
                self._current_review(db, snapshot)
                candidate_row = None
                if candidate_id is not None:
                    candidate_row = db.execute(
                        "SELECT root_operation_id,task_id,revision,execution_digest,snapshot_payload,source_candidate_id FROM execution_candidate WHERE candidate_id=?",
                        (candidate_id,),
                    ).fetchone()
                    if (
                        not candidate_row
                        or candidate_row[1:]
                        != (
                            task.task_id,
                            revision.revision,
                            snapshot.execution_digest,
                            snapshot.to_payload(),
                            candidate_row[5],
                        )
                        or candidate_row[5] is None
                    ):
                        raise EligibilityError("Candidate scope is invalid")
                if db.execute(
                    "SELECT 1 FROM operation WHERE operation_id=?", (operation_id,)
                ).fetchone():
                    raise EligibilityError("Operation identifier already recorded")
                prior = db.execute(
                    "SELECT confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest FROM local_eligibility WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if prior is not None:
                    if prior != values:
                        raise EligibilityError("Operation identifier conflict")
                    association = db.execute(
                        "SELECT candidate_id,root_operation_id FROM candidate_eligibility WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if association != (
                        (candidate_id, candidate_row[0]) if candidate_row else None
                    ):
                        raise EligibilityError("Candidate association conflict")
                    return
                db.execute(
                    "UPDATE local_eligibility SET invalidated=1 WHERE task_id=? AND consumed=0",
                    (task.task_id,),
                )
                db.execute(
                    "INSERT INTO local_eligibility(operation_id,confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (operation_id, *values, expires_at),
                )
                if candidate_row is not None:
                    db.execute(
                        "INSERT INTO candidate_eligibility VALUES(?,?,?)",
                        (operation_id, candidate_id, candidate_row[0]),
                    )
        except sqlite3.Error:
            raise EligibilityError("Local eligibility storage error") from None

    def consume(
        self,
        task: Task,
        revision: Revision,
        refs: list[Reference],
        prompt: PromptRevision,
        snapshot: ExecutionSnapshot,
        confirmation: Confirmation,
        operation_id: str,
        scope: OperationScope,
    ) -> LocalEligibilityResult:
        try:
            scope_digest = self._validate(
                task,
                revision,
                refs,
                prompt,
                snapshot,
                confirmation,
                operation_id,
                scope,
            )
        except EligibilityError:
            self.invalidate(operation_id)
            raise
        values = (
            confirmation.confirmation_id,
            task.task_id,
            revision.revision,
            revision.input_digest,
            prompt.prompt_revision,
            prompt.prompt_digest,
            snapshot.execution_digest,
            scope_digest,
        )
        try:
            with closing(self.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._current(db, task, revision)
                self._current_review(db, snapshot)
                if db.execute(
                    "SELECT 1 FROM operation WHERE operation_id=?", (operation_id,)
                ).fetchone():
                    raise EligibilityError("Operation identifier already recorded")
                row = db.execute(
                    "SELECT confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at,invalidated,consumed FROM local_eligibility WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if (
                    row is None
                    or row[:8] != values
                    or row[8] <= time.time()
                    or row[9]
                    or row[10]
                ):
                    raise EligibilityError(
                        "Eligibility missing, expired, changed or consumed"
                    )
                db.execute(
                    "UPDATE local_eligibility SET consumed=1 WHERE operation_id=?",
                    (operation_id,),
                )
            return LocalEligibilityResult()
        except EligibilityError:
            self.invalidate(operation_id)
            raise
        except sqlite3.Error:
            raise EligibilityError("Local eligibility storage error") from None

    def invalidate(self, operation_id: str) -> None:
        try:
            with closing(self.store._connect()) as db, db:
                db.execute(
                    "UPDATE local_eligibility SET invalidated=1 WHERE operation_id=? AND consumed=0",
                    (operation_id,),
                )
        except sqlite3.Error:
            raise EligibilityError("Local eligibility storage error") from None
