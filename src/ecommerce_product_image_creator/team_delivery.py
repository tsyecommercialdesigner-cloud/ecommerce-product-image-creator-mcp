"""Explicit Team query and first-delivery orchestration; default policy denies."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import artifact_locator, original_query, provider, query_registration, recovery
from .config import team_mode_enabled
from .execution_candidates import register_root_candidate
from .storage import ResourceBudget, Store


@dataclass(frozen=True)
class OutputEvidence:
    url: str
    sha256: str
    mime: str
    source: str
    handle: str


def _persist_terminal(store: Store, identity: original_query.OriginalIdentity) -> str:
    """Persist a verified terminal observation on its original Team attempt."""
    resolved = recovery.resolve_recorded(
        store,
        identity.root,
        identity.candidate_id,
        identity.task_id,
        identity.revision,
        identity.stage,
        identity.attempt_id,
        identity.digest,
    )
    state, observation = resolved.get("state"), resolved.get("observation_id")
    if state not in ("succeeded", "failed") or not isinstance(observation, int):
        return str(state)
    with closing(store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT a.upstream_status FROM attempt a JOIN operation o ON o.attempt_id=a.attempt_id "
            "JOIN recovery_observation r ON r.attempt_id=a.attempt_id "
            "WHERE a.attempt_id=? AND o.operation_id=? AND o.task_id=? AND o.revision=? "
            "AND o.digest=? AND a.upstream_id=? AND r.observation_id=? AND r.remote_state=? "
            "AND r.observation_id=(SELECT MAX(observation_id) FROM recovery_observation WHERE attempt_id=a.attempt_id)",
            (
                identity.attempt_id,
                identity.root,
                identity.task_id,
                identity.revision,
                identity.digest,
                identity.upstream_id,
                observation,
                state,
            ),
        ).fetchone()
        if current is None or current[0] not in (
            "accepted",
            "submission_uncertain",
            state,
        ):
            raise ValueError("team_terminal_identity_conflict")
        db.execute(
            "UPDATE attempt SET upstream_status=? WHERE attempt_id=?",
            (state, identity.attempt_id),
        )
    return str(state)


def _record_download_failure(
    store: Store, locator: artifact_locator.LocatorEvidence
) -> None:
    """Record only against the still-verified original successful observation."""
    with closing(store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if not artifact_locator._checked(db, store, locator):
            raise ValueError("team_download_source_changed")
        db.execute(
            "UPDATE attempt SET delivery_status='download_failed' "
            "WHERE attempt_id=? AND upstream_status='succeeded' "
            "AND delivery_status IN ('not_available','pending','download_failed') "
            "AND NOT EXISTS (SELECT 1 FROM artifact WHERE attempt_id=?)",
            (locator.attempt_id, locator.attempt_id),
        )


def _query_policy(identity: original_query.OriginalIdentity) -> bool:
    from .team_query_policy import enabled

    return enabled() and identity.endpoint == provider.ENDPOINT


def _output_evidence(
    _identity: original_query.OriginalIdentity, _observation_id: int
) -> OutputEvidence | None:
    """A URL or a downloaded hash alone does not establish independent provenance."""
    return None


def advance(
    store: Store,
    task_id: str,
    artifact_root: Path,
    budget: ResourceBudget,
    *,
    attempt_id: str | None = None,
) -> None:
    from .team_query_policy import explicit_refresh

    with explicit_refresh(store):
        _advance(store, task_id, artifact_root, budget, attempt_id=attempt_id)


def _advance(
    store: Store,
    task_id: str,
    artifact_root: Path,
    budget: ResourceBudget,
    *,
    attempt_id: str | None = None,
) -> None:
    """Make at most one query and one delivery attempt for an explicit local Get."""
    if not team_mode_enabled() or not all(
        os.environ.get(name, "").lower() == "true"
        for name in ("ENABLE_REAL_GENERATION", "ENABLE_TEAM_QUERY")
    ):
        return
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT o.operation_id,o.revision,o.attempt_id,o.digest,a.upstream_id "
            "FROM operation o JOIN attempt a ON a.attempt_id=o.attempt_id "
            "WHERE o.task_id=? AND (? IS NULL OR a.attempt_id=?) ORDER BY o.rowid DESC LIMIT 1",
            (task_id, attempt_id, attempt_id),
        ).fetchone()
        if (
            not row
            or not row[4]
            or db.execute(
                "SELECT 1 FROM artifact WHERE attempt_id=?", (row[2],)
            ).fetchone()
        ):
            return
        anchor = db.execute(
            "SELECT delivery_id FROM first_artifact_anchor WHERE attempt_id=?",
            (row[2],),
        ).fetchone()
    if anchor:
        store.reconcile_first_artifact(anchor[0], artifact_root, budget)
        return
    root, revision, attempt, digest, _upstream = row
    candidate_id = "team-" + hashlib.sha256(root.encode()).hexdigest()[:32]
    candidate = register_root_candidate(store, root, candidate_id)
    identity = original_query._identity(
        store, root, candidate_id, task_id, revision, attempt, digest
    )
    if identity is None or not _query_policy(identity):
        raise ValueError("team_query_policy_unavailable")
    prior = recovery.resolve_recorded(
        store, root, candidate_id, task_id, revision, identity.stage, attempt, digest
    )
    observation_id = (
        prior.get("observation_id") if prior.get("state") == "succeeded" else None
    )
    if prior.get("state") == "failed":
        _persist_terminal(store, identity)
        return
    if prior.get("state") == "conflict":
        return
    if (
        isinstance(observation_id, int)
        and os.environ.get("ENABLE_TEAM_DOWNLOAD", "").lower() == "true"
        and _output_evidence(identity, observation_id) is None
    ):
        # A signed URL is intentionally not stored. An explicit later refresh
        # obtains a fresh response rather than treating the old URL as available.
        observation_id = None
    if observation_id is None:
        account = original_query._query_account_scope(identity)
        if not account:
            return
        req_id, auth_id = uuid.uuid4().hex, uuid.uuid4().hex
        req = query_registration.QueryRequest(
            req_id,
            task_id,
            revision,
            identity.stage,
            root,
            candidate_id,
            attempt,
            digest,
            identity.upstream_id,
            identity.provider,
            identity.model,
            identity.endpoint,
            identity.group,
            account,
            "query_original",
            auth_id,
        )
        auth = query_registration.QueryAuthorization(
            auth_id,
            req_id,
            attempt,
            digest,
            identity.provider,
            account,
            "query_original",
            1,
            "team-explicit-query",
            time.time() + 60,
            "query cost unknown",
        )
        query_registration.record_authorization(store, auth)
        query_registration.register_request(store, req)
        result = recovery.recover_once(
            store,
            root,
            candidate_id,
            task_id,
            revision,
            identity.stage,
            attempt,
            digest,
            request_id=req_id,
        )
        if result.state == "failed":
            _persist_terminal(store, identity)
            return
        if result.state != "succeeded" or result.observation_id is None:
            return
        observation_id = result.observation_id
    if _persist_terminal(store, identity) != "succeeded":
        return
    if not isinstance(observation_id, int):
        return
    from .team_artifact_evidence import selected

    selected_output = selected(identity, observation_id)
    if selected_output is not None:
        if os.environ.get("ENABLE_TEAM_DOWNLOAD", "").lower() != "true":
            return
        from .download_safety import DownloadLimits, fetch_candidate
        from .team_query_policy import url_hash

        url, _host = selected_output
        locator_id = (
            "locator-"
            + hashlib.sha256(f"{attempt}:{observation_id}".encode()).hexdigest()[:32]
        )
        locator = artifact_locator.LocatorEvidence(
            locator_id,
            root,
            candidate_id,
            task_id,
            revision,
            identity.stage,
            attempt,
            digest,
            identity.provider,
            identity.model,
            identity.endpoint,
            identity.group,
            "team-gateway",
            identity.upstream_id,
            observation_id,
            "output-1",
            "fixture_handle",
            "team-gateway",
            url_hash(url),
            time.time(),
            time.time() + 300,
        )
        artifact_locator.register(store, locator)
        limits = DownloadLimits(
            budget.max_file_bytes,
            1024,
            5,
            5,
            30,
            max_header_bytes=16384,
            dns_seconds=5,
            dns_max_answers=32,
        )
        try:
            candidate_bytes = fetch_candidate(
                store, locator_id, url, "", "", limits, budget
            )
        except (OSError, ValueError, MemoryError, sqlite3.Error):
            try:
                _record_download_failure(store, locator)
            except (OSError, ValueError, sqlite3.Error):
                pass
            raise
        delivery_id = "delivery-" + hashlib.sha256(attempt.encode()).hexdigest()[:32]
        artifact_id = "artifact-" + hashlib.sha256(attempt.encode()).hexdigest()[:32]
        target = artifact_root / (
            artifact_id + (".png" if candidate_bytes.mime == "image/png" else ".jpg")
        )
        store.register_first_artifact(
            delivery_id,
            artifact_id,
            locator_id,
            candidate_bytes.sha256,
            candidate_bytes.mime,
            artifact_root,
            target,
        )
        download = (url, candidate_bytes)
        try:
            store.stage_first_artifact(
                delivery_id, artifact_root, budget, _download=download
            )
            store.reconcile_first_artifact(
                delivery_id, artifact_root, budget, _download=download
            )
        except (OSError, ValueError, MemoryError, sqlite3.Error):
            from .first_artifact import record_download_save_failure

            try:
                record_download_save_failure(store, delivery_id, *download)
            except (OSError, ValueError, sqlite3.Error):
                pass
            raise
        return
    evidence = _output_evidence(identity, observation_id)
    if evidence is None or os.environ.get("ENABLE_TEAM_DOWNLOAD", "").lower() != "true":
        return
    locator_id = "locator-" + hashlib.sha256(attempt.encode()).hexdigest()[:32]
    locator = artifact_locator.LocatorEvidence(
        locator_id,
        root,
        candidate_id,
        task_id,
        revision,
        identity.stage,
        attempt,
        digest,
        identity.provider,
        identity.model,
        identity.endpoint,
        identity.group,
        account if (account := original_query._query_account_scope(identity)) else "",
        identity.upstream_id,
        observation_id,
        "output-1",
        "fixture_handle",
        evidence.source,
        evidence.handle,
        time.time(),
        time.time() + 300,
    )
    artifact_locator.register(store, locator)
    delivery_id = "delivery-" + hashlib.sha256(attempt.encode()).hexdigest()[:32]
    artifact_id = "artifact-" + hashlib.sha256(attempt.encode()).hexdigest()[:32]
    from .download_safety import DownloadLimits

    limits = DownloadLimits(
        budget.max_file_bytes, 1024, 5, 5, 30, max_header_bytes=16384
    )
    target = artifact_root / (
        artifact_id + (".png" if evidence.mime == "image/png" else ".jpg")
    )
    deliver = (
        provider.deliver_downloaded_form
        if candidate.snapshot.stage == "form"
        else provider.deliver_downloaded_mock
    )
    deliver(
        store,
        locator_id,
        evidence.url,
        evidence.sha256,
        evidence.mime,
        limits,
        budget,
        delivery_id,
        artifact_id,
        artifact_root,
        target,
    )
