"""Fail-closed, single-step recovery from append-only query observations."""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from . import original_query
from .storage import Store


@dataclass(frozen=True)
class RecoveryResolution:
    state: str
    reason: str
    query_state: str
    candidate_id: str
    attempt_id: str
    observation_id: int | None
    risk_of_duplicate_cost: bool
    original_may_still_run: bool
    adapter_invoked: bool = False


def _verify_observation_source(
    identity: original_query.OriginalIdentity, row: tuple[object, ...]
) -> bool:
    """Strict observations remain closed; Team observations require active policy."""
    from .team_query_policy import enabled

    return (
        enabled()
        and len(row) >= 18
        and row[1:14]
        == (
            identity.root,
            identity.candidate_id,
            identity.task_id,
            identity.revision,
            identity.stage,
            identity.attempt_id,
            identity.digest,
            identity.provider,
            identity.model,
            identity.endpoint,
            identity.group,
            identity.upstream_id,
            "configured_query_adapter",
        )
    )


def resolve_recorded(
    store: Store,
    root: str,
    candidate_id: str,
    task_id: str,
    revision: int,
    stage: str,
    attempt_id: str,
    digest: str,
) -> dict[str, object]:
    """Reduce already-recorded observations without querying or changing state."""

    def result(
        state: str, reason: str, observation_id: int | None = None
    ) -> dict[str, object]:
        return {"state": state, "reason": reason, "observation_id": observation_id}

    try:
        identity = original_query._identity(
            store, root, candidate_id, task_id, revision, attempt_id, digest
        )
        if identity is None or identity.stage != stage:
            return result("unverified", "identity_or_source_unverified")
        with closing(store._connect()) as db:
            if not original_query._chain_is_valid(db, identity):
                return result("conflict", "observation_chain_invalid")
            rows = db.execute(
                "SELECT observation_id,root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,provider,model,endpoint,grouping,upstream_id,source,response_digest,remote_state,reason,previous_id FROM recovery_observation WHERE attempt_id=? ORDER BY observation_id",
                (attempt_id,),
            ).fetchall()
        if not rows:
            return result("unknown", "no_observations")
        previous = None
        terminal = None
        state = "unknown"
        expected = (
            root,
            candidate_id,
            task_id,
            revision,
            stage,
            attempt_id,
            digest,
            identity.provider,
            identity.model,
            identity.endpoint,
            identity.group,
            identity.upstream_id,
            "configured_query_adapter",
        )
        for row in rows:
            if row[1:14] != expected or row[17] != previous:
                return result("conflict", "observation_chain_invalid", row[0])
            if not _verify_observation_source(identity, row):
                return result("unverified", "observation_source_unverified", row[0])
            if row[15] != "query_error" and (
                not isinstance(row[14], str)
                or not re.fullmatch(r"[0-9a-f]{64}", row[14])
            ):
                return result("unverified", "observation_evidence_incomplete", row[0])
            observed = row[15]
            if observed == "conflict" or (
                terminal
                and observed in {"pending", "running", "succeeded", "failed"}
                and observed != terminal
            ):
                return result("conflict", "observation_state_conflict", row[0])
            if observed in {"succeeded", "failed"}:
                terminal = observed
                state = observed
            elif observed in {"pending", "running"} and not terminal:
                state = observed
            elif observed not in {"unknown", "query_error", "pending", "running"}:
                return result("unverified", "observation_state_unrecognized", row[0])
            previous = row[0]
        return result(state, "recorded_observations", previous)
    except (ValueError, sqlite3.Error, OSError, TypeError):
        return result("unverified", "identity_or_storage_unverified")


def recover_once(
    store: Store,
    root: str,
    candidate_id: str,
    task_id: str,
    revision: int,
    stage: str,
    attempt_id: str,
    digest: str,
    *,
    request_id: str | None = None,
) -> RecoveryResolution:
    """Query at most once. No generation, delivery or authorization is performed."""

    adapter_invoked = False

    def result(
        state: str, reason: str, query_state: str, observation_id: int | None = None
    ) -> RecoveryResolution:
        uncertain = state not in {"succeeded", "failed"}
        return RecoveryResolution(
            state,
            reason,
            query_state,
            candidate_id,
            attempt_id,
            observation_id,
            uncertain,
            uncertain,
            adapter_invoked,
        )

    try:
        identity = original_query._identity(
            store, root, candidate_id, task_id, revision, attempt_id, digest
        )
        if identity is None or identity.stage != stage:
            return result("unverified", "identity_or_source_unverified", "unverified")
        query = original_query.query_once(
            store,
            root,
            candidate_id,
            task_id,
            revision,
            attempt_id,
            digest,
            request_id=request_id,
        )
        adapter_invoked = query.adapter_invoked
        if query.state == "conflict" and query.observation_id is None:
            return result("conflict", query.reason, query.state)
        if query.state == "unknown" and query.observation_id is None:
            return result("unknown", query.reason, query.state)
        if query.state == "stopped" and query.observation_id is None:
            return result("stopped", query.reason, query.state)
        if query.state == "unverified" or query.observation_id is None:
            return result("unverified", query.reason, query.state)
        with closing(store._connect()) as db:
            exists = db.execute(
                "SELECT 1 FROM recovery_observation WHERE observation_id=? AND attempt_id=?",
                (query.observation_id, attempt_id),
            ).fetchone()
        if not exists:
            return result(
                "unverified", "observation_missing", query.state, query.observation_id
            )
        resolution = resolve_recorded(
            store, root, candidate_id, task_id, revision, stage, attempt_id, digest
        )
        return result(
            str(resolution["state"]),
            str(resolution["reason"]),
            query.state,
            query.observation_id,
        )
    except (ValueError, sqlite3.Error, OSError, TypeError):
        return result("unverified", "identity_or_storage_unverified", "unverified")
