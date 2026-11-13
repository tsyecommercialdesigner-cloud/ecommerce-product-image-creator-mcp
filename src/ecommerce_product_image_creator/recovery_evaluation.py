"""Conservative local recovery advice; never executes or authorizes an action."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from . import original_query
from .artifact_reader import ArtifactReadError, read_artifact
from .execution_candidates import CandidateError, read_candidate
from .generation_cost import summarize_generation_cost
from .query_usage import summarize_query_evidence
from .recovery import resolve_recorded
from .send_evidence import (
    verified_linked_stop,
    verified_pre_send,
    verified_recoverability,
)
from .storage import ResourceBudget, Store
from .task_view import ReadOnlyStore, _has, _SnapshotStore


def evaluate(
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
) -> dict[str, Any]:
    """Read a single SQLite snapshot; filesystem observations are not atomic with it."""
    if stage not in ("form", "mock"):
        raise ValueError("identity_mismatch")
    with closing(ReadOnlyStore(store.path)._connect()) as db:
        db.execute("BEGIN")
        snapshot = _SnapshotStore(store.path, db)
        try:
            candidate = read_candidate(snapshot, candidate_id)
            s = candidate.snapshot
            if (
                candidate.root_operation_id,
                s.task_id,
                s.revision,
                s.stage,
                s.execution_digest,
                s.digest_version,
            ) != (root, task_id, revision, stage, digest, 2):
                raise ValueError("identity_mismatch")
            row = db.execute(
                "SELECT a.upstream_status,a.upstream_id,c.model,c.endpoint FROM attempt a JOIN operation_attempt c ON c.attempt_id=a.attempt_id JOIN operation o ON o.attempt_id=a.attempt_id WHERE a.attempt_id=? AND a.task_id=? AND a.revision=? AND c.operation_id=? AND c.execution_digest=? AND o.task_id=a.task_id AND o.revision=a.revision AND o.digest=c.execution_digest",
                (attempt_id, task_id, revision, root, digest),
            ).fetchone()
            assoc = db.execute(
                "SELECT candidate_id,root_operation_id FROM candidate_attempt WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if (
                not row
                or row[2:] != (s.model, s.endpoint)
                or (candidate.source_candidate_id is None and assoc is not None)
                or (
                    candidate.source_candidate_id is not None
                    and assoc != (candidate_id, root)
                )
            ):
                raise ValueError("identity_mismatch")
        except (CandidateError, TypeError) as exc:
            raise ValueError("identity_mismatch") from exc
        current = db.execute(
            "SELECT max(revision) FROM revision WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        try:
            identity = original_query._identity(
                snapshot, root, candidate_id, task_id, revision, attempt_id, digest
            )
        except sqlite3.Error:
            identity = None
        resolved = resolve_recorded(
            snapshot, root, candidate_id, task_id, revision, stage, attempt_id, digest
        )
        pre_send = (
            verified_pre_send(db, root, attempt_id, task_id, revision, digest)
            if _has(db, "attempt_transport_evidence")
            else False
        )
        transport = (
            db.execute(
                "SELECT phase,recorded_at FROM attempt_transport_evidence WHERE attempt_id=? AND logical_operation_id=? AND task_id=? AND revision=? AND execution_digest=?",
                (attempt_id, root, task_id, revision, digest),
            ).fetchone()
            if _has(db, "attempt_transport_evidence")
            else None
        )
        fault = (
            verified_recoverability(db, root, attempt_id, task_id, revision, digest)
            if pre_send and _has(db, "attempt_fault_evidence")
            else "unknown"
        )
        stop = verified_linked_stop(db, root, attempt_id, task_id, revision, digest)
        linked = (
            db.execute(
                "SELECT source_root,source_attempt FROM risk_consumption WHERE linked_operation=? AND linked_attempt=?",
                (root, attempt_id),
            ).fetchone()
            if _has(db, "risk_consumption")
            else None
        )
        linked_operations = (
            db.execute(
                "SELECT r.linked_operation,r.linked_attempt,a.upstream_status FROM risk_consumption r JOIN attempt a ON a.attempt_id=r.linked_attempt WHERE r.source_root=? AND r.source_attempt=? ORDER BY r.rowid",
                (root, attempt_id),
            ).fetchall()
            if _has(db, "risk_consumption")
            else []
        )
        artifact_ids = db.execute(
            "SELECT artifact_id FROM artifact WHERE attempt_id=? ORDER BY rowid",
            (attempt_id,),
        ).fetchall()
        artifacts = []
        for (artifact_id,) in artifact_ids:
            try:
                a = read_artifact(
                    snapshot,
                    task_id,
                    stage,
                    revision,
                    attempt_id,
                    artifact_id,
                    artifact_root,
                    budget,
                )
                artifacts.append(
                    {
                        "artifact_id": artifact_id,
                        "file": a["file"],
                        "review": a["review"],
                        "delivery_status": a["delivery_status"],
                    }
                )
            except (ArtifactReadError, sqlite3.Error):
                artifacts.append(
                    {
                        "artifact_id": artifact_id,
                        "file": {"state": "unverified"},
                        "review": {"state": "unverified"},
                    }
                )
        try:
            generation_cost = summarize_generation_cost(
                snapshot, task_id, root=root, attempt_id=attempt_id
            )
        except (ValueError, sqlite3.Error):
            generation_cost = {
                "cost_status": "unknown",
                "billing_completeness": "unverified",
            }
        try:
            query_cost = summarize_query_evidence(
                snapshot, root, candidate_id, attempt_id
            )
        except (ValueError, sqlite3.Error):
            query_cost = {
                "cost_status": "unknown",
                "billing_completeness": "unverified",
            }
        incomplete_query = (
            db.execute(
                "SELECT invocation_id,status FROM query_invocation WHERE root_operation_id=? AND candidate_id=? AND attempt_id=? AND execution_digest=? AND status IN ('entered','response_received') AND observation_id IS NULL ORDER BY rowid DESC LIMIT 1",
                (root, candidate_id, attempt_id, digest),
            ).fetchone()
            if _has(db, "query_invocation")
            else None
        )
        state = resolved["state"]
        if state == "conflict":
            action, reason = "stop", "observation_conflict"
        elif incomplete_query:
            action = "stop"
            reason = (
                "query_response_evidence_incomplete"
                if incomplete_query[1] == "response_received"
                else "query_dispatch_outcome_unknown"
            )
        elif pre_send or stop:
            action, reason = "review_failure_evidence", "verified_local_stop"
        elif state in ("running", "pending"):
            action, reason = "wait", "original_running"
        elif state == "succeeded":
            if not artifacts or any(
                a["file"]["state"] != "verified" for a in artifacts
            ):
                action, reason = (
                    "assess_original_artifact",
                    "artifact_missing_or_invalid",
                )
            else:
                if any(a["review"]["state"] == "accept" for a in artifacts):
                    action, reason = "verify_review", "review_source_unverified"
                else:
                    action, reason = "review_artifact", "artifact_requires_review"
        elif state == "failed":
            action, reason = (
                "review_failure_evidence",
                "remote_failed_not_authorization",
            )
        elif identity is not None:
            action, reason = "query_original", "trusted_original_id"
        else:
            action, reason = "manual_risk_review", "original_status_unverified"
        if revision != current:
            action, reason = "stop", "historical_revision_requires_review"
        return {
            "identity": {
                "root": root,
                "candidate_id": candidate_id,
                "task_id": task_id,
                "revision": revision,
                "stage": stage,
                "attempt_id": attempt_id,
                "execution_digest": digest,
            },
            "historical_upstream_status": row[0],
            "trusted_upstream_id": identity.upstream_id if identity else None,
            "recovery": resolved,
            "pre_send_verified": pre_send,
            "transport_evidence": {"phase": transport[0], "recorded_at": transport[1]}
            if transport
            else None,
            "local_stop": stop,
            "recoverability": fault,
            "linked_source": {"root": linked[0], "attempt_id": linked[1]}
            if linked
            else None,
            "linked_operations": [
                {"root": item[0], "attempt_id": item[1], "historical_status": item[2]}
                for item in linked_operations
            ],
            "artifacts": artifacts,
            "generation_cost": generation_cost,
            "query_cost": query_cost,
            "incomplete_query": {
                "invocation_id": incomplete_query[0],
                "status": incomplete_query[1],
            }
            if incomplete_query
            else None,
            "action": action,
            "reason": reason,
            "authorization": "not_granted",
            "uncertainties": [
                "remote_protocol_unverified",
                "query_and_download_cost_unknown",
                "file_state_not_atomic_with_database",
            ],
            "duplicate_generation_risk": bool(linked_operations)
            or (state not in ("succeeded", "failed") and not pre_send and not stop),
        }
