"""Non-executable risk reservations; never authorize or perform generation."""

import json
import math
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import cast

from .domain import ExecutionSnapshot, Revision, Task, TaskStatus
from .execution_candidates import read_candidate
from .storage import Store


@dataclass(frozen=True)
class RiskDisclosure:
    source_root: str
    source_candidate: str
    source_attempt: str
    source_digest: str
    target_operation: str
    snapshot_digest: str
    upstream_status: str
    may_have_sent: bool
    duplicate_generation: bool
    duplicate_cost: bool
    original_cost: str
    allowed_attempts: int


@dataclass(frozen=True)
class RiskEvidence:
    authorization_id: str
    source_root: str
    source_candidate: str
    source_attempt: str
    source_digest: str
    target_operation: str
    snapshot_payload: str
    risk_notice_reference: str
    risk_acceptance_reference: str
    execution_authorization_reference: str
    expires_at: float
    disclosure: RiskDisclosure | None = None


@dataclass(frozen=True)
class RiskReservation:
    operation_id: str
    authorization_id: str
    state: str = "reserved_unverified"
    can_send: bool = False


def _verify_risk_acceptance(db: sqlite3.Connection, evidence: RiskEvidence) -> bool:
    """No independently verified Host risk-acceptance channel exists."""
    return False


def _verify_execution_authorization(
    db: sqlite3.Connection, evidence: RiskEvidence
) -> bool:
    """No independently verified Host execution-authorization channel exists."""
    return False


def _verify_revocation_source(
    db: sqlite3.Connection, authorization_id: str, reference: str
) -> bool:
    return False


