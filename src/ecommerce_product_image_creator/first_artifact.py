"""Controlled first-artifact evidence anchor and offline publication.

Production has no verified original-byte or independent digest source. This
module must fail closed until an integration supplies those verifiers.
"""

import hashlib
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

from . import artifact_locator, preparation, recovery_intent
from .storage import (
    ResourceBudget,
    Store,
    _record_verified_artifact_recovery,
    read_reference,
    validate_image_bytes,
)

if TYPE_CHECKING:
    from .download_safety import DownloadCandidate


def _verify_digest_source(
    locator: artifact_locator.LocatorEvidence, digest: str, mime: str
) -> bool:
    """An opaque locator or caller-supplied hash is not content attestation."""
    from .team_artifact_evidence import recorded

    return recorded(locator, digest, mime)


def _hook(phase: str) -> None:
    """Private isolated crash-injection seam."""


def _source(
    db: sqlite3.Connection, store: Store, row: tuple
) -> artifact_locator.LocatorEvidence:
    locator = artifact_locator.read_locator(store, row[3])
    if (
        locator is None
        or locator.attempt_id != row[2]
        or locator.expires <= time.time()
        or not artifact_locator._checked(db, store, locator)
        or not artifact_locator._verify_fixture_access(locator)
        or not _verify_digest_source(locator, row[4], row[5])
    ):
        raise ValueError("first_source_unverified")
    if locator.stage == "mock":
        preparation.check_mock_review_current(db, locator.task_id, locator.revision)
    return locator


def register_anchor(
    store: Store,
    delivery_id: str,
    artifact_id: str,
    locator_id: str,
    expected_sha256: str,
    mime: str,
    artifact_root: Path,
    target: Path,
) -> str:
    """Persist immutable first-delivery identity before touching a file."""
    if (
        not all(
            re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", x)
            for x in (delivery_id, artifact_id, locator_id)
        )
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or mime not in {"image/png", "image/jpeg"}
    ):
        raise ValueError("first_identity_invalid")
    if (
        not artifact_root.is_absolute()
        or not target.is_absolute()
        or not target.is_relative_to(artifact_root)
        or ".." in target.parts
        or str(target).startswith("\\\\")
    ):
        raise ValueError("target_path_unverified")
    # Staging and target are siblings, pinned as NTFS directories at each use.
    stage = target.parent / (
        ".first-artifact-" + hashlib.sha256(delivery_id.encode()).hexdigest() + ".tmp"
    )
    if stage == target:
        raise ValueError("target_path_unverified")
    locator = artifact_locator.read_locator(store, locator_id)
    if locator is None:
        raise ValueError("first_source_unverified")
    values = (
        delivery_id,
        artifact_id,
        locator.attempt_id,
        locator_id,
        expected_sha256,
        mime,
        str(target),
        str(stage),
    )
    with (
        recovery_intent._pin_directories(target.parent),
        closing(store._connect()) as db,
    ):
        db.execute("BEGIN IMMEDIATE")
        try:
            _source(db, store, values)
            prior = db.execute(
                "SELECT delivery_id,artifact_id,attempt_id,locator_id,expected_sha256,expected_mime,target_path,stage_path FROM first_artifact_anchor WHERE delivery_id=? OR artifact_id=? OR attempt_id=? OR target_path=?",
                (delivery_id, artifact_id, locator.attempt_id, str(target)),
            ).fetchall()
            if prior:
                if prior != [values]:
                    raise ValueError("first_identity_conflict")
                db.rollback()
                return delivery_id
            if db.execute(
                "SELECT 1 FROM artifact WHERE artifact_id=? OR attempt_id=? OR path=?",
                (artifact_id, locator.attempt_id, str(target)),
            ).fetchone():
                raise ValueError("artifact_conflict")
            db.execute(
                "INSERT INTO first_artifact_anchor VALUES (?,?,?,?,?,?,?,?,?)",
                (*values, time.time()),
            )
            db.commit()
            return delivery_id
        except BaseException:
            db.rollback()
            raise


def _anchor(db: sqlite3.Connection, delivery_id: str) -> tuple:
    row = db.execute(
        "SELECT delivery_id,artifact_id,attempt_id,locator_id,expected_sha256,expected_mime,target_path,stage_path FROM first_artifact_anchor WHERE delivery_id=?",
        (delivery_id,),
    ).fetchone()
    if row is None:
        raise ValueError("first_anchor_missing")
    return row


