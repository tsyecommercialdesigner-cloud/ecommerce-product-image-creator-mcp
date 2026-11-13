"""Team gateway provenance and locally computed integrity evidence."""

import hashlib
import os
import re
import sqlite3
from contextlib import closing

from . import provider, team_query_policy
from .storage import Store


def allowed_host(host: str) -> bool:
    hosts = os.environ.get("TEAM_DOWNLOAD_HOSTS", "").split(",")
    return bool(host and host in {h.strip().lower() for h in hosts if h.strip()})


def selected(identity: object, observation: int) -> tuple[str, str] | None:
    from .download_safety import _target
    from .original_query import OriginalIdentity

    captured = team_query_policy.response()
    if not isinstance(identity, OriginalIdentity) or not isinstance(captured, tuple):
        return None
    if captured[:2] != (identity, observation):
        return None
    result = provider.parse_query(200, captured[2], identity.upstream_id)
    if result.state != "succeeded" or not result.result_available:
        return None
    url = result.image_urls[0]
    host, _ = _target(url)
    if not allowed_host(host):
        return None
    # MIME is verified against the actual HTTP response and decoded image.
    return url, host


def _current_store() -> Store | None:
    value = team_query_policy.store()
    return value if isinstance(value, Store) else None


def record(locator: object, url: str, digest: str, mime: str) -> None:
    from .artifact_locator import LocatorEvidence
    from .download_safety import _target

    store = _current_store()
    if store is None or not isinstance(locator, LocatorEvidence):
        raise ValueError("team_evidence_unavailable")
    host, _ = _target(url)
    if not allowed_host(host) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("team_evidence_invalid")
    with closing(store._connect()) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS team_artifact_evidence ("
            "locator_id TEXT PRIMARY KEY, observation_id INTEGER NOT NULL, "
            "url_sha256 TEXT NOT NULL, host TEXT NOT NULL, local_sha256 TEXT NOT NULL, "
            "mime TEXT NOT NULL, supplier_sha256 TEXT)"
        )
        values = (
            locator.locator_id,
            locator.observation_id,
            team_query_policy.url_hash(url),
            host,
            digest,
            mime,
            None,
        )
        existing = db.execute(
            "SELECT * FROM team_artifact_evidence WHERE locator_id=?",
            (locator.locator_id,),
        ).fetchone()
        if existing is not None and existing != values:
            raise ValueError("team_evidence_conflict")
        if existing is None:
            db.execute(
                "INSERT INTO team_artifact_evidence VALUES (?,?,?,?,?,?,?)", values
            )


def recorded(
    locator: object,
    digest: str | None = None,
    mime: str | None = None,
    url: str | None = None,
) -> bool:
    from .artifact_locator import LocatorEvidence

    store = _current_store()
    if (
        store is None
        or not isinstance(locator, LocatorEvidence)
        or locator.source != "team-gateway"
    ):
        return False
    try:
        with closing(store._connect()) as db:
            row = db.execute(
                "SELECT observation_id,url_sha256,host,local_sha256,mime,supplier_sha256 "
                "FROM team_artifact_evidence WHERE locator_id=?",
                (locator.locator_id,),
            ).fetchone()
    except sqlite3.Error:
        return False
    return bool(
        row
        and row[0] == locator.observation_id
        and allowed_host(row[2])
        and row[5] is None
        and (digest is None or digest == row[3])
        and (mime is None or mime == row[4])
        and (url is None or team_query_policy.url_hash(url) == row[1])
    )


def content(locator: object, data: bytes) -> bool:
    return recorded(locator, hashlib.sha256(data).hexdigest())
