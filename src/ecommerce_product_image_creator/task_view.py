"""Read-only local task history; never queries a provider or authorizes execution."""

import re
import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path
from typing import Any, Self, cast

from . import timing_evidence
from .artifact_reader import ArtifactReadError, read_artifact
from .execution_candidates import CandidateError, read_candidate
from .generation_cost import read_supplier_response_costs, summarize_generation_cost
from .provider import ENDPOINT
from .query_usage import summarize_query_evidence
from .recovery import resolve_recorded
from .send_evidence import verified_linked_stop
from .storage import ResourceBudget, Store
from .timing_evidence import TimingEvidence


class ReadOnlyStore(Store):
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10
        )


class _BorrowedReadConnection:
    """A borrowed handle cannot close or restart the owner's read transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if sql.strip().upper() == "BEGIN" and self._connection.in_transaction:
            return self._connection.execute("SELECT 1 WHERE 0")
        if not sql.lstrip().upper().startswith("SELECT "):
            raise sqlite3.OperationalError("Task view permits only SELECT")
        return self._connection.execute(sql, parameters)

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _SnapshotStore(Store):
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    def _connect(self) -> sqlite3.Connection:
        return cast(sqlite3.Connection, _BorrowedReadConnection(self._connection))


def _has(db: sqlite3.Connection, table: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def read_task_view(
    store: Store,
    task_id: str,
    artifact_root: Path,
    budget: ResourceBudget,
    *,
    attempt_id: str | None = None,
    _connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Return local persisted evidence only, preserving unknown and distinct states."""
    if (
        not task_id
        or not isinstance(task_id, str)
        or (attempt_id is not None and not attempt_id)
    ):
        raise ValueError("invalid_task_identity")
    with (
        closing(ReadOnlyStore(store.path)._connect())
        if _connection is None
        else nullcontext(_connection)
    ) as db:
        if not db.in_transaction:
            db.execute("BEGIN")
        store = _SnapshotStore(store.path, db)
        task = db.execute(
            "SELECT status FROM task WHERE task_id=?", (task_id,)
        ).fetchone()
        if not task:
            raise ValueError("task_not_found")
        revisions = db.execute(
            "SELECT revision FROM revision WHERE task_id=? ORDER BY revision",
            (task_id,),
        ).fetchall()
        current_revision = revisions[-1][0] if revisions else None
        prepared = _has(db, "prepared_execution")
        stages = (
            dict(
                db.execute(
                    "SELECT revision,stage FROM prepared_execution WHERE task_id=?",
                    (task_id,),
                ).fetchall()
            )
            if prepared
            else {}
        )
        rows = db.execute(
            "SELECT a.attempt_id,a.revision,a.upstream_status,a.delivery_status,a.upstream_id,a.previous_attempt_id,"
            "c.operation_id,c.sequence,c.execution_digest,c.model,c.endpoint,o.operation_id "
            "FROM attempt a LEFT JOIN operation_attempt c ON c.attempt_id=a.attempt_id "
            "LEFT JOIN operation o ON o.attempt_id=a.attempt_id WHERE a.task_id=? ORDER BY a.revision,a.rowid",
            (task_id,),
        ).fetchall()
        if attempt_id is not None and not any(row[0] == attempt_id for row in rows):
            raise ValueError("attempt_not_found")
        candidates = (
            db.execute(
                "SELECT candidate_id,root_operation_id,task_id,revision,execution_digest,source_candidate_id,snapshot_payload FROM execution_candidate WHERE task_id=? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            if _has(db, "execution_candidate")
            else []
        )
        candidate_views = []
        verified_candidates: set[str] = set()
        for r in candidates:
            item: dict[str, Any] = {
                "candidate_id": r[0],
                "root_operation_id": r[1],
                "revision": r[3],
                "execution_digest": r[4],
                "source_candidate_id": r[5],
            }
            try:
                candidate = read_candidate(store, r[0])
                snap = candidate.snapshot
                if (snap.task_id, snap.revision, snap.execution_digest) != (
                    task_id,
                    r[3],
                    r[4],
                ) or candidate.root_operation_id != r[1]:
                    raise ValueError("candidate mismatch")
                verified_candidates.add(r[0])
                item.update(
                    {
                        "stage": snap.stage,
                        "model": snap.model,
                        "group": snap.group,
                        "snapshot_status": "valid",
                    }
                )
            except (CandidateError, ValueError, TypeError):
                item["snapshot_status"] = "unverified"
            candidate_views.append(item)
        attempt_views: list[dict[str, Any]] = []
        for row in rows:
            (
                aid,
                rev,
                upstream,
                delivery,
                upstream_id,
                previous,
                root,
                seq,
                digest,
                model,
                endpoint,
                operation,
            ) = row
            if attempt_id is not None and aid != attempt_id:
                continue
            stage = stages.get(rev, "unknown")
            association = (
                db.execute(
                    "SELECT candidate_id FROM candidate_attempt WHERE attempt_id=? AND root_operation_id=?",
                    (aid, root),
                ).fetchone()
                if root and _has(db, "candidate_attempt")
                else None
            )
            matches = [
                c
                for c in candidates
                if c[0] in verified_candidates
                and c[1] == root
                and c[3] == rev
                and c[4] == digest
            ]
            candidate_id = (
                association[0]
                if association and any(c[0] == association[0] for c in matches)
                else matches[0][0]
                if len(matches) == 1 and not association
                else None
            )
            transport = (
                db.execute(
                    "SELECT phase,recorded_at FROM attempt_transport_evidence WHERE attempt_id=? AND logical_operation_id=? AND task_id=? AND revision=? AND execution_digest=?",
                    (aid, root, task_id, rev, digest),
                ).fetchone()
                if root and _has(db, "attempt_transport_evidence")
                else None
            )
            observations = (
                db.execute(
                    "SELECT observation_id,remote_state,reason,observed_at,previous_id FROM recovery_observation WHERE task_id=? AND revision=? AND attempt_id=? AND root_operation_id=? AND candidate_id=? AND execution_digest=? ORDER BY observation_id",
                    (task_id, rev, aid, root, candidate_id, digest),
                ).fetchall()
                if root and candidate_id and _has(db, "recovery_observation")
                else []
            )
            conclusion = (
                resolve_recorded(
                    store, root, candidate_id, task_id, rev, stage, aid, digest
                )
                if root and candidate_id and digest and stage in ("form", "mock")
                else {
                    "state": "unverified",
                    "reason": "identity_or_source_unverified",
                    "observation_id": None,
                }
            )
            timing = None
            if root and candidate_id and digest and _has(db, "attempt_timing_evidence"):
                t = db.execute(
                    "SELECT started_at,completed_at,source FROM attempt_timing_evidence WHERE attempt_id=? AND root=? AND candidate_id=? AND task_id=? AND revision=? AND stage=? AND execution_digest=?",
                    (aid, root, candidate_id, task_id, rev, stage, digest),
                ).fetchone()
                if t and conclusion["state"] in ("succeeded", "failed"):
                    evidence = TimingEvidence(
                        root,
                        candidate_id,
                        task_id,
                        rev,
                        stage,
                        aid,
                        digest,
                        t[0],
                        t[1],
                        t[2],
                    )
                    if (
                        timing_evidence._verify_timing_source(
                            store._connect(), evidence
                        )
                        and t[1] >= t[0]
                    ):
                        timing = t
            artifact_rows = db.execute(
                "SELECT artifact_id FROM artifact WHERE attempt_id=? ORDER BY rowid",
                (aid,),
            ).fetchall()
            artifacts: list[dict[str, Any]] = []
            for (artifact_id,) in artifact_rows:
                try:
                    if stage not in ("form", "mock"):
                        raise ArtifactReadError("stage unknown")
                    art = read_artifact(
                        store,
                        task_id,
                        stage,
                        rev,
                        aid,
                        artifact_id,
                        artifact_root,
                        budget,
                    )
                    if (
                        art["delivery_status"] == "missing_or_corrupt"
                        and art["file"]["state"] == "verified"
                    ):
                        # Bytes alone cannot establish that an unreconciled replacement
                        # is the inode bound to this artifact's publication evidence.
                        art["file"] = {"state": "identity_unverified", "mime": None}
                        art["review_applicability"] = "invalid_artifact"
                    artifacts.append(
                        {
                            k: art[k]
                            for k in (
                                "artifact_id",
                                "digest",
                                "delivery_status",
                                "file",
                                "review",
                                "review_applicability",
                                "form_source",
                                "revision_origin",
                            )
                        }
                    )
                    if _has(db, "artifact_integrity_observation"):
                        integrity = db.execute(
                            "SELECT observation_id,state,reason,previous_status,observed_at FROM artifact_integrity_observation WHERE artifact_id=? AND attempt_id=? ORDER BY observation_id DESC LIMIT 1",
                            (artifact_id, aid),
                        ).fetchone()
                        if integrity:
                            artifacts[-1]["integrity_observation"] = dict(
                                zip(
                                    (
                                        "observation_id",
                                        "state",
                                        "reason",
                                        "previous_status",
                                        "observed_at",
                                    ),
                                    integrity,
                                    strict=True,
                                )
                            )
                    review_id = art["review"].get("review_id")
                    if review_id and stage == "form" and _has(db, "form_review_detail"):
                        when = db.execute(
                            "SELECT created_at FROM form_review_detail WHERE review_id=?",
                            (review_id,),
                        ).fetchone()
                        artifacts[-1]["review"]["recorded_at"] = (
                            when[0] if when else None
                        )
                    elif review_id and stage == "mock" and _has(db, "mock_review"):
                        when = db.execute(
                            "SELECT created_at FROM mock_review WHERE review_id=?",
                            (review_id,),
                        ).fetchone()
                        artifacts[-1]["review"]["recorded_at"] = (
                            when[0] if when else None
                        )
                except (ArtifactReadError, KeyError, ValueError, OSError):
                    artifacts.append(
                        {
                            "artifact_id": artifact_id,
                            "file": {"state": "unverified"},
                            "review": {"state": "unverified"},
                        }
                    )
            try:
                cost = (
                    summarize_generation_cost(store, task_id, root=root, attempt_id=aid)
                    if root
                    else None
                )
            except (ValueError, sqlite3.Error):
                cost = None
            try:
                query_cost = (
                    summarize_query_evidence(store, root, candidate_id, aid)
                    if root and candidate_id
                    else None
                )
            except (ValueError, sqlite3.Error):
                query_cost = None
            if query_cost is not None:
                query_cost.pop("invocations", None)
                query_cost["billing_event_count"] = (
                    db.execute(
                        "SELECT count(*) FROM query_billing_event b JOIN query_invocation q ON q.invocation_id=b.invocation_id WHERE q.root_operation_id=? AND q.candidate_id=? AND q.attempt_id=?",
                        (root, candidate_id, aid),
                    ).fetchone()[0]
                    if _has(db, "query_billing_event") and _has(db, "query_invocation")
                    else None
                )
            evidence = (
                db.execute(
                    "SELECT evidence_id,source,evidence_reference,recorded_at FROM generation_cost_evidence WHERE task_id=? AND attempt_id=? AND root=? ORDER BY rowid",
                    (task_id, aid, root),
                ).fetchall()
                if _has(db, "generation_cost_evidence")
                else []
            )
            linked = (
                db.execute(
                    "SELECT authorization_id,source_root,source_attempt,EXISTS(SELECT 1 FROM risk_reservation_revocation v WHERE v.authorization_id=risk_consumption.authorization_id) FROM risk_consumption WHERE linked_operation=? AND linked_attempt=?",
                    (root, aid),
                ).fetchone()
                if root and _has(db, "risk_consumption")
                else None
            )
            attempt_views.append(
                {
                    "attempt_id": aid,
                    "revision": rev,
                    "stage": stage,
                    "root_operation_id": root,
                    "operation_id": operation,
                    "sequence": seq,
                    "previous_attempt_id": previous,
                    "execution_digest": digest,
                    "candidate_id": candidate_id,
                    "model": model,
                    "endpoint": endpoint if endpoint == ENDPOINT else "unverified",
                    "local_intent": "recorded" if operation else "unknown",
                    "transport": {
                        "phase": transport[0] if transport else "unknown",
                        "recorded_at": transport[1] if transport else None,
                        "verified_local_stop": verified_linked_stop(
                            db, root, aid, task_id, rev, digest
                        )
                        if root and digest
                        else None,
                    },
                    "upstream": {
                        "historical_status": upstream,
                        "upstream_id": upstream_id
                        if isinstance(upstream_id, str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", upstream_id)
                        else None,
                        "current_conclusion": conclusion["state"],
                        "conclusion_reason": conclusion["reason"],
                        "conclusion_observation_id": conclusion["observation_id"],
                        "observations": [
                            {
                                "observation_id": o[0],
                                "state": o[1],
                                "reason": o[2],
                                "observed_at": o[3],
                                "previous_id": o[4],
                                "source_trust": "configured_verifier"
                                if conclusion["reason"] == "recorded_observations"
                                else "unverified",
                            }
                            for o in observations
                        ],
                    },
                    "delivery_status": delivery,
                    "artifacts": artifacts,
                    "generation_cost": cost
                    or {
                        "known_subtotal": {},
                        "total_verified": False,
                        "billing_completeness": "unverified",
                        "cost_status": "unknown",
                    },
                    "supplier_cost_reports": read_supplier_response_costs(
                        store, root, aid
                    )
                    if root and candidate_id
                    else [],
                    "generation_cost_evidence": [
                        {
                            "evidence_id": e[0],
                            "source": e[1],
                            "recorded_at": e[3],
                            "source_trust": "configured_verifier",
                        }
                        for e in evidence
                    ],
                    "query_cost": query_cost
                    or {
                        "invocation_count": None,
                        "cost_status": "unknown",
                        "billing_completeness": "unverified",
                    },
                    "linked_source": {
                        "root_operation_id": linked[1],
                        "attempt_id": linked[2],
                        "revoked": bool(linked[3]),
                    }
                    if linked
                    else None,
                    "time": {
                        "attempt_created_at": None,
                        "intent_at": None,
                        "entered_at": transport[1]
                        if transport and transport[0] == "entered"
                        else None,
                        "remote_completed_at": timing[1] if timing else None,
                        "artifact_delivered_at": None,
                        "duration_seconds": timing[1] - timing[0] if timing else None,
                        "duration_status": "configured_verifier"
                        if timing
                        else "unknown",
                        "remote_started_at": timing[0] if timing else None,
                    },
                }
            )
        try:
            total = summarize_generation_cost(store, task_id) if rows else None
        except (ValueError, sqlite3.Error):
            total = None
        return {
            "task_id": task_id,
            "task_status": task[0],
            "current_revision": current_revision,
            "current_stage": stages.get(current_revision, "unknown"),
            "revisions": [
                {"revision": r[0], "stage": stages.get(r[0], "unknown")}
                for r in revisions
            ],
            "candidates": candidate_views,
            "attempts": attempt_views,
            "generation_cost": total
            or {
                "known_subtotal": {},
                "total_verified": False,
                "billing_completeness": "unverified",
            },
            "query_scope": "local_only",
            "host_token_usage": "not_measured",
        }