def _stage_identity(path: Path) -> tuple[str, str, int, int]:
    info = path.lstat()
    return str(info.st_dev), str(info.st_ino), info.st_size, info.st_mtime_ns


def _download_source(
    db: sqlite3.Connection,
    store: Store,
    row: tuple,
    url: str,
    candidate: "DownloadCandidate",
) -> bytes:
    """Recheck authoritative identity, independent digest and download scope."""
    from . import download_safety

    locator = _source(db, store, row)
    if (
        locator.stage not in {"form", "mock"}
        or candidate.locator_id != row[3]
        or candidate.sha256 != row[4]
        or candidate.mime != row[5]
        or not isinstance(candidate.data, bytes)
        or hashlib.sha256(candidate.data).hexdigest() != row[4]
        or not download_safety._verify_download_scope(locator, url, row[4], row[5])
        or not artifact_locator._verify_fixture_content(locator, candidate.data)
    ):
        raise ValueError("download_delivery_source_unverified")
    return candidate.data


def record_download_save_failure(
    store: Store, delivery_id: str, url: str, candidate: "DownloadCandidate"
) -> None:
    """Record a local failure only against an independently verified success."""
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = _anchor(db, delivery_id)
            _download_source(db, store, row, url, candidate)
            if db.execute(
                "SELECT 1 FROM artifact WHERE attempt_id=?", (row[2],)
            ).fetchone():
                raise ValueError("delivery_already_recorded")
            result = db.execute(
                "UPDATE attempt SET delivery_status='save_failed' WHERE attempt_id=? AND delivery_status IN ('not_available','pending','save_failed')",
                (row[2],),
            )
            if result.rowcount != 1:
                raise ValueError("delivery_state_conflict")
            db.commit()
        except BaseException:
            db.rollback()
            raise


def record_download_failure(
    store: Store, locator_id: str, url: str, digest: str, mime: str, stage: str
) -> None:
    """Persist a download failure without manufacturing a delivery anchor."""
    from . import download_safety

    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            locator = artifact_locator.read_locator(store, locator_id)
            if locator is None or locator.stage != stage:
                raise ValueError("download_source_unverified")
            row = (None, None, locator.attempt_id, locator_id, digest, mime)
            checked = _source(db, store, row)
            download_safety._target(url)
            if not download_safety._verify_download_scope(checked, url, digest, mime):
                raise ValueError("download_source_unverified")
            if db.execute(
                "SELECT 1 FROM artifact WHERE attempt_id=?", (locator.attempt_id,)
            ).fetchone():
                raise ValueError("delivery_already_recorded")
            result = db.execute(
                "UPDATE attempt SET delivery_status='download_failed' WHERE attempt_id=? AND delivery_status IN ('not_available','pending','download_failed')",
                (locator.attempt_id,),
            )
            if result.rowcount != 1:
                raise ValueError("delivery_state_conflict")
            db.commit()
        except BaseException:
            db.rollback()
            raise


