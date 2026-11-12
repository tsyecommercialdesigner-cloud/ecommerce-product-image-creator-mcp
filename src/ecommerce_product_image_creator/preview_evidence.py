"""Historical preview evidence; it does not establish human or billing authorization."""

import json
import sqlite3
import time
from typing import Any


def persist_preview(
    db: sqlite3.Connection, operation_id: str, attempt_id: str, evidence: dict[str, Any]
) -> None:
    if (
        evidence.get("operation_id", operation_id) != operation_id
        or evidence.get("attempt_id", attempt_id) != attempt_id
    ):
        raise ValueError("Preview operation association mismatch")
    evidence = {**evidence, "operation_id": operation_id, "attempt_id": attempt_id}
    db.execute("""CREATE TABLE IF NOT EXISTS attempt_preview (
      attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, evidence TEXT NOT NULL,
      FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
      FOREIGN KEY(operation_id) REFERENCES operation(operation_id))""")
    db.execute(
        "INSERT INTO attempt_preview VALUES(?,?,?)",
        (
            attempt_id,
            operation_id,
            json.dumps(
                evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        ),
    )


def read_preview(db: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempt_preview'"
    ).fetchone():
        return {
            "status": "unknown",
            "structure_verified": False,
            "current_decision": "unknown",
        }
    row = db.execute(
        "SELECT operation_id,evidence FROM attempt_preview WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if not row:
        return {
            "status": "unknown",
            "structure_verified": False,
            "current_decision": "unknown",
        }
    value: dict[str, Any] = json.loads(row[1])
    if (
        value.get("status") != "preview"
        or value.get("structure_verified") is not False
        or not isinstance(value.get("decision_id"), str)
        or value.get("attempt_id") != attempt_id
        or value.get("operation_id") != row[0]
    ):
        raise ValueError("Invalid preview evidence")
    decision = db.execute(
        """SELECT c.invalidated,d.expires_at,c.task_id,c.revision,c.execution_digest,c.payload,c.source FROM business_choice c
       LEFT JOIN business_choice_detail d ON d.choice_id=c.choice_id WHERE c.choice_id=?""",
        (value["decision_id"],),
    ).fetchone()
    latest = db.execute(
        "SELECT MAX(revision) FROM revision WHERE task_id=?", (value.get("task_id"),)
    ).fetchone()
    mismatch = False
    if decision:
        payload = json.loads(decision[5])
        mismatch = (
            (decision[2], decision[3], decision[4], decision[6])
            != (
                value.get("task_id"),
                value.get("revision"),
                value.get("execution_digest"),
                value.get("source"),
            )
            or payload.get("conflicts") != value.get("conflicts")
            or payload.get("assumptions") != value.get("assumptions")
            or payload.get("mode") != "preview"
        )
    value["current_decision"] = (
        "unavailable"
        if decision is None
        else "mismatch"
        if mismatch
        else "invalidated"
        if decision[0]
        else "expired"
        if decision[1] is not None and decision[1] <= time.time()
        else "superseded"
        if not latest or latest[0] != value.get("revision")
        else "not_invalidated"
    )
    return value
