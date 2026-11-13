"""Offline advisory classification; never grants authority or sends requests.

Inputs describe observations supplied by a caller, not verified authorization or
proof. A future execution boundary must independently revalidate all evidence.
"""

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from . import provider


class DeterministicInputError(ValueError):
    """The frozen input must change before the operation can succeed."""


class DeterministicConfigurationError(ValueError):
    """The execution configuration cannot succeed unchanged."""


class AuthorizationGateError(ValueError):
    """A local authorization or business gate rejected the operation."""


class TransientLocalError(Exception):
    """A controlled pre-dispatch component identified a temporary failure."""


def local_failure_category(error: Exception) -> str:
    """Classify explicit internal exception types, never exception text."""
    return {
        DeterministicInputError: "deterministic_input",
        DeterministicConfigurationError: "deterministic_configuration",
        AuthorizationGateError: "authorization_gate",
        TransientLocalError: "transient_local",
    }.get(type(error), "unknown")


@dataclass(frozen=True)
class FailureClassification:
    state: str
    remote_effect: str
    evidence: str
    next_action: str = "stop"
    task_id: str | None = None
    http_status: int | None = None
    retry_after_seconds: int | None = None
    can_send: bool = False


def _valid_id(value: str | None) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value))


def parse_retry_after(raw: str | None, now: datetime) -> int | None:
    """Parse seconds or a future HTTP date; no sleeping or network operations."""
    if not isinstance(raw, str) or now.tzinfo is None or now.utcoffset() is None:
        return None
    text = raw.strip()
    if re.fullmatch(r"[0-9]+", text):
        try:
            return int(text)
        except ValueError:
            return None
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        seconds = (parsed - now).total_seconds()
        return math.ceil(seconds) if seconds >= 0 and math.isfinite(seconds) else None
    except (TypeError, ValueError, OverflowError, IndexError):
        return None


def classify_transport(
    event: str, *, evidence: str, task_id: str | None = None
) -> FailureClassification:
    # This is an explicit local-boundary observation, not a transport exception.
    if event == "not_sent" and evidence == "local_guard" and task_id is None:
        return FailureClassification("not_sent", "not_executed", "local_guard")
    known_id = task_id if _valid_id(task_id) else None
    source = (
        event
        if event in {"connect_interrupted", "connect_timeout", "read_timeout"}
        else "unrecognized_transport"
    )
    return FailureClassification(
        "unknown", "unknown", source, "query_original" if known_id else "stop", known_id
    )


def classify_submission(
    http_status: int,
    payload: Any,
    *,
    retry_after: str | None = None,
    now: datetime | None = None,
) -> FailureClassification:
    delay = parse_retry_after(retry_after, now or datetime.now(UTC))
    result = provider.parse_submission(http_status, payload)
    if result.state == "accepted" and result.task_id:
        return FailureClassification(
            "accepted",
            "executed",
            "accepted_response",
            "query_original",
            result.task_id,
            http_status,
            delay,
        )
    state = "unknown" if http_status != 200 else "uninterpretable"
    return FailureClassification(
        state,
        "unknown",
        "http_response" if http_status != 200 else "uninterpretable_response",
        http_status=http_status,
        retry_after_seconds=delay,
    )


def classify_query(
    http_status: int, payload: Any, task_id: str
) -> FailureClassification:
    known_id = task_id if _valid_id(task_id) else None
    result = provider.parse_query(http_status, payload, task_id)
    state = {
        "pending": "accepted",
        "running": "running",
        "failed": "failed",
        "succeeded": "succeeded",
    }.get(result.state)
    if state is None:
        return FailureClassification(
            "uninterpretable",
            "unknown",
            "uninterpretable_query",
            "query_original" if known_id else "stop",
            known_id,
            http_status,
        )
    return FailureClassification(
        state,
        "executed",
        "query_response",
        "query_original" if state in {"accepted", "running"} else "stop",
        known_id,
        http_status,
    )


@dataclass(frozen=True)
class RetryFacts:
    """Caller assertions to be independently checked at any future execution boundary."""

    operation_id: str
    model: str
    endpoint: str
    attempts_for_combination: int
    attempts_total: int
    safe_failure_proven: bool = False
    fee_scope_verified: bool = False
    combination_verified: bool = False
    business_clear: bool = False
    recoverability: str = "unknown"


@dataclass(frozen=True)
class RetryDecision:
    outcome: str
    reason: str
    delay_seconds: int | None = None
    can_send: bool = False


def evaluate_retry_candidate(
    result: FailureClassification, facts: RetryFacts
) -> RetryDecision:
    """Return an advisory candidate only; this cannot grant or consume authority."""
    if result.state not in {"not_sent", "failed"} or result.remote_effect == "unknown":
        return RetryDecision("stop", "remote_effect_uncertain_or_not_retryable")
    if result.state == "not_sent" and facts.recoverability != "transient_local":
        return RetryDecision("stop", "recoverability_unverified_or_deterministic")
    if not all(
        (
            facts.safe_failure_proven,
            facts.fee_scope_verified,
            facts.combination_verified,
            facts.business_clear,
        )
    ):
        return RetryDecision("stop", "independent_evidence_required")
    if (
        not facts.operation_id.strip()
        or facts.model not in provider.MODELS
        or facts.endpoint != provider.ENDPOINT
    ):
        return RetryDecision("stop", "unsupported_scope")
    if not (
        0 <= facts.attempts_for_combination < 3
        and 0 <= facts.attempts_total < 6
        and facts.attempts_for_combination <= facts.attempts_total
    ):
        return RetryDecision("stop", "attempt_budget_exhausted_or_invalid")
    if result.http_status == 429 and result.retry_after_seconds is None:
        return RetryDecision("stop", "retry_after_required")
    delay = result.retry_after_seconds if result.http_status == 429 else 5
    return RetryDecision("candidate_only", "revalidation_required", delay)
