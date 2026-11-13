"""Inert original-artifact locator evidence; never downloads or authorizes."""

import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import astuple, dataclass, field
from pathlib import Path

from . import original_query, preparation, recovery
from .artifact_reader import ArtifactReadError, read_artifact
from .storage import FileSafetyError, ResourceBudget, Store, validate_image_bytes


@dataclass(frozen=True)
class LocatorEvidence:
    locator_id: str
    root: str
    candidate_id: str
    task_id: str
    revision: int
    stage: str
    attempt_id: str
    digest: str
    provider: str
    model: str
    endpoint: str
    group: str
    scope: str
    upstream_id: str
    observation_id: int | None
    artifact_identity: str
    locator_type: str
    source: str
    handle: str
    created: float
    expires: float


@dataclass(frozen=True)
class Assessment:
    state: str
    reason: str
    locator_id: str | None = None
    observation_id: int | None = None
    artifact_id: str | None = None
    source_mock_revision: int | None = None
    source_review_id: str | None = None
    artifact_sha256: str | None = None
    source_sha256: str | None = None
    task_id: str | None = None
    revision: int | None = None
    stage: str | None = None
    attempt_id: str | None = None


def _mock_copy(
    db: sqlite3.Connection,
    store: Store,
    task_id: str,
    revision: int,
    attempt_id: str,
    artifact_id: str,
    observation_id: int,
    budget: ResourceBudget,
) -> Assessment | None:
    """Inspect a source-linked frozen Mock input without releasing its bytes."""
    artifact = db.execute(
        "SELECT x.sha256,a.task_id,a.revision,a.upstream_status,a.delivery_status,p.stage FROM artifact x JOIN attempt a ON a.attempt_id=x.attempt_id JOIN prepared_execution p ON p.task_id=a.task_id AND p.revision=a.revision WHERE x.artifact_id=? AND a.attempt_id=?",
        (artifact_id, attempt_id),
    ).fetchone()
    historical_delivery = artifact is not None and artifact[4] == "delivered"
    if (
        artifact is not None
        and artifact[4] == "missing_or_corrupt"
        and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_integrity_observation'"
        ).fetchone()
    ):
        historical_delivery = (
            db.execute(
                "SELECT 1 FROM artifact_integrity_observation WHERE artifact_id=? AND attempt_id=? AND expected_sha256=? AND previous_status='delivered' LIMIT 1",
                (artifact_id, attempt_id, artifact[0]),
            ).fetchone()
            is not None
        )
    if (
        artifact is None
        or (artifact[1], artifact[2], artifact[3], artifact[5])
        != (task_id, revision, "succeeded", "form")
        or not historical_delivery
    ):
        return Assessment(
            "artifact_conflict",
            "form_artifact_identity_mismatch",
            observation_id=observation_id,
        )
    sources = db.execute(
        "SELECT revision,artifact_id,review_id,artifact_sha256 FROM prepared_mock_source WHERE task_id=? AND form_revision=? ORDER BY revision",
        (task_id, revision),
    ).fetchall()
    if not sources:
        return None
    if (
        len(sources) != 1
        or sources[0][1] != artifact_id
        or sources[0][3] != artifact[0]
    ):
        return Assessment(
            "artifact_conflict",
            "mock_source_ambiguous_or_mismatch",
            observation_id=observation_id,
        )
    mock_revision, _, review_id, _ = sources[0]
    review = db.execute(
        "SELECT r.task_id,r.form_revision,r.artifact_id,r.artifact_sha256,r.source,d.decision,d.evidence_reference,d.invalidation_reason,d.expires_at,EXISTS(SELECT 1 FROM form_review_invalidation i WHERE i.review_id=r.review_id) FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id WHERE r.review_id=?",
        (review_id,),
    ).fetchone()
    if (
        review is None
        or review[:4] != (task_id, revision, artifact_id, artifact[0])
        or review[5] != "accept"
        or review[7] is not None
        or (review[8] is not None and review[8] <= time.time())
        or review[9]
        or not preparation._review_is_trusted(
            review[4], task_id, review_id, artifact[0], review[6]
        )
    ):
        return Assessment(
            "locator_unverified",
            "historical_review_unverified",
            observation_id=observation_id,
        )
    try:
        preparation._reject_review_conflict(
            db, task_id, revision, artifact_id, artifact[0]
        )
    except preparation.PreparationError:
        return Assessment(
            "artifact_conflict",
            "historical_review_conflict",
            observation_id=observation_id,
        )
    sizes = db.execute(
        "SELECT length(data) FROM prepared_reference WHERE task_id=? AND revision=?",
        (task_id, mock_revision),
    ).fetchall()
    if not sizes or any(
        not isinstance(row[0], int) or row[0] > budget.max_file_bytes for row in sizes
    ):
        return Assessment(
            "locator_unverified",
            "mock_snapshot_budget_unverified",
            observation_id=observation_id,
        )
    try:
        saved = preparation.read_prepared(store, task_id, mock_revision)
    except ValueError:
        return Assessment(
            "locator_unverified",
            "mock_snapshot_unverified",
            observation_id=observation_id,
        )
    if (
        saved["stage"] != "mock"
        or saved["digest_version"] != 2
        or saved["form_source"]
        != {
            "form_revision": revision,
            "artifact_id": artifact_id,
            "review_id": review_id,
            "sha256": artifact[0],
        }
    ):
        return Assessment(
            "artifact_conflict",
            "mock_snapshot_source_mismatch",
            observation_id=observation_id,
        )
    ref = db.execute(
        "SELECT ref_id,category,mime,sha256,data FROM prepared_reference WHERE task_id=? AND revision=? AND position=0",
        (task_id, mock_revision),
    ).fetchone()
    if (
        ref is None
        or ref[0] != artifact_id
        or ref[1] != "form"
        or ref[3] != artifact[0]
        or not isinstance(ref[4], bytes)
        or hashlib.sha256(ref[4]).hexdigest() != artifact[0]
        or len(ref[4]) > budget.max_file_bytes
    ):
        return Assessment(
            "artifact_conflict",
            "mock_copy_identity_or_digest_mismatch",
            observation_id=observation_id,
        )
    try:
        validate_image_bytes(ref[4], ref[2], budget)
    except FileSafetyError:
        return Assessment(
            "artifact_conflict",
            "mock_copy_invalid_image",
            observation_id=observation_id,
        )
    return Assessment(
        "recover_from_local_bytes",
        "verified_mock_form_input",
        observation_id=observation_id,
        artifact_id=artifact_id,
        source_mock_revision=mock_revision,
        source_review_id=review_id,
        artifact_sha256=artifact[0],
        source_sha256=ref[3],
        task_id=task_id,
        revision=revision,
        stage="form",
        attempt_id=attempt_id,
    )


