"""Inert immutable execution candidates; this module never creates eligibility or sends."""

import hashlib
import math
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass

from .domain import ExecutionSnapshot
from .preparation import read_prepared
from .storage import Store


class CandidateError(ValueError):
    """Candidate identity, scope, or historical evidence is invalid."""


@dataclass(frozen=True)
class ExecutionCandidate:
    candidate_id: str
    root_operation_id: str
    source_candidate_id: str | None
    snapshot: ExecutionSnapshot
    prompt: str
    references: tuple[tuple[str, str, str, bytes], ...]
    created_at: float
    capability_state: str = "unverified"
    authorization_state: str = "unverified"
    eligibility_state: str = "none"
    can_send: bool = False


def _frozen(
    store: Store, snapshot: ExecutionSnapshot
) -> tuple[str, tuple[tuple[str, str, str, bytes], ...]]:
    saved = read_prepared(store, snapshot.task_id, snapshot.revision)
    if not saved.get("snapshot_payload"):
        raise CandidateError("Historical snapshot cannot establish a candidate")
    base = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
    if base.digest_version != 2 or (
        snapshot.task_id,
        snapshot.revision,
        snapshot.input_digest,
        snapshot.stage,
        snapshot.prompt_revision,
        snapshot.prompt_digest,
        snapshot.references,
        snapshot.operation_scope,
    ) != (
        base.task_id,
        base.revision,
        base.input_digest,
        base.stage,
        base.prompt_revision,
        base.prompt_digest,
        base.references,
        base.operation_scope,
    ):
        raise CandidateError("Candidate changes frozen business content")
    with closing(store._connect()) as db:
        rows = db.execute(
            "SELECT ref_id,mime,sha256,data,category FROM prepared_reference WHERE task_id=? AND revision=? ORDER BY position",
            (snapshot.task_id, snapshot.revision),
        ).fetchall()
    refs = tuple(
        (r[0], r[1], r[2], r[3])
        for r in rows
        if snapshot.stage == "mock" or r[4] == "form"
    )
    if tuple((r[0], r[2]) for r in refs) != snapshot.references or any(
        hashlib.sha256(r[3]).hexdigest() != r[2] for r in refs
    ):
        raise CandidateError("Frozen candidate images are inconsistent")
    return saved["prompt"], refs


def read_candidate(store: Store, candidate_id: str) -> ExecutionCandidate:
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT candidate_id,root_operation_id,source_candidate_id,snapshot_payload,execution_digest,created_at,task_id,revision FROM execution_candidate WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if not row:
            raise CandidateError("Candidate not found")
        root = db.execute(
            "SELECT task_id,revision,digest FROM operation WHERE operation_id=?",
            (row[1],),
        ).fetchone()
        parent = (
            db.execute(
                "SELECT root_operation_id,task_id,revision FROM execution_candidate WHERE candidate_id=?",
                (row[2],),
            ).fetchone()
            if row[2]
            else None
        )
    try:
        snapshot = ExecutionSnapshot.from_payload(row[3])
        if (
            snapshot.digest_version != 2
            or (snapshot.task_id, snapshot.revision, snapshot.execution_digest)
            != (row[6], row[7], row[4])
            or not root
            or root[:2] != (row[6], row[7])
            or (row[2] is not None and parent != (row[1], row[6], row[7]))
            or (row[2] is None and root[2] != row[4])
            or not math.isfinite(row[5])
            or row[5] <= 0
        ):
            raise CandidateError("Candidate relationship is invalid")
        prompt, refs = _frozen(store, snapshot)
        if (
            root[2]
            != read_prepared(store, snapshot.task_id, snapshot.revision)[
                "execution_digest"
            ]
        ):
            # A risk-linked root may retain the alternate source's exact v2
            # snapshot. Its immutable consumption and source chain must agree.
            with closing(store._connect()) as db:
                tables = {
                    item[0]
                    for item in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('risk_consumption','risk_reservation')"
                    )
                }
                if (
                    tables != {"risk_consumption", "risk_reservation"}
                    or row[2] is not None
                ):
                    raise CandidateError("Root execution digest changed")
                link = db.execute(
                    "SELECT r.source_root,r.source_candidate,r.source_attempt,r.source_digest,r.linked_attempt,r.execution_digest,s.snapshot_payload FROM risk_consumption r JOIN risk_reservation s ON s.authorization_id=r.authorization_id WHERE r.linked_operation=? AND r.linked_candidate=? AND s.source_root=r.source_root AND s.source_candidate=r.source_candidate AND s.source_attempt=r.source_attempt AND s.source_digest=r.source_digest AND s.target_operation=r.linked_operation",
                    (row[1], candidate_id),
                ).fetchone()
                if (
                    not link
                    or link[3] != row[4]
                    or link[5] != row[4]
                    or link[6] != row[3]
                ):
                    raise CandidateError("Linked candidate scope mismatch")
                if db.execute(
                    "SELECT 1 FROM risk_consumption WHERE linked_operation=?",
                    (link[0],),
                ).fetchone():
                    raise CandidateError("Nested linked candidate is unsupported")
                if (
                    db.execute(
                        "SELECT 1 FROM operation_attempt WHERE operation_id=? AND attempt_id=? AND execution_digest=?",
                        (link[0], link[2], link[3]),
                    ).fetchone()
                    is None
                    or db.execute(
                        "SELECT 1 FROM operation_attempt WHERE operation_id=? AND sequence=1 AND attempt_id=? AND execution_digest=?",
                        (row[1], link[4], row[4]),
                    ).fetchone()
                    is None
                    or db.execute(
                        "SELECT candidate_id,root_operation_id FROM candidate_attempt WHERE attempt_id=?",
                        (link[2],),
                    ).fetchone()
                    != (link[1], link[0])
                ):
                    raise CandidateError("Linked candidate attempt mismatch")
            source = read_candidate(store, link[1])
            if source.root_operation_id != link[0] or source.snapshot != snapshot:
                raise CandidateError("Linked candidate source mismatch")
        return ExecutionCandidate(
            row[0], row[1], row[2], snapshot, prompt, refs, row[5]
        )
    except (TypeError, ValueError) as exc:
        raise CandidateError("Candidate integrity failure") from exc