def stage(
    store: Store,
    delivery_id: str,
    artifact_root: Path,
    budget: ResourceBudget,
    *,
    _download: tuple[str, "DownloadCandidate"] | None = None,
) -> str:
    with closing(store._connect()) as db:
        row = _anchor(db, delivery_id)
        locator = _source(db, store, row)
    target, stage_path = Path(row[6]), Path(row[7])
    if not target.is_relative_to(artifact_root) or stage_path.parent != target.parent:
        raise ValueError("target_path_unverified")
    with recovery_intent._pin_directories(target.parent):
        if _download is None:
            data, mime = artifact_locator._fetch_fixture(locator, budget)
        else:
            with closing(store._connect()) as db:
                data = _download_source(db, store, row, *_download)
            mime = row[5]
        if (
            not isinstance(data, bytes)
            or mime != row[5]
            or not artifact_locator._verify_fixture_content(locator, data)
        ):
            raise ValueError("first_content_unverified")
        image = validate_image_bytes(data, mime, budget)
        if image.sha256 != row[4]:
            raise ValueError("first_digest_conflict")
        try:
            fd = os.open(
                stage_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            with recovery_intent._pin_file(stage_path), closing(store._connect()) as db:
                recorded = db.execute(
                    "SELECT file_device,file_inode,file_size,file_mtime_ns FROM first_artifact_stage WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                if recorded is None or recorded != _stage_identity(stage_path):
                    raise ValueError("first_stage_conflict") from None
                existing = read_reference(artifact_root, stage_path, mime, budget)
                if existing.sha256 != row[4]:
                    raise ValueError("first_stage_conflict") from None
                _source(db, store, row)
                if _download is not None:
                    _download_source(db, store, row, *_download)
                return "staged"
        with os.fdopen(fd, "wb") as handle:
            midpoint = len(data) // 2
            handle.write(data[:midpoint])
            handle.flush()
            _hook("partial_write")
            handle.write(data[midpoint:])
            handle.flush()
            os.fsync(handle.fileno())
        _hook("written")
        with recovery_intent._pin_file(stage_path):
            if read_reference(artifact_root, stage_path, mime, budget).sha256 != row[4]:
                raise ValueError("first_stage_conflict")
            fingerprint = _stage_identity(stage_path)
            _hook("verified")
            with closing(store._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    if _anchor(db, delivery_id) != row:
                        raise ValueError("first_anchor_changed")
                    _source(db, store, row)
                    if _download is not None:
                        _download_source(db, store, row, *_download)
                    if fingerprint != _stage_identity(stage_path):
                        raise ValueError("first_stage_conflict")
                    db.execute(
                        "INSERT INTO first_artifact_stage VALUES (?,?,?,?,?)",
                        (delivery_id, *fingerprint),
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        return "staged"


def publish_and_reconcile(
    store: Store,
    delivery_id: str,
    artifact_root: Path,
    budget: ResourceBudget,
    *,
    _download: tuple[str, "DownloadCandidate"] | None = None,
) -> str:
    """Publish without overwrite, then independently commit Artifact association."""
    with closing(store._connect()) as db:
        row = _anchor(db, delivery_id)
    target, stage_path = Path(row[6]), Path(row[7])
    if not target.is_relative_to(artifact_root) or stage_path.parent != target.parent:
        raise ValueError("target_path_unverified")
    with (
        recovery_intent._pin_directories(target.parent),
        recovery_intent._pin_file(stage_path),
    ):
        staged = read_reference(artifact_root, stage_path, row[5], budget)
        if staged.sha256 != row[4]:
            raise ValueError("first_stage_conflict")
        with closing(store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if _anchor(db, delivery_id) != row:
                    raise ValueError("first_anchor_changed")
                _source(db, store, row)
                if _download is not None:
                    _download_source(db, store, row, *_download)
                recorded = db.execute(
                    "SELECT file_device,file_inode,file_size,file_mtime_ns FROM first_artifact_stage WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                if recorded is None or recorded != _stage_identity(stage_path):
                    raise ValueError("first_stage_conflict")
                original = db.execute(
                    "SELECT artifact_id,attempt_id,path,sha256 FROM artifact WHERE artifact_id=? OR attempt_id=? OR path=?",
                    (row[1], row[2], row[6]),
                ).fetchall()
                expected = (row[1], row[2], row[6], row[4])
                if original and original != [expected]:
                    raise ValueError("artifact_conflict")
                _hook("before_link")
                try:
                    os.link(stage_path, target)
                except FileExistsError:
                    pass
                _hook("after_link")
                with recovery_intent._pin_file(target):
                    target_info, stage_info = target.stat(), stage_path.stat()
                    if (target_info.st_dev, target_info.st_ino) != (
                        stage_info.st_dev,
                        stage_info.st_ino,
                    ) or read_reference(
                        artifact_root, target, row[5], budget
                    ).sha256 != row[4]:
                        raise ValueError("target_conflict")
                    if not original:
                        db.execute("INSERT INTO artifact VALUES (?,?,?,?)", expected)
                        updated = db.execute(
                            "UPDATE attempt SET delivery_status='delivered' WHERE attempt_id=? AND delivery_status IN ('not_available','pending','save_failed','download_failed')",
                            (row[2],),
                        )
                        if updated.rowcount != 1:
                            raise ValueError("delivery_state_conflict")
                    else:
                        _record_verified_artifact_recovery(
                            db, row[1], row[2], row[4], "verified_first_artifact_link"
                        )
                    _hook("before_commit")
                    db.commit()
                _hook("after_commit")
                return "delivered"
            except BaseException:
                db.rollback()
                raise
