"""Read-only validation of locally recorded transport-boundary evidence."""

import math
import re
import sqlite3
import time


def _linked_stop_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS linked_dispatch_stop (
        attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
        task_id TEXT NOT NULL, revision INTEGER NOT NULL, execution_digest TEXT NOT NULL,
        authorization_id TEXT NOT NULL, entered INTEGER NOT NULL CHECK(entered IN (0,1)),
        reason TEXT NOT NULL CHECK(reason='local_gate'),
        source TEXT NOT NULL CHECK(source='server_pre_dispatch'), recorded_at REAL NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
        FOREIGN KEY(authorization_id) REFERENCES risk_reservation(authorization_id))""")
    for action in ("UPDATE", "DELETE"):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS linked_dispatch_stop_no_{action.lower()} BEFORE {action} ON linked_dispatch_stop BEGIN SELECT RAISE(ABORT,'immutable stop'); END"
        )


def _record_linked_stop(
    db: sqlite3.Connection, operation_id: str, attempt_id: str, entered: bool
) -> None:
    """Only call from the controlled path before dispatch has been invoked."""
    _linked_stop_schema(db)
    marker = db.execute(
        "SELECT phase FROM attempt_transport_evidence WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if marker != (("entered",) if entered else None):
        raise ValueError("transport_boundary_conflict")
    inserted = db.execute(
        "INSERT INTO linked_dispatch_stop "
        "SELECT a.attempt_id,o.operation_id,o.task_id,o.revision,o.digest,r.authorization_id,?,"
        "'local_gate','server_pre_dispatch',? FROM operation o "
        "JOIN attempt a ON a.attempt_id=o.attempt_id "
        "JOIN risk_consumption r ON r.linked_operation=o.operation_id AND r.linked_attempt=a.attempt_id AND r.execution_digest=o.digest "
        "WHERE o.operation_id=? AND a.attempt_id=? AND a.upstream_id IS NULL AND a.upstream_status='submission_uncertain'",
        (int(entered), time.time(), operation_id, attempt_id),
    )
    if inserted.rowcount != 1:
        raise ValueError("linked_stop_identity_invalid")


def verified_linked_stop(
    db: sqlite3.Connection,
    root: str,
    attempt: str,
    task_id: str,
    revision: int,
    digest: str,
) -> str | None:
    """Return only a bound server stop fact; it never authorizes another send."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='linked_dispatch_stop'"
    ).fetchone():
        return None
    row = db.execute(
        "SELECT s.entered,e.phase,s.recorded_at FROM linked_dispatch_stop s "
        "JOIN risk_consumption r ON r.authorization_id=s.authorization_id AND r.linked_operation=s.operation_id AND r.linked_attempt=s.attempt_id AND r.execution_digest=s.execution_digest "
        "JOIN operation o ON o.operation_id=s.operation_id AND o.attempt_id=s.attempt_id AND o.task_id=s.task_id AND o.revision=s.revision AND o.digest=s.execution_digest "
        "JOIN attempt a ON a.attempt_id=s.attempt_id AND a.task_id=s.task_id AND a.revision=s.revision "
        "JOIN operation_attempt c ON c.operation_id=s.operation_id AND c.attempt_id=s.attempt_id AND c.sequence=1 AND c.execution_digest=s.execution_digest "
        "JOIN execution_candidate ec ON ec.candidate_id=r.linked_candidate AND ec.root_operation_id=s.operation_id AND ec.task_id=s.task_id AND ec.revision=s.revision AND ec.execution_digest=s.execution_digest "
        "LEFT JOIN attempt_transport_evidence e ON e.attempt_id=s.attempt_id "
        "WHERE s.operation_id=? AND s.attempt_id=? AND s.task_id=? AND s.revision=? AND s.execution_digest=? "
        "AND s.reason='local_gate' AND s.source='server_pre_dispatch' AND a.upstream_id IS NULL AND a.upstream_status IN ('unknown','submission_uncertain') "
        "AND (e.attempt_id IS NULL OR (e.operation_id=s.operation_id AND e.logical_operation_id=s.operation_id AND e.task_id=s.task_id AND e.revision=s.revision AND e.execution_digest=s.execution_digest AND e.reason='dispatch_boundary' AND e.source='server_pre_dispatch'))",
        (root, attempt, task_id, revision, digest),
    ).fetchone()
    if (
        not row
        or not isinstance(row[2], (float, int))
        or not math.isfinite(row[2])
        or row[2] <= 0
    ):
        return None
    if row[:2] == (0, None):
        return "stopped_before_entered"
    if row[:2] == (1, "entered"):
        return "entered_then_stopped"
    return None


