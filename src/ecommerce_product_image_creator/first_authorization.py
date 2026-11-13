"""Independent, fail-closed authorization evidence for MCP first submissions."""

import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass

from .domain import ExecutionSnapshot


@dataclass(frozen=True)
class FirstAuthorization:
    authorization_id: str
    kind: str
    operation_id: str
    task_id: str
    revision: int
    stage: str
    execution_digest: str
    model: str
    endpoint: str
    group: str
    scope_digest: str
    expires_at: float
    source: str
    source_reference: str


def _verify_source(db: sqlite3.Connection, evidence: FirstAuthorization) -> bool:
    """A real Host and fee source have not been verified."""
    return False


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS first_authorization (authorization_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(operation_id,kind))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS first_authorization_revocation (authorization_id TEXT PRIMARY KEY, FOREIGN KEY(authorization_id) REFERENCES first_authorization(authorization_id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS first_authorization_consumption (authorization_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, attempt_id TEXT NOT NULL, FOREIGN KEY(authorization_id) REFERENCES first_authorization(authorization_id))"
    )
    for table in (
        "first_authorization",
        "first_authorization_revocation",
        "first_authorization_consumption",
    ):
        for action in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable authorization evidence'); END"
            )


def record(db: sqlite3.Connection, evidence: FirstAuthorization) -> None:
    if (
        evidence.kind not in {"host", "fee"}
        or not all(
            (
                evidence.authorization_id,
                evidence.operation_id,
                evidence.task_id,
                evidence.execution_digest,
                evidence.source,
                evidence.source_reference,
            )
        )
        or not math.isfinite(evidence.expires_at)
        or evidence.expires_at <= time.time()
        or not _verify_source(db, evidence)
    ):
        raise ValueError("Authorization source is unverified")
    initialize(db)
    payload = json.dumps(asdict(evidence), sort_keys=True, separators=(",", ":"))
    db.execute(
        "INSERT INTO first_authorization VALUES (?,?,?,?)",
        (evidence.authorization_id, evidence.operation_id, evidence.kind, payload),
    )


def check(
    db: sqlite3.Connection,
    operation_id: str,
    snapshot: ExecutionSnapshot,
    *,
    attempt_id: str | None = None,
) -> tuple[str, str]:
    try:
        rows = db.execute(
            "SELECT authorization_id,kind,payload FROM first_authorization WHERE operation_id=?",
            (operation_id,),
        ).fetchall()
        if len(rows) != 2 or {row[1] for row in rows} != {"host", "fee"}:
            raise ValueError("Independent authorization missing")
        ids = []
        for auth_id, kind, payload in rows:
            evidence = FirstAuthorization(**json.loads(payload))
            if (
                (
                    evidence.authorization_id,
                    evidence.kind,
                    evidence.operation_id,
                    evidence.task_id,
                    evidence.revision,
                    evidence.stage,
                    evidence.execution_digest,
                    evidence.model,
                    evidence.endpoint,
                    evidence.group,
                    evidence.scope_digest,
                )
                != (
                    auth_id,
                    kind,
                    operation_id,
                    snapshot.task_id,
                    snapshot.revision,
                    snapshot.stage,
                    snapshot.execution_digest,
                    snapshot.model,
                    snapshot.endpoint,
                    snapshot.group,
                    snapshot.operation_scope,
                )
                or not math.isfinite(evidence.expires_at)
                or evidence.expires_at <= time.time()
                or not _verify_source(db, evidence)
            ):
                raise ValueError("Authorization scope or source is unverified")
            if db.execute(
                "SELECT 1 FROM first_authorization_revocation WHERE authorization_id=?",
                (auth_id,),
            ).fetchone():
                raise ValueError("Authorization revoked")
            consumed = db.execute(
                "SELECT operation_id,attempt_id FROM first_authorization_consumption WHERE authorization_id=?",
                (auth_id,),
            ).fetchone()
            if (attempt_id is None and consumed is not None) or (
                attempt_id is not None and consumed != (operation_id, attempt_id)
            ):
                raise ValueError("Authorization already consumed or missing")
            ids.append(auth_id)
        return ids[0], ids[1]
    except (sqlite3.Error, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ValueError("Independent authorization unavailable") from error


def consume(
    db: sqlite3.Connection, operation_id: str, attempt_id: str, ids: tuple[str, str]
) -> None:
    for authorization_id in ids:
        db.execute(
            "INSERT INTO first_authorization_consumption VALUES (?,?,?)",
            (authorization_id, operation_id, attempt_id),
        )