def register_root_candidate(
    store: Store, root: str, candidate_id: str
) -> ExecutionCandidate:
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT o.task_id,o.revision,o.digest FROM operation o JOIN operation_attempt c ON c.operation_id=o.operation_id AND c.sequence=1 AND c.attempt_id=o.attempt_id AND c.execution_digest=o.digest WHERE o.operation_id=?",
            (root,),
        ).fetchone()
    if not row:
        raise CandidateError("Root operation history is incomplete")
    saved = read_prepared(store, row[0], row[1])
    if not saved.get("snapshot_payload"):
        raise CandidateError("Root snapshot is unavailable")
    snapshot = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
    if snapshot.execution_digest != row[2] or snapshot.digest_version != 2:
        raise CandidateError("Root snapshot mismatch")
    return _insert(store, root, candidate_id, None, snapshot)


def create_candidate(
    store: Store,
    root: str,
    candidate_id: str,
    source_id: str,
    snapshot: ExecutionSnapshot,
) -> ExecutionCandidate:
    source = read_candidate(store, source_id)
    if (
        source.root_operation_id != root
        or source.snapshot.task_id != snapshot.task_id
        or source.snapshot.revision != snapshot.revision
        or source.snapshot.execution_digest == snapshot.execution_digest
    ):
        raise CandidateError("Candidate source or execution mismatch")
    return _insert(store, root, candidate_id, source_id, snapshot)


def _insert(
    store: Store,
    root: str,
    candidate_id: str,
    source_id: str | None,
    snapshot: ExecutionSnapshot,
) -> ExecutionCandidate:
    if not root or not candidate_id or snapshot.digest_version != 2:
        raise CandidateError("Invalid candidate identity or version")
    _frozen(store, snapshot)
    root_digest = read_prepared(store, snapshot.task_id, snapshot.revision)[
        "execution_digest"
    ]
    try:
        with closing(store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            root_row = db.execute(
                "SELECT task_id,revision,digest FROM operation WHERE operation_id=?",
                (root,),
            ).fetchone()
            if not root_row or root_row != (
                snapshot.task_id,
                snapshot.revision,
                root_digest,
            ):
                raise CandidateError("Root scope mismatch")
            if source_id:
                source = db.execute(
                    "SELECT root_operation_id,task_id,revision FROM execution_candidate WHERE candidate_id=?",
                    (source_id,),
                ).fetchone()
                if source != (root, snapshot.task_id, snapshot.revision):
                    raise CandidateError("Source scope mismatch")
            elif root_row[2] != snapshot.execution_digest:
                raise CandidateError("Root digest mismatch")
            prior = db.execute(
                "SELECT root_operation_id,source_candidate_id,snapshot_payload FROM execution_candidate WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            expected = (root, source_id, snapshot.to_payload())
            if prior is not None:
                if prior != expected:
                    raise CandidateError("Candidate identifier conflict")
            else:
                db.execute(
                    "INSERT INTO execution_candidate VALUES(?,?,?,?,?,?,?,?)",
                    (
                        candidate_id,
                        root,
                        snapshot.task_id,
                        snapshot.revision,
                        source_id,
                        snapshot.to_payload(),
                        snapshot.execution_digest,
                        time.time(),
                    ),
                )
    except sqlite3.IntegrityError:
        raise CandidateError("Candidate conflicts with existing history") from None
    return read_candidate(store, candidate_id)