def verified_pre_send(
    db: sqlite3.Connection,
    root: str,
    attempt: str,
    task_id: str,
    revision: int,
    digest: str,
) -> bool:
    """A missing marker is unknown; only a bound explicit local failure is evidence."""
    row = db.execute(
        "SELECT e.phase,e.reason,e.source,a.upstream_status,a.upstream_id,e.recorded_at "
        "FROM attempt_transport_evidence e JOIN attempt a ON a.attempt_id=e.attempt_id "
        "JOIN operation o ON o.operation_id=e.operation_id AND o.attempt_id=e.attempt_id "
        "JOIN operation_attempt c ON c.attempt_id=e.attempt_id AND c.operation_id=e.logical_operation_id "
        "JOIN operation r ON r.operation_id=c.operation_id "
        "WHERE e.attempt_id=? AND e.logical_operation_id=? AND e.task_id=? AND e.revision=? "
        "AND e.execution_digest=? AND o.task_id=e.task_id AND o.revision=e.revision AND o.digest=e.execution_digest "
        "AND a.task_id=e.task_id AND a.revision=e.revision AND c.execution_digest=e.execution_digest "
        "AND r.task_id=e.task_id AND r.revision=e.revision AND ((r.digest=e.execution_digest AND NOT EXISTS (SELECT 1 FROM candidate_attempt ca0 WHERE ca0.attempt_id=e.attempt_id)) OR EXISTS ("
        "SELECT 1 FROM candidate_attempt ca JOIN execution_candidate ec ON ec.candidate_id=ca.candidate_id "
        "WHERE ca.attempt_id=e.attempt_id AND ca.root_operation_id=r.operation_id AND ec.root_operation_id=r.operation_id "
        "AND ec.task_id=e.task_id AND ec.revision=e.revision AND ec.execution_digest=e.execution_digest))",
        (attempt, root, task_id, revision, digest),
    ).fetchone()
    return bool(
        row
        and row[:3] == ("pre_send_failure", "local_validation", "server_pre_dispatch")
        and row[3] in ("submission_uncertain", "unknown")
        and row[4] is None
        and isinstance(row[5], (int, float))
        and math.isfinite(row[5])
        and row[5] > 0
    )


def verified_recoverability(
    db: sqlite3.Connection,
    root: str,
    attempt: str,
    task_id: str,
    revision: int,
    digest: str,
) -> str:
    """Read bound server evidence; missing, conflicting or legacy records are unknown."""
    if not verified_pre_send(db, root, attempt, task_id, revision, digest):
        return "unknown"
    row = db.execute(
        "SELECT f.category FROM attempt_fault_evidence f "
        "JOIN attempt_transport_evidence e ON e.attempt_id=f.attempt_id "
        "WHERE f.attempt_id=? AND f.logical_operation_id=? AND f.task_id=? "
        "AND f.revision=? AND f.execution_digest=? AND f.source='server_pre_dispatch' "
        "AND f.logical_operation_id=e.logical_operation_id AND f.task_id=e.task_id "
        "AND f.revision=e.revision AND f.execution_digest=e.execution_digest "
        "AND f.recorded_at=e.recorded_at",
        (attempt, root, task_id, revision, digest),
    ).fetchone()
    allowed = {
        "deterministic_input",
        "deterministic_configuration",
        "authorization_gate",
        "transient_local",
    }
    return row[0] if row and row[0] in allowed else "unknown"


def safe_failure_kind(
    db: sqlite3.Connection,
    root: str,
    attempt: str,
    task_id: str,
    revision: int,
    digest: str,
    *,
    remote_verified: bool = False,
) -> str:
    """Classify bound facts; verified remote evidence is supplied only by an internal verifier."""
    row = db.execute(
        "SELECT a.upstream_status,a.upstream_id,e.phase FROM operation_attempt c "
        "JOIN attempt a ON a.attempt_id=c.attempt_id JOIN operation r ON r.operation_id=c.operation_id "
        "JOIN operation o ON o.attempt_id=c.attempt_id LEFT JOIN attempt_transport_evidence e ON e.attempt_id=c.attempt_id "
        "WHERE c.operation_id=? AND c.attempt_id=? AND c.execution_digest=? AND a.task_id=? AND a.revision=? "
        "AND r.task_id=a.task_id AND r.revision=a.revision AND o.task_id=a.task_id AND o.revision=a.revision AND o.digest=c.execution_digest "
        "AND ((r.digest=c.execution_digest AND NOT EXISTS (SELECT 1 FROM candidate_attempt ca0 WHERE ca0.attempt_id=c.attempt_id)) OR EXISTS (SELECT 1 FROM candidate_attempt ca JOIN execution_candidate ec ON ec.candidate_id=ca.candidate_id "
        "WHERE ca.attempt_id=c.attempt_id AND ca.root_operation_id=r.operation_id AND ec.root_operation_id=r.operation_id "
        "AND ec.task_id=a.task_id AND ec.revision=a.revision AND ec.execution_digest=c.execution_digest))",
        (root, attempt, digest, task_id, revision),
    ).fetchone()
    if not row:
        return "unverified"
    if row[2] == "pre_send_failure":
        if not verified_pre_send(db, root, attempt, task_id, revision, digest):
            return "conflict"
        return (
            "pre_send"
            if verified_recoverability(db, root, attempt, task_id, revision, digest)
            == "transient_local"
            else "not_recoverable"
        )
    valid_upstream = isinstance(row[1], str) and bool(
        re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row[1])
    )
    if row[0] == "failed" and valid_upstream:
        return "remote_terminal" if remote_verified else "remote_unverified"
    if valid_upstream and row[0] in {
        "unknown",
        "submission_uncertain",
        "accepted",
        "pending",
        "running",
    }:
        return "query_original"
    return "unverified"