def _schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS risk_reservation (
        authorization_id TEXT PRIMARY KEY, source_root TEXT NOT NULL UNIQUE,
        source_candidate TEXT NOT NULL, source_attempt TEXT NOT NULL,
        source_digest TEXT NOT NULL, target_operation TEXT NOT NULL UNIQUE,
        snapshot_payload TEXT NOT NULL, evidence_payload TEXT NOT NULL,
        created_at REAL NOT NULL, state TEXT NOT NULL CHECK(state='reserved_unverified'),
        FOREIGN KEY(source_root) REFERENCES operation(operation_id),
        FOREIGN KEY(source_candidate) REFERENCES execution_candidate(candidate_id),
        FOREIGN KEY(source_attempt) REFERENCES attempt(attempt_id))""")
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS risk_reservation_no_update BEFORE UPDATE ON risk_reservation BEGIN SELECT RAISE(ABORT,'immutable reservation'); END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS risk_reservation_no_delete BEFORE DELETE ON risk_reservation BEGIN SELECT RAISE(ABORT,'immutable reservation'); END"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS risk_reservation_revocation (authorization_id TEXT PRIMARY KEY, reference TEXT NOT NULL, recorded_at REAL NOT NULL, FOREIGN KEY(authorization_id) REFERENCES risk_reservation(authorization_id))"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS risk_revocation_no_update BEFORE UPDATE ON risk_reservation_revocation BEGIN SELECT RAISE(ABORT,'immutable revocation'); END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS risk_revocation_no_delete BEFORE DELETE ON risk_reservation_revocation BEGIN SELECT RAISE(ABORT,'immutable revocation'); END"
    )
    db.execute("""CREATE TABLE IF NOT EXISTS risk_consumption (
        authorization_id TEXT PRIMARY KEY, source_root TEXT NOT NULL,
        source_candidate TEXT NOT NULL, source_attempt TEXT NOT NULL,
        source_digest TEXT NOT NULL, linked_operation TEXT NOT NULL UNIQUE,
        linked_attempt TEXT NOT NULL UNIQUE, linked_candidate TEXT NOT NULL UNIQUE,
        execution_digest TEXT NOT NULL, consumed_at REAL NOT NULL,
        FOREIGN KEY(authorization_id) REFERENCES risk_reservation(authorization_id),
        FOREIGN KEY(linked_operation) REFERENCES operation(operation_id),
        FOREIGN KEY(linked_attempt) REFERENCES attempt(attempt_id),
        FOREIGN KEY(linked_candidate) REFERENCES execution_candidate(candidate_id))""")
    for action in ("UPDATE", "DELETE"):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS risk_consumption_no_{action.lower()} BEFORE {action} ON risk_consumption BEGIN SELECT RAISE(ABORT,'immutable consumption'); END"
        )


def verify_consumption(
    db: sqlite3.Connection,
    authorization_id: str,
    target: str,
    snapshot: ExecutionSnapshot,
) -> RiskEvidence:
    """Recheck the persisted, independently verified scope under the caller's write lock."""
    _schema(db)
    row = db.execute(
        "SELECT source_root,source_candidate,source_attempt,source_digest,target_operation,snapshot_payload,evidence_payload FROM risk_reservation WHERE authorization_id=?",
        (authorization_id,),
    ).fetchone()
    if not row:
        raise ValueError("reservation_missing")
    data = json.loads(row[6])["request"]
    if data.get("disclosure") is not None:
        data["disclosure"] = RiskDisclosure(**data["disclosure"])
    evidence = RiskEvidence(**data)
    d = evidence.disclosure
    if (
        (
            evidence.authorization_id,
            evidence.source_root,
            evidence.source_candidate,
            evidence.source_attempt,
            evidence.source_digest,
            evidence.target_operation,
            evidence.snapshot_payload,
        )
        != (authorization_id, *row[:6])
        or target != evidence.target_operation
        or snapshot.to_payload() != evidence.snapshot_payload
        or snapshot.digest_version != 2
        or evidence.source_digest != snapshot.execution_digest
        or not math.isfinite(evidence.expires_at)
        or evidence.expires_at <= time.time()
        or d is None
        or (
            d.source_root,
            d.source_candidate,
            d.source_attempt,
            d.source_digest,
            d.target_operation,
            d.snapshot_digest,
            d.upstream_status,
            d.original_cost,
        )
        != (
            evidence.source_root,
            evidence.source_candidate,
            evidence.source_attempt,
            evidence.source_digest,
            target,
            snapshot.execution_digest,
            "unknown",
            "unknown",
        )
        or d.may_have_sent is not True
        or d.duplicate_generation is not True
        or d.duplicate_cost is not True
        or type(d.allowed_attempts) is not int
        or d.allowed_attempts != 1
    ):
        raise ValueError("reservation_scope_invalid")
    if db.execute(
        "SELECT 1 FROM risk_reservation_revocation WHERE authorization_id=?",
        (authorization_id,),
    ).fetchone():
        raise ValueError("authorization_revoked")
    if not _verify_risk_acceptance(db, evidence) or not _verify_execution_authorization(
        db, evidence
    ):
        raise ValueError("source_unverified")
    candidate = db.execute(
        "SELECT root_operation_id,task_id,revision,execution_digest,snapshot_payload FROM execution_candidate WHERE candidate_id=?",
        (evidence.source_candidate,),
    ).fetchone()
    if candidate != (
        evidence.source_root,
        snapshot.task_id,
        snapshot.revision,
        evidence.source_digest,
        evidence.snapshot_payload,
    ):
        raise ValueError("source_candidate_mismatch")
    source_parent = db.execute(
        "SELECT source_candidate_id FROM execution_candidate WHERE candidate_id=?",
        (evidence.source_candidate,),
    ).fetchone()
    if (
        source_parent
        and source_parent[0] is not None
        and db.execute(
            "SELECT candidate_id,root_operation_id FROM candidate_attempt WHERE attempt_id=?",
            (evidence.source_attempt,),
        ).fetchone()
        != (evidence.source_candidate, evidence.source_root)
    ):
        raise ValueError("source_candidate_attempt_mismatch")
    original = db.execute(
        "SELECT a.upstream_status,a.upstream_id,t.phase,t.logical_operation_id,t.task_id,t.revision,t.execution_digest,c.execution_digest,t.reason,t.source "
        "FROM attempt a JOIN operation_attempt c ON c.attempt_id=a.attempt_id LEFT JOIN attempt_transport_evidence t ON t.attempt_id=a.attempt_id WHERE c.operation_id=? AND a.attempt_id=?",
        (evidence.source_root, evidence.source_attempt),
    ).fetchone()
    if (
        not original
        or original[0] not in ("unknown", "submission_uncertain")
        or original[1:]
        != (
            None,
            "entered",
            evidence.source_root,
            snapshot.task_id,
            snapshot.revision,
            evidence.source_digest,
            evidence.source_digest,
            "dispatch_boundary",
            "server_pre_dispatch",
        )
    ):
        raise ValueError("original_state_unverified")
    if db.execute(
        "SELECT 1 FROM recovery_observation WHERE attempt_id=?",
        (evidence.source_attempt,),
    ).fetchone():
        raise ValueError("original_observation_requires_review")
    budget = db.execute(
        "SELECT count(*),coalesce(sum(count),0) FROM attempt_counter WHERE operation_id=?",
        (evidence.source_root,),
    ).fetchone()
    attempts = db.execute(
        "SELECT count(*) FROM operation_attempt WHERE operation_id=?",
        (evidence.source_root,),
    ).fetchone()
    if not budget or not budget[0] or not attempts or budget[1] != attempts[0]:
        raise ValueError("budget_unverified")
    root = db.execute(
        "SELECT task_id,revision,attempt_count FROM operation WHERE operation_id=?",
        (evidence.source_root,),
    ).fetchone()
    last = db.execute(
        "SELECT sequence,attempt_id FROM operation_attempt WHERE operation_id=? ORDER BY sequence DESC LIMIT 1",
        (evidence.source_root,),
    ).fetchone()
    if root != (snapshot.task_id, snapshot.revision, attempts[0]) or last != (
        attempts[0],
        evidence.source_attempt,
    ):
        raise ValueError("original_chain_unverified")
    chain = db.execute(
        "SELECT c.sequence,c.attempt_id,c.previous_attempt_id,c.execution_digest,"
        "a.previous_attempt_id,a.task_id,a.revision,o.task_id,o.revision,o.digest,c.model,c.endpoint "
        "FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id "
        "JOIN operation o ON o.attempt_id=c.attempt_id WHERE c.operation_id=? ORDER BY c.sequence",
        (evidence.source_root,),
    ).fetchall()
    previous = None
    combinations: dict[tuple[str, str], int] = {}
    for sequence, item in enumerate(chain, 1):
        if (
            item[0] != sequence
            or item[2] != previous
            or item[4] != previous
            or item[5:9]
            != (
                snapshot.task_id,
                snapshot.revision,
                snapshot.task_id,
                snapshot.revision,
            )
            or item[3] != item[9]
        ):
            raise ValueError("original_chain_unverified")
        previous = item[1]
        key = (item[10], item[11])
        combinations[key] = combinations.get(key, 0) + 1
    if len(chain) != attempts[0]:
        raise ValueError("original_chain_unverified")
    counters = db.execute(
        "SELECT model,endpoint,count FROM attempt_counter WHERE operation_id=?",
        (evidence.source_root,),
    ).fetchall()
    if (
        {(item[0], item[1]): item[2] for item in counters} != combinations
        or attempts[0] > 6
        or len(combinations) > 2
        or any(count > 3 for count in combinations.values())
    ):
        raise ValueError("budget_unverified")
    if db.execute(
        "SELECT 1 FROM risk_consumption WHERE linked_operation=?",
        (evidence.source_root,),
    ).fetchone():
        raise ValueError("risk_lineage_unverified")
    return evidence