def read_locator(store: Store, locator_id: str) -> LocatorEvidence | None:
    """Read audit metadata only; presence does not establish trust or permission."""
    with closing(store._connect()) as db:
        row = db.execute(
            "SELECT * FROM artifact_locator_evidence WHERE locator_id=?", (locator_id,)
        ).fetchone()
    return LocatorEvidence(*row) if row else None


def _verify_locator_source(
    identity: original_query.OriginalIdentity, evidence: LocatorEvidence
) -> bool:
    """Verify Team provenance in its explicit refresh, keeping Strict closed."""
    from .team_artifact_evidence import recorded, selected
    from .team_query_policy import url_hash

    if evidence.source != "team-gateway" or evidence.observation_id is None:
        return False
    selected_url = selected(identity, evidence.observation_id)
    return bool(
        (selected_url and evidence.handle == url_hash(selected_url[0]))
        or recorded(evidence)
    )


def _checked(db: sqlite3.Connection, store: Store, e: LocatorEvidence) -> bool:
    identity = original_query._identity(
        store, e.root, e.candidate_id, e.task_id, e.revision, e.attempt_id, e.digest
    )
    if identity is None or (
        identity.stage,
        identity.provider,
        identity.model,
        identity.endpoint,
        identity.group,
        identity.upstream_id,
    ) != (e.stage, e.provider, e.model, e.endpoint, e.group, e.upstream_id):
        return False
    if not _verify_locator_source(identity, e) or not original_query._chain_is_valid(
        db, identity
    ):
        return False
    rows = db.execute(
        "SELECT observation_id,root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,provider,model,endpoint,grouping,upstream_id,source,response_digest,remote_state,reason,previous_id FROM recovery_observation WHERE attempt_id=? ORDER BY observation_id",
        (e.attempt_id,),
    ).fetchall()
    found = False
    for row in rows:
        if not recovery._verify_observation_source(identity, row) or row[15] in {
            "conflict",
            "failed",
        }:
            return False
        if row[0] == e.observation_id:
            found = (
                row[15] == "succeeded"
                and isinstance(row[14], str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", row[14]))
            )
    return found


def register(store: Store, evidence: LocatorEvidence) -> str:
    """Register a source-verified, non-secret opaque handle, never a URL."""
    e = evidence
    if (
        not all(
            re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value)
            for value in (
                e.locator_id,
                e.scope,
                e.artifact_identity,
                e.source,
                e.handle,
            )
        )
        or e.locator_type != "fixture_handle"
        or e.observation_id is None
        or not all(math.isfinite(v) for v in (e.created, e.expires))
        or not e.created < e.expires
        or e.created > time.time()
        or e.expires <= time.time()
    ):
        raise ValueError("Invalid or expired locator evidence")
    try:
        with closing(store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not _checked(db, store, e):
                raise ValueError("Locator identity or source unverified")
            row = db.execute(
                "SELECT * FROM artifact_locator_evidence WHERE locator_id=?",
                (e.locator_id,),
            ).fetchone()
            if row is not None:
                if row != astuple(e):
                    raise ValueError("Locator identifier conflicts")
                return e.locator_id
            db.execute(
                "INSERT INTO artifact_locator_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                astuple(e),
            )
            return e.locator_id
    except (sqlite3.Error, OSError, TypeError) as exc:
        raise ValueError("Unable to register locator evidence") from exc


def assess(store: Store, evidence: LocatorEvidence) -> Assessment:
    """Inspect only persisted evidence; no file, provider, authorization or attempt writes."""
    try:
        with closing(store._connect()) as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT * FROM artifact_locator_evidence WHERE locator_id=?",
                (evidence.locator_id,),
            ).fetchone()
            if row != astuple(evidence):
                return Assessment("locator_unverified", "evidence_missing_or_mismatch")
            if evidence.created > time.time():
                return Assessment(
                    "locator_unverified",
                    "locator_time_unverified",
                    evidence.locator_id,
                    evidence.observation_id,
                )
            if evidence.expires <= time.time():
                return Assessment(
                    "locator_unverified",
                    "locator_expired",
                    evidence.locator_id,
                    evidence.observation_id,
                )
            if not _checked(db, store, evidence):
                return Assessment(
                    "locator_unverified",
                    "source_or_observation_unverified",
                    evidence.locator_id,
                    evidence.observation_id,
                )
            others = db.execute(
                "SELECT DISTINCT artifact_identity,locator_type,scope,handle FROM artifact_locator_evidence WHERE attempt_id=?",
                (evidence.attempt_id,),
            ).fetchall()
            if len(others) != 1:
                return Assessment(
                    "artifact_conflict",
                    "locator_conflict",
                    evidence.locator_id,
                    evidence.observation_id,
                )
            return Assessment(
                "remote_retrieval_candidate",
                "locator_verified_for_offline_assessment",
                evidence.locator_id,
                evidence.observation_id,
            )
    except (ValueError, sqlite3.Error, OSError, TypeError):
        return Assessment("locator_unverified", "identity_or_storage_unverified")


