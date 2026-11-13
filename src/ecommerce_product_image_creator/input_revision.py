"""Scope-bound input revision declarations; no declaration is a Host proof."""

import json
import math
import sqlite3
import time
from contextlib import closing
from typing import Any

from ecommerce_product_image_creator.config import team_mode_enabled
from ecommerce_product_image_creator.preparation import PreparationError, read_prepared
from ecommerce_product_image_creator.storage import Store


def _schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS input_revision_decision (
      decision_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
      source_input_digest TEXT NOT NULL, source_execution_digest TEXT NOT NULL,
      source_refs TEXT NOT NULL, target_refs TEXT NOT NULL, reason TEXT NOT NULL,
      source TEXT NOT NULL, evidence_reference TEXT, expires_at REAL NOT NULL,
      created_at REAL NOT NULL, trust_state TEXT NOT NULL DEFAULT 'unverified',
      invalidated INTEGER NOT NULL DEFAULT 0, invalidation_reason TEXT,
      target_revision INTEGER, target_execution_digest TEXT,
      FOREIGN KEY(task_id,source_revision) REFERENCES revision(task_id,revision))""")


def _source_is_trusted(record: dict[str, Any]) -> bool:
    """Allow explicit local Team input changes; Strict mode still fails closed."""
    return (
        team_mode_enabled()
        and record.get("source") == "team-local-input-change"
        and record.get("evidence_reference") == "team-explicit-preparation"
    )


def _ref_values(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return refs


def record_input_revision(
    store: Store,
    decision_id: str,
    task_id: str,
    source_revision: int,
    target_refs: list[dict[str, Any]],
    reason: str,
    source: str,
    expires_at: float,
    evidence_reference: str | None = None,
) -> None:
    saved = read_prepared(store, task_id, source_revision)
    if (
        saved["digest_version"] != 2
        or not decision_id
        or not reason.strip()
        or not source.strip()
        or not math.isfinite(expires_at)
        or expires_at <= time.time()
        or not target_refs
        or any(
            not isinstance(r, dict)
            or not all(
                isinstance(r.get(k), str) and r[k]
                for k in ("ref_id", "category", "sha256")
            )
            for r in target_refs
        )
        or len({r["ref_id"] for r in target_refs}) != len(target_refs)
    ):
        raise ValueError("Invalid input revision declaration")
    try:
        with closing(store._connect()) as db, db:
            _schema(db)
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                "SELECT MAX(revision) FROM revision WHERE task_id=?", (task_id,)
            ).fetchone()
            if not latest or latest[0] != source_revision:
                raise ValueError("Stale input revision declaration")
            db.execute(
                """INSERT INTO input_revision_decision(decision_id,task_id,source_revision,source_input_digest,source_execution_digest,source_refs,target_refs,reason,source,evidence_reference,expires_at,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    task_id,
                    source_revision,
                    saved["input_digest"],
                    saved["execution_digest"],
                    json.dumps(_ref_values(saved["references"])),
                    json.dumps(target_refs),
                    reason,
                    source,
                    evidence_reference,
                    expires_at,
                    time.time(),
                ),
            )
    except sqlite3.IntegrityError:
        raise ValueError("Input revision decision conflict") from None


def read_input_revision(store: Store, decision_id: str) -> dict[str, Any]:
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT * FROM input_revision_decision WHERE decision_id=?", (decision_id,)
        ).fetchone()
    if not row:
        raise ValueError("Input revision decision unavailable")
    names = (
        "decision_id",
        "task_id",
        "source_revision",
        "source_input_digest",
        "source_execution_digest",
        "source_refs",
        "target_refs",
        "reason",
        "source",
        "evidence_reference",
        "expires_at",
        "created_at",
        "trust_state",
        "invalidated",
        "invalidation_reason",
        "target_revision",
        "target_execution_digest",
    )
    result: dict[str, Any] = dict(zip(names, row, strict=True))
    result["source_refs"] = json.loads(result["source_refs"])
    result["target_refs"] = json.loads(result["target_refs"])
    return result


def invalidate_input_revision(store: Store, decision_id: str, reason: str) -> None:
    if not reason.strip():
        raise ValueError("Invalid reason")
    with closing(store._connect()) as db, db:
        if (
            db.execute(
                "UPDATE input_revision_decision SET invalidated=1,invalidation_reason=? WHERE decision_id=? AND invalidated=0",
                (reason, decision_id),
            ).rowcount
            != 1
        ):
            raise ValueError("Decision missing or invalidated")


def check_input_revision_locked(
    db: sqlite3.Connection,
    store: Store,
    decision_id: str | None,
    task_id: str,
    revision: int,
    target_refs: list[dict[str, Any]],
) -> None:
    if not decision_id:
        raise PreparationError("Subset or input revision confirmation is required")
    record = read_input_revision(store, decision_id)
    row = db.execute(
        "SELECT invalidated,expires_at,target_revision FROM input_revision_decision WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    saved = read_prepared(store, task_id, revision)
    if (
        not row
        or row[0]
        or row[1] <= time.time()
        or row[2] is not None
        or record["task_id"] != task_id
        or record["source_revision"] != revision
        or record["source_input_digest"] != saved["input_digest"]
        or record["source_execution_digest"] != saved["execution_digest"]
        or record["source_refs"] != _ref_values(saved["references"])
        or record["target_refs"] != target_refs
        or not _source_is_trusted(record)
    ):
        raise PreparationError("Input revision confirmation is unverified or stale")


def input_revision_gate_locked(
    db: sqlite3.Connection,
    store: Store,
    task_id: str,
    revision: int,
    execution_digest: str,
) -> bool:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='input_revision_decision'"
    ).fetchone()
    rows = (
        db.execute(
            "SELECT decision_id,invalidated,expires_at,target_execution_digest FROM input_revision_decision WHERE task_id=? AND target_revision=?",
            (task_id, revision),
        ).fetchall()
        if exists
        else []
    )
    if not rows:
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subset_decision'"
            ).fetchone()
            and db.execute(
                "SELECT 1 FROM subset_decision d JOIN subset_proposal p ON p.proposal_id=d.proposal_id WHERE p.task_id=? AND d.target_revision=?",
                (task_id, revision),
            ).fetchone()
        ):
            return True  # The separate subset gate verifies the linked decision.
        current = db.execute(
            "SELECT stage FROM prepared_execution WHERE task_id=? AND revision=?",
            (task_id, revision),
        ).fetchone()
        if not current:
            return False
        if current[0] == "form":
            prior = db.execute(
                "SELECT MAX(revision) FROM prepared_execution WHERE task_id=? AND revision<? AND stage='form'",
                (task_id, revision),
            ).fetchone()
            categories = {"form", "artwork"}
        else:
            prior = db.execute(
                "SELECT MAX(revision) FROM prepared_execution WHERE task_id=? AND revision<?",
                (task_id, revision),
            ).fetchone()
            categories = {"artwork"}
        if not prior or prior[0] is None:
            return current[0] == "form"
        contexts = db.execute(
            "SELECT revision,context FROM prepared_input_v2 WHERE task_id=? AND revision IN (?,?)",
            (task_id, prior[0], revision),
        ).fetchall()
        if len(contexts) != 2:
            return False
        by_revision = {
            r: [x for x in json.loads(c)["references"] if x["category"] in categories]
            for r, c in contexts
        }
        return by_revision[prior[0]] == by_revision[revision]
    return all(
        not row[1]
        and row[2] > time.time()
        and row[3] == execution_digest
        and _source_is_trusted(read_input_revision(store, row[0]))
        for row in rows
    )
