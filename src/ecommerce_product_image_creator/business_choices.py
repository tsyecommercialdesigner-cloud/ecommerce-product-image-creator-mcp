"""Locally persisted, scope-bound business choices; never billing authorization."""

import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from ecommerce_product_image_creator.preparation import (
    read_prepare_response,
    read_prepared,
)
from ecommerce_product_image_creator.storage import Store


@dataclass(frozen=True)
class BusinessChoice:
    choice_id: str
    task_id: str
    revision: int
    input_digest: str
    execution_digest: str
    kind: str
    payload: dict[str, Any]
    source: str
    created_at: float
    invalidated: bool = False
    expires_at: float | None = None
    evidence_reference: str | None = None


def _source_is_trusted(choice: BusinessChoice) -> bool:
    """No real Host handoff has been verified; production fails closed."""
    return False


def _table(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS business_choice (
       choice_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
       input_digest TEXT NOT NULL, execution_digest TEXT NOT NULL, kind TEXT NOT NULL,
       payload TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
       invalidated INTEGER NOT NULL DEFAULT 0 CHECK(invalidated IN (0,1)),
       FOREIGN KEY(task_id,revision) REFERENCES revision(task_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS business_choice_detail (
       choice_id TEXT PRIMARY KEY, expires_at REAL, evidence_reference TEXT,
       invalidation_reason TEXT,
       FOREIGN KEY(choice_id) REFERENCES business_choice(choice_id))""")


def _latest(db: sqlite3.Connection, task_id: str, revision: int) -> bool:
    row = db.execute(
        "SELECT MAX(revision) FROM revision WHERE task_id=?", (task_id,)
    ).fetchone()
    return row is not None and row[0] == revision


def record_choice(store: Store, choice: BusinessChoice) -> None:
    saved = read_prepared(store, choice.task_id, choice.revision)
    summary = read_prepare_response(store, choice.task_id, choice.revision)
    if (
        saved["digest_version"] != 2
        or saved["input_digest"] != choice.input_digest
        or saved["execution_digest"] != choice.execution_digest
        or choice.invalidated
        or not choice.choice_id
        or not choice.source
        or not math.isfinite(choice.created_at)
        or (
            choice.expires_at is not None
            and (
                not math.isfinite(choice.expires_at)
                or choice.expires_at <= choice.created_at
            )
        )
    ):
        raise ValueError("Invalid choice scope")
    p = choice.payload
    if choice.kind == "version":
        versions = {
            r["version"]
            for r in summary["packaging_versions"]["references"]
            if r["version"]
        }
        valid_one = set(p) == {"version"} and p["version"] in versions
        valid_alias = (
            set(p) == {"same_version"}
            and isinstance(p["same_version"], list)
            and len(p["same_version"]) == len(versions)
            and set(p["same_version"]) == versions
            and len(versions) > 1
        )
        if not (valid_one or valid_alias):
            raise ValueError("Invalid version choice")
    elif choice.kind == "uncertainty":
        if (
            set(p) != {"conflicts", "assumptions", "mode"}
            or p["mode"] != "preview"
            or p["conflicts"] != summary["structure_conflicts"]["reported"]
            or not isinstance(p["assumptions"], dict)
            or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in p["assumptions"].items()
            )
        ):
            raise ValueError("Invalid uncertainty scope")
    elif choice.kind == "subset":
        if (
            set(p) != {"suggested", "selected"}
            or p["selected"] != saved["stage_references"]
            or not isinstance(p["suggested"], list)
            or not set(p["suggested"]).issubset(set(saved["stage_references"]))
        ):
            raise ValueError("Invalid subset scope")
    else:
        raise ValueError("Unknown business choice")
    payload = json.dumps(p, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        with closing(store._connect()) as db, db:
            _table(db)
            db.execute("BEGIN IMMEDIATE")
            if not _latest(db, choice.task_id, choice.revision):
                raise ValueError("Stale business choice")
            db.execute(
                "INSERT INTO business_choice VALUES (?,?,?,?,?,?,?,?,?,0)",
                (
                    choice.choice_id,
                    choice.task_id,
                    choice.revision,
                    choice.input_digest,
                    choice.execution_digest,
                    choice.kind,
                    payload,
                    choice.source,
                    choice.created_at,
                ),
            )
            db.execute(
                "INSERT INTO business_choice_detail(choice_id,expires_at,evidence_reference) VALUES(?,?,?)",
                (choice.choice_id, choice.expires_at, choice.evidence_reference),
            )
    except sqlite3.IntegrityError:
        raise ValueError("Duplicate or invalid business choice") from None


def read_choice(store: Store, choice_id: str) -> BusinessChoice:
    with closing(store._connect()) as db:
        _table(db)
        row = db.execute(
            "SELECT c.choice_id,c.task_id,c.revision,c.input_digest,c.execution_digest,c.kind,c.payload,c.source,c.created_at,c.invalidated,d.expires_at,d.evidence_reference FROM business_choice c LEFT JOIN business_choice_detail d ON d.choice_id=c.choice_id WHERE c.choice_id=?",
            (choice_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Business choice not found")
    return BusinessChoice(
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        row[5],
        json.loads(row[6]),
        row[7],
        row[8],
        bool(row[9]),
        row[10],
        row[11],
    )


def invalidate_choice(
    store: Store, choice_id: str, reason: str = "invalidated"
) -> None:
    """Persist invalidation without changing or deleting historical evidence."""
    with closing(store._connect()) as db, db:
        _table(db)
        if (
            db.execute(
                "UPDATE business_choice SET invalidated=1 WHERE choice_id=?",
                (choice_id,),
            ).rowcount
            != 1
        ):
            raise ValueError("Business choice not found")
        db.execute(
            "INSERT INTO business_choice_detail(choice_id,invalidation_reason) VALUES(?,?) ON CONFLICT(choice_id) DO UPDATE SET invalidation_reason=excluded.invalidation_reason",
            (choice_id, reason),
        )


def evaluate_choices(
    store: Store,
    task_id: str,
    revision: int,
    *,
    choice_ids: list[str] | None = None,
    suggested: list[str] | None = None,
) -> dict[str, Any]:
    saved = read_prepared(store, task_id, revision)
    summary = read_prepare_response(store, task_id, revision)
    with closing(store._connect()) as db:
        current = _latest(db, task_id, revision)
    import time

    trusted: list[BusinessChoice] = []
    uncertainty_requested = False
    for choice_id in choice_ids or []:
        c = read_choice(store, choice_id)
        if c.kind == "uncertainty" and c.task_id == task_id and c.revision == revision:
            uncertainty_requested = True
        if (
            current
            and not c.invalidated
            and (c.expires_at is None or c.expires_at > time.time())
            and c.task_id == task_id
            and c.revision == revision
            and c.input_digest == saved["input_digest"]
            and c.execution_digest == saved["execution_digest"]
            and _source_is_trusted(c)
        ):
            trusted.append(c)
    pending = []
    chosen = None
    same_version = None
    if not current or saved["digest_version"] != 2:
        pending.append("stale_snapshot")
    versions = summary["packaging_versions"]
    if versions["status"] == "multiple_declared_versions":
        values = [c for c in trusted if c.kind == "version"]
        if len(values) != 1:
            pending.append("packaging_version")
        else:
            chosen = values[0].payload.get("version")
            same_version = values[0].payload.get("same_version")
            if saved["stage"] == "mock" and same_version is None:
                pending.append("mixed_packaging_inputs_require_new_snapshot")
    conflicts = summary["structure_conflicts"]["reported"]
    assumptions: dict[str, str] = {}
    preview = False
    preview_decision_id = None
    accepted = [
        c
        for c in trusted
        if c.kind == "uncertainty" and c.payload["conflicts"] == conflicts
    ]
    if (conflicts or uncertainty_requested) and len(accepted) != 1:
        pending.append("structure_clarification")
    elif len(accepted) == 1:
        assumptions = accepted[0].payload["assumptions"]
        preview_decision_id = accepted[0].choice_id
        preview = True
    if suggested is not None:
        accepted = [
            c
            for c in trusted
            if c.kind == "subset"
            and c.payload["suggested"] == suggested
            and c.payload["selected"] == saved["stage_references"]
        ]
        if len(accepted) != 1:
            pending.append("subset_confirmation")
    return {
        "status": "pending" if pending else "preview" if preview else "ready",
        "pending": pending,
        "selected": saved["stage_references"],
        "chosen_version": chosen,
        "same_version": same_version,
        "unknown_structure": True,
        "structure_verified": False,
        "assumptions": assumptions,
        "preview_decision_id": preview_decision_id,
        "authorization": "unverified",
    }


def _subset_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS subset_proposal (
        proposal_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
        input_digest TEXT NOT NULL, execution_digest TEXT NOT NULL, source TEXT NOT NULL,
        candidates TEXT NOT NULL, selected TEXT NOT NULL, excluded TEXT NOT NULL,
        reason TEXT NOT NULL, created_at REAL NOT NULL,
        FOREIGN KEY(task_id,revision) REFERENCES revision(task_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS subset_decision (
        decision_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,
        decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
        source TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL,
        invalidated INTEGER NOT NULL DEFAULT 0 CHECK(invalidated IN (0,1)),
        invalidation_reason TEXT, target_revision INTEGER,
        trust_state TEXT NOT NULL DEFAULT 'unverified' CHECK(trust_state='unverified'),
        FOREIGN KEY(proposal_id) REFERENCES subset_proposal(proposal_id))""")


def record_subset_proposal(
    store: Store,
    proposal_id: str,
    task_id: str,
    revision: int,
    selected: list[str],
    source: str,
    reason: str,
) -> dict[str, Any]:
    """Record a suggestion only; no input, approval or authorization is changed."""
    import time

    saved = read_prepared(store, task_id, revision)
    candidates = saved["stage_references"]
    if (
        saved["digest_version"] != 2
        or not proposal_id
        or not source.strip()
        or not reason.strip()
        or not isinstance(selected, list)
        or not selected
        or len(set(selected)) != len(selected)
        or selected != [r for r in candidates if r in selected]
    ):
        raise ValueError("Invalid subset proposal")
    if saved["stage"] == "mock" and (
        not saved["form_source"]
        or saved["form_source"]["artifact_id"] not in selected
        or len(selected) < 2
    ):
        raise ValueError("Mock subset must retain form source and artwork")
    excluded = [r for r in candidates if r not in selected]
    try:
        with closing(store._connect()) as db, db:
            _subset_tables(db)
            db.execute("BEGIN IMMEDIATE")
            if not _latest(db, task_id, revision):
                raise ValueError("Stale proposal")
            db.execute(
                "INSERT INTO subset_proposal VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    task_id,
                    revision,
                    saved["input_digest"],
                    saved["execution_digest"],
                    source,
                    json.dumps(candidates),
                    json.dumps(selected),
                    json.dumps(excluded),
                    reason,
                    time.time(),
                ),
            )
    except sqlite3.IntegrityError:
        raise ValueError("Proposal conflict") from None
    return read_subset_proposal(store, proposal_id)


def read_subset_proposal(store: Store, proposal_id: str) -> dict[str, Any]:
    with closing(store._connect()) as db:
        row = db.execute(
            """SELECT p.task_id,p.revision,p.input_digest,p.execution_digest,p.source,
            p.candidates,p.selected,p.excluded,p.reason,p.created_at,d.decision,d.source,d.expires_at,
            d.invalidated,d.invalidation_reason,d.target_revision,d.trust_state,d.decision_id
            FROM subset_proposal p LEFT JOIN subset_decision d ON d.proposal_id=p.proposal_id
            WHERE p.proposal_id=?""",
            (proposal_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Proposal not found")
    return {
        "proposal_id": proposal_id,
        "task_id": row[0],
        "revision": row[1],
        "input_digest": row[2],
        "execution_digest": row[3],
        "source": row[4],
        "candidates": json.loads(row[5]),
        "selected": json.loads(row[6]),
        "excluded": json.loads(row[7]),
        "reason": row[8],
        "created_at": row[9],
        "status": "invalidated" if row[13] else row[10] or "pending",
        "decision_source": row[11],
        "expires_at": row[12],
        "invalidation_reason": row[14],
        "target_revision": row[15],
        "trust_state": row[16] or "unverified",
        "decision_id": row[17],
    }


def record_subset_decision(
    store: Store,
    proposal_id: str,
    decision_id: str,
    decision: str,
    source: str,
    expires_at: float,
) -> None:
    """Persist an unverified declaration, not a verified human decision."""
    import time

    if (
        not decision_id
        or decision not in ("accepted", "rejected")
        or not source.strip()
        or not math.isfinite(expires_at)
        or expires_at <= time.time()
    ):
        raise ValueError("Invalid subset decision")
    try:
        with closing(store._connect()) as db, db:
            _subset_tables(db)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT task_id,revision FROM subset_proposal WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if not row or not _latest(db, row[0], row[1]):
                raise ValueError("Proposal missing or stale")
            db.execute(
                "INSERT INTO subset_decision(decision_id,proposal_id,decision,source,expires_at,created_at) VALUES(?,?,?,?,?,?)",
                (decision_id, proposal_id, decision, source, expires_at, time.time()),
            )
    except sqlite3.IntegrityError:
        raise ValueError("Decision conflict") from None


def invalidate_subset_decision(store: Store, proposal_id: str, reason: str) -> None:
    if not reason.strip():
        raise ValueError("Invalid invalidation reason")
    with closing(store._connect()) as db, db:
        if (
            db.execute(
                "UPDATE subset_decision SET invalidated=1,invalidation_reason=? WHERE proposal_id=? AND invalidated=0",
                (reason, proposal_id),
            ).rowcount
            != 1
        ):
            raise ValueError("Decision not found or invalidated")


def _subset_source_is_trusted(proposal: dict[str, Any]) -> bool:
    """No production Host confirmation verifier is configured."""
    return False


def apply_subset_decision(store: Store, proposal_id: str) -> dict[str, Any]:
    """Create a revision from frozen bytes, once, under the local SQLite lock."""
    import time
    from pathlib import Path

    from ecommerce_product_image_creator.domain import (
        ExecutionSnapshot,
        PromptRevision,
        Reference,
        ReferenceCategory,
        Revision,
        digest_references,
        validate_task_input,
    )
    from ecommerce_product_image_creator.preparation import (
        _input_context,
        _observations,
        check_mock_review_current,
    )

    proposal = read_subset_proposal(store, proposal_id)
    try:
        with closing(store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT d.decision,d.source,d.expires_at,d.invalidated,d.target_revision,
                        p.task_id,p.revision,p.input_digest,p.execution_digest,p.selected
                        FROM subset_decision d JOIN subset_proposal p ON p.proposal_id=d.proposal_id
                        WHERE p.proposal_id=?""",
                (proposal_id,),
            ).fetchone()
            if (
                not row
                or row[0] != "accepted"
                or row[2] <= time.time()
                or row[3]
                or row[4] is not None
                or not _latest(db, row[5], row[6])
                or not _subset_source_is_trusted(proposal)
            ):
                raise ValueError("Subset decision unavailable or unverified")
            task_id, oldrev, input_digest, execution_digest, selected_json = row[5:]
            saved = read_prepared(store, task_id, oldrev)
            if (
                saved["digest_version"] != 2
                or saved["input_digest"] != input_digest
                or saved["execution_digest"] != execution_digest
            ):
                raise ValueError("Subset decision snapshot changed")
            if saved["stage"] == "mock":
                check_mock_review_current(db, task_id, oldrev)
            selected = json.loads(selected_json)
            old_refs = saved["references"]
            keep = [
                r
                for r in old_refs
                if (
                    r["ref_id"] in selected
                    or (saved["stage"] == "form" and r["category"] == "artwork")
                )
            ]
            refs = [
                Reference(
                    r["ref_id"],
                    ReferenceCategory(r["category"]),
                    Path("."),
                    r["mime"],
                    r["byte_count"],
                    r["sha256"],
                    view=r["view"],
                    region=r["region"],
                    version=r["version"],
                    importance=r["importance"],
                    purpose=r.get("purpose"),
                    observations=_observations(r.get("observations")),
                    task_id=task_id,
                )
                for r in keep
            ]
            validate_task_input(refs, saved["instructions"])
            new_digest = digest_references(refs, saved["instructions"])
            old_snapshot = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
            newrev = oldrev + 1
            prompt = PromptRevision.from_text(
                task_id, newrev, newrev, saved["stage"], saved["prompt"]
            )
            snapshot = ExecutionSnapshot.create(
                Revision(task_id, newrev, new_digest),
                prompt,
                saved["stage"],
                saved["model"],
                saved["endpoint"],
                saved["group"],
                references=tuple(
                    (r["ref_id"], r["sha256"]) for r in keep if r["ref_id"] in selected
                ),
                operation_scope=old_snapshot.operation_scope,
            )
            current = db.execute(
                "SELECT status,input_digest FROM task WHERE task_id=?", (task_id,)
            ).fetchone()
            if (
                not current
                or current[0] not in ("draft", "awaiting_confirmation", "ready")
                or current[1] != input_digest
            ):
                raise ValueError("Task cannot be revised")
            db.execute(
                "INSERT INTO revision VALUES(?,?,?)", (task_id, newrev, new_digest)
            )
            db.execute(
                "INSERT INTO prepared_execution VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    newrev,
                    saved["stage"],
                    saved["prompt"],
                    prompt.prompt_digest,
                    snapshot.execution_digest,
                    saved["model"],
                    saved["endpoint"],
                    saved["group"],
                ),
            )
            db.execute(
                "INSERT INTO prepared_snapshot_v2 VALUES(?,?,?)",
                (task_id, newrev, snapshot.to_payload()),
            )
            db.execute(
                "INSERT INTO prepared_input_v2 VALUES(?,?,?)",
                (task_id, newrev, _input_context(refs, saved["instructions"])),
            )
            for table, column in (
                ("prepared_scope_v2", "scope"),
                ("prepared_conflicts", "reported"),
            ):
                db.execute(
                    f"INSERT INTO {table} SELECT task_id,?,{column} FROM {table} WHERE task_id=? AND revision=?",
                    (newrev, task_id, oldrev),
                )
            by_id = dict(
                zip(
                    (r["ref_id"] for r in old_refs),
                    saved["reference_bytes"],
                    strict=True,
                )
            )
            for i, r in enumerate(keep):
                db.execute(
                    "INSERT INTO prepared_reference VALUES(?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        newrev,
                        i,
                        r["ref_id"],
                        r["category"],
                        r["mime"],
                        r["sha256"],
                        by_id[r["ref_id"]],
                    ),
                )
            if saved["stage"] == "mock":
                db.execute(
                    "INSERT INTO prepared_mock_source SELECT task_id,?,form_revision,artifact_id,review_id,artifact_sha256 FROM prepared_mock_source WHERE task_id=? AND revision=?",
                    (newrev, task_id, oldrev),
                )
            db.execute(
                "UPDATE task SET input_digest=?,status='awaiting_confirmation' WHERE task_id=?",
                (new_digest, task_id),
            )
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_eligibility'"
            ).fetchone():
                db.execute(
                    "UPDATE local_eligibility SET invalidated=1 WHERE task_id=? AND consumed=0",
                    (task_id,),
                )
            if (
                db.execute(
                    "UPDATE subset_decision SET target_revision=? WHERE proposal_id=? AND target_revision IS NULL AND invalidated=0",
                    (newrev, proposal_id),
                ).rowcount
                != 1
            ):
                raise ValueError("Subset decision already consumed")
        return {
            "task_id": task_id,
            "revision": newrev,
            "execution_digest": snapshot.execution_digest,
            "authorization": "unverified",
        }
    except (sqlite3.Error, KeyError, TypeError, OSError, MemoryError):
        raise ValueError("Cannot safely apply subset") from None


def subset_gate_locked(
    db: sqlite3.Connection, store: Store, task_id: str, revision: int
) -> bool:
    """Recheck applicable subset records while caller holds BEGIN IMMEDIATE."""
    import time

    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subset_proposal'"
    ).fetchone():
        return True
    rows = db.execute(
        """SELECT p.proposal_id,p.revision,p.input_digest,p.execution_digest,
            p.candidates,p.selected,d.decision,d.expires_at,d.invalidated,d.target_revision
            FROM subset_proposal p LEFT JOIN subset_decision d ON d.proposal_id=p.proposal_id
            WHERE p.task_id=? AND (p.revision=? OR d.target_revision=?)""",
        (task_id, revision, revision),
    ).fetchall()
    for row in rows:
        proposal = read_subset_proposal(store, row[0])
        source = read_prepared(store, task_id, row[1])
        if (source["input_digest"], source["execution_digest"]) != (row[2], row[3]):
            return False
        if (
            row[6] not in ("accepted", "rejected")
            or row[7] is None
            or row[7] <= time.time()
            or row[8]
            or not _subset_source_is_trusted(proposal)
        ):
            return False
        if row[1] == revision and row[6] == "accepted" and row[4] != row[5]:
            return False
        if row[9] == revision and (row[6] != "accepted" or row[1] == revision):
            return False
        if row[9] == revision:
            target = read_prepared(store, task_id, revision)
            if target["stage_references"] != json.loads(row[5]):
                return False
    return True