def assess_attempt(
    store: Store,
    root: str,
    candidate_id: str,
    task_id: str,
    revision: int,
    stage: str,
    attempt_id: str,
    digest: str,
    observation_id: int,
    *,
    artifact_id: str | None = None,
    artifact_root: Path | None = None,
    budget: ResourceBudget | None = None,
) -> Assessment:
    """Preflight a persisted observation; never creates a locator or reads input blobs."""
    try:
        if (artifact_id is None, artifact_root is None, budget is None) not in (
            (True, True, True),
            (False, False, False),
        ):
            return Assessment("artifact_conflict", "local_check_parameters_incomplete")
        identity = original_query._identity(
            store, root, candidate_id, task_id, revision, attempt_id, digest
        )
        if identity is None or identity.stage != stage:
            return Assessment("upstream_not_confirmed", "identity_or_source_unverified")
        with closing(store._connect()) as db:
            db.execute("BEGIN")
            if not original_query._chain_is_valid(db, identity):
                return Assessment("artifact_conflict", "observation_chain_invalid")
            row = db.execute(
                "SELECT observation_id,root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,provider,model,endpoint,grouping,upstream_id,source,response_digest,remote_state,reason,previous_id FROM recovery_observation WHERE observation_id=? AND attempt_id=?",
                (observation_id, attempt_id),
            ).fetchone()
            if (
                row is None
                or not recovery._verify_observation_source(identity, row)
                or row[15] != "succeeded"
            ):
                return Assessment(
                    "upstream_not_confirmed",
                    "success_observation_unverified",
                    observation_id=observation_id,
                )
            if db.execute(
                "SELECT 1 FROM recovery_observation WHERE attempt_id=? AND remote_state IN ('conflict','failed')",
                (attempt_id,),
            ).fetchone():
                return Assessment(
                    "artifact_conflict",
                    "observation_state_conflict",
                    observation_id=observation_id,
                )
            variants = db.execute(
                "SELECT DISTINCT artifact_identity,locator_type,scope,handle FROM artifact_locator_evidence WHERE attempt_id=?",
                (attempt_id,),
            ).fetchall()
            if len(variants) > 1:
                return Assessment(
                    "artifact_conflict",
                    "locator_conflict",
                    observation_id=observation_id,
                )
            if (
                artifact_id is not None
                and artifact_root is not None
                and budget is not None
            ):
                owner = db.execute(
                    "SELECT attempt_id FROM artifact WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()
                if owner is not None and owner != (attempt_id,):
                    return Assessment(
                        "artifact_conflict",
                        "artifact_owner_mismatch",
                        observation_id=observation_id,
                    )
                if owner is not None and stage in ("form", "mock"):
                    try:
                        local = read_artifact(
                            store,
                            task_id,
                            stage,
                            revision,
                            attempt_id,
                            artifact_id,
                            artifact_root,
                            budget,
                        )
                    except ArtifactReadError:
                        return Assessment(
                            "artifact_conflict",
                            "artifact_identity_unverified",
                            observation_id=observation_id,
                        )
                    if (
                        local["file"]["state"] == "verified"
                        and local["upstream_status"] == "succeeded"
                        and local["delivery_status"] == "delivered"
                    ):
                        return Assessment(
                            "already_available",
                            "local_artifact_verified",
                            observation_id=observation_id,
                        )
                    if local["file"]["state"] == "digest_mismatch":
                        return Assessment(
                            "artifact_conflict",
                            "local_digest_mismatch",
                            observation_id=observation_id,
                        )
                    if stage == "form" and local["file"]["state"] == "missing":
                        candidate = _mock_copy(
                            db,
                            store,
                            task_id,
                            revision,
                            attempt_id,
                            artifact_id,
                            observation_id,
                            budget,
                        )
                        if candidate is not None:
                            return candidate
            rows = db.execute(
                "SELECT * FROM artifact_locator_evidence WHERE attempt_id=? AND observation_id=? ORDER BY locator_id",
                (attempt_id, observation_id),
            ).fetchall()
            if not rows:
                return Assessment(
                    "locator_unverified",
                    "locator_missing",
                    observation_id=observation_id,
                )
        return assess(store, LocatorEvidence(*rows[0]))
    except (ValueError, sqlite3.Error, OSError, TypeError):
        return Assessment("upstream_not_confirmed", "identity_or_storage_unverified")


@dataclass(frozen=True)
class VerifiedArtifactBytes:
    """Ephemeral internal result; not a delivered artifact or a Host authorization."""

    task_id: str
    revision: int
    stage: str
    root: str
    candidate_id: str
    attempt_id: str
    execution_digest: str
    artifact_id: str
    observation_id: int
    source: str
    evidence_id: str
    sha256: str
    mime: str
    width: int
    height: int
    size: int
    expected_digest_verified: bool
    data: bytes = field(repr=False)
    _proof: bytes = field(default=b"", repr=False, compare=False)


_process_key = secrets.token_bytes(32)


def _proof_for(result: VerifiedArtifactBytes) -> bytes:
    values = (
        result.task_id,
        result.revision,
        result.stage,
        result.root,
        result.candidate_id,
        result.attempt_id,
        result.execution_digest,
        result.artifact_id,
        result.observation_id,
        result.source,
        result.evidence_id,
        result.sha256,
        result.mime,
        result.width,
        result.height,
        result.size,
        result.expected_digest_verified,
    )
    return hmac.digest(
        _process_key, repr(values).encode("utf-8") + b"\x00" + result.data, "sha256"
    )


def verify_acquired_bytes(result: VerifiedArtifactBytes) -> bool:
    """Check process-local integrity; never substitute for publication-time source checks."""
    return hmac.compare_digest(result._proof, _proof_for(result))


def _verify_fixture_access(evidence: LocatorEvidence) -> bool:
    """No real retrieval or fee authorization exists in the default configuration."""
    from .team_artifact_evidence import recorded

    return recorded(evidence)


def _fetch_fixture(
    evidence: LocatorEvidence, budget: ResourceBudget
) -> tuple[bytes, str]:
    raise ValueError("fixture_source_unavailable")


def _verify_fixture_content(evidence: LocatorEvidence, data: bytes) -> bool:
    from .team_artifact_evidence import content

    return content(evidence, data)


def acquire_original_bytes(
    store: Store,
    root: str,
    candidate_id: str,
    task_id: str,
    revision: int,
    stage: str,
    attempt_id: str,
    digest: str,
    observation_id: int,
    *,
    artifact_id: str,
    artifact_root: Path,
    budget: ResourceBudget,
) -> VerifiedArtifactBytes:
    """Revalidate identity and acquire bytes only from controlled internal sources.

    No file, generation attempt, eligibility, or delivery record is created.
    """
    # Fetch fixture bytes without a SQLite write lock. Recheck the complete
    # identity and locator after fetching, within the transaction below.
    preliminary = assess_attempt(
        store,
        root,
        candidate_id,
        task_id,
        revision,
        stage,
        attempt_id,
        digest,
        observation_id,
        artifact_id=artifact_id,
        artifact_root=artifact_root,
        budget=budget,
    )
    fetched: tuple[LocatorEvidence, bytes, str] | None = None
    if preliminary.state == "remote_retrieval_candidate":
        evidence = read_locator(store, preliminary.locator_id or "")
        if (
            evidence is None
            or evidence.locator_type != "fixture_handle"
            or not _verify_fixture_access(evidence)
        ):
            raise ValueError("fixture_access_unverified")
        try:
            data, mime = _fetch_fixture(evidence, budget)
        except Exception:  # noqa: BLE001 - sanitize untrusted adapter errors
            raise ValueError("fixture_source_failed") from None
        if not isinstance(data, bytes) or len(data) > budget.max_file_bytes:
            raise ValueError("fixture_content_invalid")
        fetched = (evidence, data, mime)
    with closing(store._connect()) as db:
        version = db.execute("PRAGMA data_version").fetchone()[0]
        db.execute("BEGIN")
        try:
            result = _acquire_in_transaction(
                db,
                store,
                root,
                candidate_id,
                task_id,
                revision,
                stage,
                attempt_id,
                digest,
                observation_id,
                artifact_id=artifact_id,
                artifact_root=artifact_root,
                budget=budget,
                _fetched=fetched,
                _read_only=True,
            )
        finally:
            db.rollback()
        if db.execute("PRAGMA data_version").fetchone()[0] != version:
            raise ValueError("source_changed_during_acquisition")
        return result


def _acquire_in_transaction(
    db: sqlite3.Connection,
    store: Store,
    root: str,
    candidate_id: str,
    task_id: str,
    revision: int,
    stage: str,
    attempt_id: str,
    digest: str,
    observation_id: int,
    *,
    artifact_id: str,
    artifact_root: Path,
    budget: ResourceBudget,
    _fetched: tuple[LocatorEvidence, bytes, str] | None = None,
    _read_only: bool = False,
) -> VerifiedArtifactBytes:
    """Revalidate bytes under the caller's write transaction; never commit it."""
    if not db.in_transaction:
        raise ValueError("transaction_required")
    # Assessment also reads through independent connections. Uncommitted caller
    # changes would be invisible there, so only a fresh connection is safe.
    if db.total_changes:
        raise ValueError("transaction_has_prior_changes")
    databases = db.execute("PRAGMA database_list").fetchall()
    main = next((row[2] for row in databases if row[1] == "main"), None)
    if not main or Path(main).resolve() != store.path.resolve():
        raise ValueError("transaction_database_mismatch")
    if not _read_only:
        db.execute("UPDATE task SET task_id=task_id WHERE 0")
    assessment = assess_attempt(
        store,
        root,
        candidate_id,
        task_id,
        revision,
        stage,
        attempt_id,
        digest,
        observation_id,
        artifact_id=artifact_id,
        artifact_root=artifact_root,
        budget=budget,
    )
    evidence: LocatorEvidence | None = None
    if assessment.state == "recover_from_local_bytes":
        row = db.execute(
            "SELECT data,mime FROM prepared_reference WHERE task_id=? AND revision=? AND position=0 AND ref_id=?",
            (task_id, assessment.source_mock_revision, artifact_id),
        ).fetchone()
        if row is None or not isinstance(row[0], bytes):
            raise ValueError("local_copy_unavailable")
        data, mime = row
        source = "mock_frozen_form_input"
        evidence_id = assessment.source_review_id
        expected = assessment.artifact_sha256
    elif assessment.state == "remote_retrieval_candidate":
        evidence = read_locator(store, assessment.locator_id or "")
        if (
            evidence is None
            or evidence.locator_type != "fixture_handle"
            or not _verify_fixture_access(evidence)
        ):
            raise ValueError("fixture_access_unverified")
        if _fetched is None or _fetched[0] != evidence:
            raise ValueError("fixture_source_changed")
        _, data, mime = _fetched
        if not _verify_fixture_content(evidence, data):
            raise ValueError("fixture_content_unverified")
        source = "fixture_locator"
        evidence_id = evidence.locator_id
        row = db.execute(
            "SELECT sha256 FROM artifact WHERE artifact_id=? AND attempt_id=?",
            (artifact_id, attempt_id),
        ).fetchone()
        expected = row[0] if row else None
    else:
        raise ValueError(assessment.reason)
    image = validate_image_bytes(data, mime, budget)
    if expected is not None and image.sha256 != expected:
        raise ValueError("artifact_digest_mismatch")
    if evidence_id is None:
        raise ValueError("source_evidence_missing")
    if source == "fixture_locator" and (
        evidence is None
        or evidence.expires <= time.time()
        or not _verify_fixture_access(evidence)
    ):
        raise ValueError("fixture_access_expired")
    result = VerifiedArtifactBytes(
        task_id,
        revision,
        stage,
        root,
        candidate_id,
        attempt_id,
        digest,
        artifact_id,
        observation_id,
        source,
        evidence_id,
        image.sha256,
        image.mime,
        image.width,
        image.height,
        len(data),
        expected is not None,
        data,
    )
    object.__setattr__(result, "_proof", _proof_for(result))
    return result