def reserve(store: Store, evidence: RiskEvidence) -> RiskReservation:
    """Store a single inert proposal only after independent controlled verifiers agree."""
    e = evidence
    if (
        not all(
            isinstance(x, str) and x.strip() and len(x) <= 256
            for x in (
                e.authorization_id,
                e.source_root,
                e.source_candidate,
                e.source_attempt,
                e.source_digest,
                e.target_operation,
                e.risk_notice_reference,
                e.risk_acceptance_reference,
                e.execution_authorization_reference,
            )
        )
        or not math.isfinite(e.expires_at)
        or e.expires_at <= time.time()
    ):
        raise ValueError("invalid_or_expired_evidence")
    snap = ExecutionSnapshot.from_payload(e.snapshot_payload)
    candidate = read_candidate(store, e.source_candidate)
    if (
        candidate.root_operation_id != e.source_root
        or candidate.snapshot != snap
        or snap.execution_digest != e.source_digest
        or snap.digest_version != 2
    ):
        raise ValueError("snapshot_mismatch")
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _schema(db)
            if db.execute(
                "SELECT 1 FROM risk_consumption WHERE linked_operation=?",
                (e.source_root,),
            ).fetchone():
                raise ValueError("risk_lineage_unverified")
            if e.expires_at <= time.time():
                raise ValueError("invalid_or_expired_evidence")
            if not _verify_risk_acceptance(
                db, e
            ) or not _verify_execution_authorization(db, e):
                raise ValueError("source_unverified")
            disclosure = e.disclosure
            if (
                disclosure is None
                or (
                    disclosure.source_root,
                    disclosure.source_candidate,
                    disclosure.source_attempt,
                    disclosure.source_digest,
                    disclosure.target_operation,
                    disclosure.snapshot_digest,
                    disclosure.upstream_status,
                    disclosure.original_cost,
                )
                != (
                    e.source_root,
                    e.source_candidate,
                    e.source_attempt,
                    e.source_digest,
                    e.target_operation,
                    snap.execution_digest,
                    "unknown",
                    "unknown",
                )
                or (
                    disclosure.may_have_sent is not True
                    or disclosure.duplicate_generation is not True
                    or disclosure.duplicate_cost is not True
                    or type(disclosure.allowed_attempts) is not int
                    or disclosure.allowed_attempts != 1
                )
            ):
                raise ValueError("risk_disclosure_mismatch")
            if db.execute(
                "SELECT 1 FROM risk_reservation_revocation WHERE authorization_id=?",
                (e.authorization_id,),
            ).fetchone():
                raise ValueError("authorization_revoked")
            if read_candidate(store, e.source_candidate) != candidate:
                raise ValueError("snapshot_mismatch")
            current = db.execute(
                "SELECT status,(SELECT MAX(revision) FROM revision WHERE task_id=?) FROM task WHERE task_id=?",
                (snap.task_id, snap.task_id),
            ).fetchone()
            if current != ("ready", snap.revision):
                raise ValueError("current_revision_unverified")
            from .offline_submission import _check_business_gate
            from .tasks import LocalEligibility

            LocalEligibility._current_review(db, snap)
            task_row = db.execute(
                "SELECT instructions FROM task WHERE task_id=?", (snap.task_id,)
            ).fetchone()
            if not task_row:
                raise ValueError("current_revision_unverified")
            _check_business_gate(
                db,
                cast(LocalEligibility, SimpleNamespace(store=store)),
                Task(snap.task_id, snap.input_digest, task_row[0], TaskStatus.READY),
                Revision(snap.task_id, snap.revision, snap.input_digest),
            )
            previous = db.execute(
                "SELECT target_operation,evidence_payload FROM risk_reservation WHERE authorization_id=?",
                (e.authorization_id,),
            ).fetchone()
            row = db.execute(
                "SELECT a.upstream_status,a.upstream_id,t.phase,t.logical_operation_id,t.task_id,t.revision,t.execution_digest,c.execution_digest,t.reason,t.source FROM attempt a JOIN operation_attempt c ON c.attempt_id=a.attempt_id LEFT JOIN attempt_transport_evidence t ON t.attempt_id=a.attempt_id WHERE c.operation_id=? AND a.attempt_id=?",
                (e.source_root, e.source_attempt),
            ).fetchone()
            observations = db.execute(
                "SELECT remote_state FROM recovery_observation WHERE attempt_id=? ORDER BY observation_id",
                (e.source_attempt,),
            ).fetchall()
            if any(item[0] == "conflict" for item in observations):
                raise ValueError("original_observation_conflict")
            if row and row[0] in ("running", "pending", "accepted"):
                raise ValueError("original_processing")
            if row and row[0] == "succeeded":
                raise ValueError("original_succeeded")
            if row and row[1] is not None:
                raise ValueError("query_original")
            if (
                not row
                or row[0] not in ("submission_uncertain", "unknown")
                or row[1:]
                != (
                    None,
                    "entered",
                    e.source_root,
                    snap.task_id,
                    snap.revision,
                    e.source_digest,
                    e.source_digest,
                    "dispatch_boundary",
                    "server_pre_dispatch",
                )
            ):
                raise ValueError("original_state_unverified")
            if observations:
                raise ValueError("original_observation_requires_review")
            payload = json.dumps(
                {
                    "request": asdict(e),
                    "observed": {
                        "upstream_status": row[0],
                        "upstream_id": None,
                        "transport_phase": row[2],
                        "query_path": "no_upstream_id",
                        "observation_count": 0,
                        "cost_status": "unknown",
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            budget = db.execute(
                "SELECT count(*),coalesce(sum(count),0) FROM attempt_counter WHERE operation_id=?",
                (e.source_root,),
            ).fetchone()
            attempts = db.execute(
                "SELECT count(*) FROM operation_attempt WHERE operation_id=?",
                (e.source_root,),
            ).fetchone()
            if not budget or not attempts or not budget[0] or budget[1] != attempts[0]:
                raise ValueError("budget_unverified")
            if e.expires_at <= time.time():
                raise ValueError("invalid_or_expired_evidence")
            if previous:
                if previous != (e.target_operation, payload):
                    raise ValueError("authorization_conflict")
                db.rollback()
                return RiskReservation(e.target_operation, e.authorization_id)
            if db.execute(
                "SELECT 1 FROM risk_reservation WHERE source_root=? OR target_operation=?",
                (e.source_root, e.target_operation),
            ).fetchone():
                raise ValueError("reservation_conflict")
            if db.execute(
                "SELECT 1 FROM operation WHERE operation_id=?", (e.target_operation,)
            ).fetchone():
                raise ValueError("target_conflict")
            db.execute(
                "INSERT INTO risk_reservation VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    e.authorization_id,
                    e.source_root,
                    e.source_candidate,
                    e.source_attempt,
                    e.source_digest,
                    e.target_operation,
                    e.snapshot_payload,
                    payload,
                    time.time(),
                    "reserved_unverified",
                ),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return RiskReservation(e.target_operation, e.authorization_id)


def revoke(store: Store, authorization_id: str, reference: str) -> None:
    if not authorization_id or not reference:
        raise ValueError("invalid_revocation")
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _schema(db)
            if not _verify_revocation_source(db, authorization_id, reference):
                raise ValueError("source_unverified")
            previous = db.execute(
                "SELECT reference FROM risk_reservation_revocation WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
            if previous:
                if previous != (reference,):
                    raise ValueError("revocation_conflict")
            elif not db.execute(
                "SELECT 1 FROM risk_reservation WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone():
                raise ValueError("authorization_not_found")
            else:
                db.execute(
                    "INSERT INTO risk_reservation_revocation VALUES(?,?,?)",
                    (authorization_id, reference, time.time()),
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
