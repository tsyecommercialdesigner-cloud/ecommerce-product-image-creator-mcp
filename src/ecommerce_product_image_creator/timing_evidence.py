"""Bound provider timing evidence; no production timing source is configured."""

import math
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass

from . import original_query
from .storage import Store


@dataclass(frozen=True)
class TimingEvidence:
    root: str
    candidate_id: str
    task_id: str
    revision: int
    stage: str
    attempt_id: str
    execution_digest: str
    started_at: float
    completed_at: float
    source: str


def _verify_timing_source(db: sqlite3.Connection, evidence: TimingEvidence) -> bool:
    """The production provider has no verified clock or timestamp protocol."""
    return False


def record_timing(store: Store, evidence: TimingEvidence) -> None:
    e = evidence
    if (
        not all(math.isfinite(v) and v > 0 for v in (e.started_at, e.completed_at))
        or e.completed_at < e.started_at
        or not math.isfinite(e.completed_at - e.started_at)
        or not e.source
        or len(e.source) > 128
    ):
        raise ValueError("invalid_timing")
    identity = original_query._identity(
        store,
        e.root,
        e.candidate_id,
        e.task_id,
        e.revision,
        e.attempt_id,
        e.execution_digest,
    )
    if identity is None or identity.stage != e.stage:
        raise ValueError("timing_identity_unverified")
    with closing(store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if not original_query._verify_original_binding(
            db, identity
        ) or not _verify_timing_source(db, e):
            raise ValueError("timing_source_unverified")
        owner = db.execute(
            "SELECT a.upstream_id,a.task_id,a.revision,c.execution_digest FROM attempt a JOIN operation_attempt c ON c.attempt_id=a.attempt_id WHERE c.operation_id=? AND a.attempt_id=?",
            (e.root, e.attempt_id),
        ).fetchone()
        if owner != (identity.upstream_id, e.task_id, e.revision, e.execution_digest):
            raise ValueError("timing_identity_changed")
        db.execute("""CREATE TABLE IF NOT EXISTS attempt_timing_evidence (
            attempt_id TEXT PRIMARY KEY, root TEXT NOT NULL, candidate_id TEXT NOT NULL,
            task_id TEXT NOT NULL, revision INTEGER NOT NULL, stage TEXT NOT NULL,
            execution_digest TEXT NOT NULL, started_at REAL NOT NULL, completed_at REAL NOT NULL,
            source TEXT NOT NULL, recorded_at REAL NOT NULL,
            FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
            FOREIGN KEY(candidate_id) REFERENCES execution_candidate(candidate_id))""")
        for action in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS attempt_timing_no_{action.lower()} BEFORE {action} ON attempt_timing_evidence BEGIN SELECT RAISE(ABORT,'immutable timing'); END"
            )
        values = (
            e.attempt_id,
            e.root,
            e.candidate_id,
            e.task_id,
            e.revision,
            e.stage,
            e.execution_digest,
            e.started_at,
            e.completed_at,
            e.source,
        )
        old = db.execute(
            "SELECT attempt_id,root,candidate_id,task_id,revision,stage,execution_digest,started_at,completed_at,source FROM attempt_timing_evidence WHERE attempt_id=?",
            (e.attempt_id,),
        ).fetchone()
        if old is not None:
            if old != values:
                raise ValueError("timing_conflict")
            return
        db.execute(
            "INSERT INTO attempt_timing_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (*values, time.time()),
        )
