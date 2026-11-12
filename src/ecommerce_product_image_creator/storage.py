"""Local SQLite transaction primitives; no remote calls or authorization decision."""

import hashlib
import io
import math
import os
import sqlite3
import stat
import sys
import warnings
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, UnidentifiedImageError

from ecommerce_product_image_creator.domain import (
    DeliveryStatus,
    TaskStatus,
    UpstreamStatus,
    check_task_transition,
    check_transition,
)

if TYPE_CHECKING:
    from .download_safety import DownloadCandidate


class ConflictError(ValueError):
    """Duplicate, conflicting, or invalid local identity."""


class FileSafetyError(ValueError):
    """Input cannot be safely read or decoded."""


def _integrity_hook(phase: str) -> None:
    """Internal test seam; it never grants file or execution authority."""


def _record_verified_artifact_recovery(
    db: sqlite3.Connection, artifact_id: str, attempt_id: str, digest: str, reason: str
) -> None:
    """Called only while a verified, pinned file and the writer transaction are held."""
    status = db.execute(
        "SELECT a.delivery_status FROM attempt a JOIN artifact x ON x.attempt_id=a.attempt_id WHERE a.attempt_id=? AND x.artifact_id=? AND x.sha256=?",
        (attempt_id, artifact_id, digest),
    ).fetchone()
    if status != ("missing_or_corrupt",):
        return
    db.execute(
        "INSERT INTO artifact_integrity_observation(artifact_id,attempt_id,expected_sha256,state,reason,previous_status,observed_at) VALUES(?,?,?,?,?,?,?)",
        (
            artifact_id,
            attempt_id,
            digest,
            "delivered",
            reason,
            "missing_or_corrupt",
            datetime.now(UTC).timestamp(),
        ),
    )
    db.execute(
        "UPDATE attempt SET delivery_status='delivered' WHERE attempt_id=? AND delivery_status='missing_or_corrupt'",
        (attempt_id,),
    )


@dataclass(frozen=True)
class ResourceBudget:
    """Caller-supplied technical protections, not a business file size limit."""

    max_file_bytes: int
    max_pixels: int
    max_decoded_bytes: int

    def __post_init__(self) -> None:
        if min(self.max_file_bytes, self.max_pixels, self.max_decoded_bytes) <= 0:
            raise ValueError("Resource protections must be positive")


@dataclass(frozen=True)
class InputSnapshot:
    data: bytes
    sha256: str
    mime: str


@dataclass(frozen=True)
class ValidatedImage:
    sha256: str
    mime: str
    width: int
    height: int


def validate_image_bytes(
    data: bytes, declared_mime: str, budget: ResourceBudget
) -> ValidatedImage:
    """Validate actual image bytes with the same limits used for reference inputs."""
    if not data or len(data) > budget.max_file_bytes:
        raise FileSafetyError("Input exceeds technical resource protection")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                actual = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(
                    image.format or ""
                )
                if actual is None or actual != declared_mime:
                    raise FileSafetyError("Image format does not match declaration")
                width, height = image.size
                pixels = width * height
                if (
                    width <= 0
                    or height <= 0
                    or pixels > budget.max_pixels
                    or pixels * max(4, len(image.getbands())) > budget.max_decoded_bytes
                ):
                    raise FileSafetyError("Image exceeds technical decode protection")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        return ValidatedImage(hashlib.sha256(data).hexdigest(), actual, width, height)
    except FileSafetyError:
        raise
    except (
        OSError,
        ValueError,
        MemoryError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
        UnidentifiedImageError,
    ):
        raise FileSafetyError("Input cannot be safely read or decoded") from None


def _windows_handle_path(fd: int) -> Path:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = kernel.GetFinalPathNameByHandleW
    fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    fn.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(fd)
    required = fn(handle, None, 0, 0)
    if not required:
        raise FileSafetyError("Unable to verify opened file path")
    buffer = ctypes.create_unicode_buffer(required + 1)
    length = fn(handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise FileSafetyError("Unable to verify opened file path")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def read_reference(
    root: Path, path: Path, declared_mime: str, budget: ResourceBudget
) -> InputSnapshot:
    """Freeze validated local image bytes after containment, stability, and resource checks."""
    if not root.is_absolute() or not path.is_absolute():
        raise FileSafetyError("Absolute input paths required")
    if sys.platform == "win32" and (
        str(root).startswith("\\\\") or str(path).startswith("\\\\")
    ):
        raise FileSafetyError("Network input paths are not accepted")
    try:
        root_info = root.lstat()
        if (
            stat.S_ISLNK(root_info.st_mode)
            or getattr(root_info, "st_file_attributes", 0) & 0x400
        ):
            raise FileSafetyError("Input root cannot be a link or reparse point")
        root = root.resolve(strict=True)
        if not root.is_dir() or not path.is_relative_to(root):
            raise FileSafetyError("Input is outside the allowed root")
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise FileSafetyError("Links and reparse points are not accepted")
        if not path.resolve(strict=True).is_relative_to(root):
            raise FileSafetyError("Input is outside the allowed root")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise FileSafetyError("Input is not a regular file")
            if sys.platform == "win32" and not _windows_handle_path(
                source.fileno()
            ).resolve().is_relative_to(root):
                raise FileSafetyError("Opened file is outside the allowed root")
            data = source.read(budget.max_file_bytes + 1)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ):
                raise FileSafetyError("Input changed during reading")
            current_link = path.lstat()
            if (
                stat.S_ISLNK(current_link.st_mode)
                or getattr(current_link, "st_file_attributes", 0) & 0x400
            ):
                raise FileSafetyError("Input path changed during reading")
            current = path.stat()
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            ):
                raise FileSafetyError("Input path changed during reading")
            if len(data) > budget.max_file_bytes:
                raise FileSafetyError("Input exceeds technical resource protection")
        verified = validate_image_bytes(data, declared_mime, budget)
        return InputSnapshot(data=data, sha256=verified.sha256, mime=verified.mime)
    except FileSafetyError:
        raise
    except (
        OSError,
        ValueError,
        MemoryError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
        UnidentifiedImageError,
    ):
        raise FileSafetyError("Input cannot be safely read or decoded") from None


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS task (
                    task_id TEXT PRIMARY KEY, input_digest TEXT NOT NULL,
                    instructions TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft');
                CREATE TABLE IF NOT EXISTS revision (
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
                    input_digest TEXT NOT NULL, PRIMARY KEY(task_id, revision),
                    FOREIGN KEY(task_id) REFERENCES task(task_id));
                CREATE TABLE IF NOT EXISTS attempt (
                    attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    upstream_status TEXT NOT NULL, delivery_status TEXT NOT NULL,
                    upstream_id TEXT, previous_attempt_id TEXT,
                    FOREIGN KEY(task_id, revision) REFERENCES revision(task_id, revision),
                    FOREIGN KEY(previous_attempt_id) REFERENCES attempt(attempt_id));
                CREATE TABLE IF NOT EXISTS operation (
                    operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    digest TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE, attempt_count INTEGER NOT NULL CHECK(attempt_count > 0),
                    FOREIGN KEY(task_id, revision) REFERENCES revision(task_id, revision),
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id));
                CREATE TABLE IF NOT EXISTS artifact (
                    artifact_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
                    path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id));
                CREATE TABLE IF NOT EXISTS artifact_integrity_observation (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    expected_sha256 TEXT NOT NULL, state TEXT NOT NULL,
                    reason TEXT NOT NULL, previous_status TEXT NOT NULL, observed_at REAL NOT NULL,
                    FOREIGN KEY(artifact_id) REFERENCES artifact(artifact_id),
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id));
                CREATE TRIGGER IF NOT EXISTS artifact_integrity_no_update BEFORE UPDATE ON artifact_integrity_observation BEGIN SELECT RAISE(ABORT,'immutable integrity observation'); END;
                CREATE TRIGGER IF NOT EXISTS artifact_integrity_no_delete BEFORE DELETE ON artifact_integrity_observation BEGIN SELECT RAISE(ABORT,'immutable integrity observation'); END;
                CREATE TABLE IF NOT EXISTS first_artifact_anchor (
                    delivery_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL UNIQUE,
                    attempt_id TEXT NOT NULL UNIQUE, locator_id TEXT NOT NULL,
                    expected_sha256 TEXT NOT NULL, expected_mime TEXT NOT NULL,
                    target_path TEXT NOT NULL UNIQUE, stage_path TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
                    FOREIGN KEY(locator_id) REFERENCES artifact_locator_evidence(locator_id));
                CREATE TRIGGER IF NOT EXISTS first_artifact_anchor_no_update BEFORE UPDATE ON first_artifact_anchor BEGIN SELECT RAISE(ABORT,'immutable first artifact anchor'); END;
                CREATE TRIGGER IF NOT EXISTS first_artifact_anchor_no_delete BEFORE DELETE ON first_artifact_anchor BEGIN SELECT RAISE(ABORT,'immutable first artifact anchor'); END;
                CREATE TABLE IF NOT EXISTS first_artifact_stage (
                    delivery_id TEXT PRIMARY KEY, file_device TEXT NOT NULL, file_inode TEXT NOT NULL,
                    file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL,
                    FOREIGN KEY(delivery_id) REFERENCES first_artifact_anchor(delivery_id));
                CREATE TRIGGER IF NOT EXISTS first_artifact_stage_no_update BEFORE UPDATE ON first_artifact_stage BEGIN SELECT RAISE(ABORT,'immutable first artifact stage'); END;
                CREATE TRIGGER IF NOT EXISTS first_artifact_stage_no_delete BEFORE DELETE ON first_artifact_stage BEGIN SELECT RAISE(ABORT,'immutable first artifact stage'); END;
                CREATE TABLE IF NOT EXISTS artifact_recovery_intent (
                    recovery_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL UNIQUE,
                    root_operation_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, stage TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, execution_digest TEXT NOT NULL,
                    observation_id INTEGER NOT NULL, expected_sha256 TEXT NOT NULL,
                    target_path TEXT NOT NULL, source TEXT NOT NULL, evidence_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','staged')),
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    FOREIGN KEY(artifact_id) REFERENCES artifact(artifact_id),
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id));
                CREATE TABLE IF NOT EXISTS artifact_recovery_stage (
                    recovery_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE,
                    sha256 TEXT NOT NULL, mime TEXT NOT NULL, byte_count INTEGER NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL,
                    file_device TEXT NOT NULL, file_inode TEXT NOT NULL,
                    file_mtime_ns INTEGER NOT NULL, created_at REAL NOT NULL,
                    FOREIGN KEY(recovery_id) REFERENCES artifact_recovery_intent(recovery_id));
                CREATE TABLE IF NOT EXISTS artifact_recovery_publication (
                    recovery_id TEXT PRIMARY KEY, target_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, file_device TEXT NOT NULL, file_inode TEXT NOT NULL,
                    file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL,
                    published_at REAL NOT NULL,
                    FOREIGN KEY(recovery_id) REFERENCES artifact_recovery_intent(recovery_id));
                CREATE TABLE IF NOT EXISTS artifact_recovery_link_observation (
                    recovery_id TEXT PRIMARY KEY, target_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, file_device TEXT NOT NULL, file_inode TEXT NOT NULL,
                    file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL,
                    observed_at REAL NOT NULL, reason TEXT NOT NULL,
                    FOREIGN KEY(recovery_id) REFERENCES artifact_recovery_intent(recovery_id));
                CREATE TABLE IF NOT EXISTS artifact_recovery_reconciliation (
                    recovery_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL, execution_digest TEXT NOT NULL, task_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, stage TEXT NOT NULL, expected_sha256 TEXT NOT NULL,
                    target_path TEXT NOT NULL, file_device TEXT NOT NULL, file_inode TEXT NOT NULL,
                    file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL, basis TEXT NOT NULL,
                    reconciled_at REAL NOT NULL, reason TEXT NOT NULL,
                    FOREIGN KEY(recovery_id) REFERENCES artifact_recovery_intent(recovery_id),
                    FOREIGN KEY(artifact_id) REFERENCES artifact(artifact_id));
                CREATE TRIGGER IF NOT EXISTS recovery_link_observation_no_update BEFORE UPDATE ON artifact_recovery_link_observation BEGIN SELECT RAISE(ABORT,'immutable recovery observation'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_link_observation_no_delete BEFORE DELETE ON artifact_recovery_link_observation BEGIN SELECT RAISE(ABORT,'immutable recovery observation'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_reconciliation_no_update BEFORE UPDATE ON artifact_recovery_reconciliation BEGIN SELECT RAISE(ABORT,'immutable reconciliation'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_reconciliation_no_delete BEFORE DELETE ON artifact_recovery_reconciliation BEGIN SELECT RAISE(ABORT,'immutable reconciliation'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_publication_no_update BEFORE UPDATE ON artifact_recovery_publication BEGIN SELECT RAISE(ABORT,'immutable publication'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_publication_no_delete BEFORE DELETE ON artifact_recovery_publication BEGIN SELECT RAISE(ABORT,'immutable publication'); END;
                CREATE TABLE IF NOT EXISTS query_invocation (
                    invocation_id TEXT PRIMARY KEY, root_operation_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    stage TEXT NOT NULL, attempt_id TEXT NOT NULL, execution_digest TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, endpoint TEXT NOT NULL, grouping TEXT NOT NULL,
                    account_scope TEXT, upstream_id TEXT NOT NULL, source TEXT NOT NULL,
                    authorization_state TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, observation_id INTEGER,
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
                    FOREIGN KEY(candidate_id) REFERENCES execution_candidate(candidate_id),
                    FOREIGN KEY(observation_id) REFERENCES recovery_observation(observation_id));
                CREATE TABLE IF NOT EXISTS query_request (
                    request_id TEXT PRIMARY KEY, scope_payload TEXT NOT NULL,
                    authorization_id TEXT NOT NULL UNIQUE, invocation_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(invocation_id) REFERENCES query_invocation(invocation_id));
                CREATE TRIGGER IF NOT EXISTS query_invocation_no_delete BEFORE DELETE ON query_invocation BEGIN SELECT RAISE(ABORT,'immutable query invocation'); END;
                CREATE TABLE IF NOT EXISTS query_authorization (
                    authorization_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    evidence_payload TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS query_authorization_revocation (
                    authorization_id TEXT PRIMARY KEY, source_reference TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    FOREIGN KEY(authorization_id) REFERENCES query_authorization(authorization_id));
                CREATE TABLE IF NOT EXISTS query_authorization_consumption (
                    authorization_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    invocation_id TEXT NOT NULL UNIQUE, consumed_at REAL NOT NULL,
                    FOREIGN KEY(authorization_id) REFERENCES query_authorization(authorization_id),
                    FOREIGN KEY(request_id) REFERENCES query_request(request_id),
                    FOREIGN KEY(invocation_id) REFERENCES query_invocation(invocation_id));
                CREATE TABLE IF NOT EXISTS query_dispatch_stop (
                    invocation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    authorization_id TEXT NOT NULL, entered INTEGER NOT NULL CHECK(entered IN (0,1)),
                    reason TEXT NOT NULL CHECK(reason IN ('revoked','expired')),
                    source TEXT NOT NULL CHECK(source='server_pre_dispatch'), recorded_at REAL NOT NULL,
                    FOREIGN KEY(invocation_id) REFERENCES query_invocation(invocation_id),
                    FOREIGN KEY(request_id) REFERENCES query_request(request_id),
                    FOREIGN KEY(authorization_id) REFERENCES query_authorization(authorization_id));
                CREATE TABLE IF NOT EXISTS query_request_observation_baseline (
                    request_id TEXT PRIMARY KEY, observation_id INTEGER,
                    FOREIGN KEY(request_id) REFERENCES query_request(request_id),
                    FOREIGN KEY(observation_id) REFERENCES recovery_observation(observation_id));
                CREATE TRIGGER IF NOT EXISTS query_request_baseline_no_update BEFORE UPDATE ON query_request_observation_baseline BEGIN SELECT RAISE(ABORT,'immutable query baseline'); END;
                CREATE TRIGGER IF NOT EXISTS query_request_baseline_no_delete BEFORE DELETE ON query_request_observation_baseline BEGIN SELECT RAISE(ABORT,'immutable query baseline'); END;
                CREATE TRIGGER IF NOT EXISTS query_dispatch_stop_no_update BEFORE UPDATE ON query_dispatch_stop BEGIN SELECT RAISE(ABORT,'immutable query stop'); END;
                CREATE TRIGGER IF NOT EXISTS query_dispatch_stop_no_delete BEFORE DELETE ON query_dispatch_stop BEGIN SELECT RAISE(ABORT,'immutable query stop'); END;
                CREATE TRIGGER IF NOT EXISTS query_request_no_update BEFORE UPDATE ON query_request BEGIN SELECT RAISE(ABORT,'immutable query request'); END;
                CREATE TRIGGER IF NOT EXISTS query_request_no_delete BEFORE DELETE ON query_request BEGIN SELECT RAISE(ABORT,'immutable query request'); END;
                CREATE TRIGGER IF NOT EXISTS query_authorization_no_update BEFORE UPDATE ON query_authorization BEGIN SELECT RAISE(ABORT,'immutable query authorization'); END;
                CREATE TRIGGER IF NOT EXISTS query_authorization_no_delete BEFORE DELETE ON query_authorization BEGIN SELECT RAISE(ABORT,'immutable query authorization'); END;
                CREATE TRIGGER IF NOT EXISTS query_authorization_revocation_no_update BEFORE UPDATE ON query_authorization_revocation BEGIN SELECT RAISE(ABORT,'immutable query revocation'); END;
                CREATE TRIGGER IF NOT EXISTS query_authorization_revocation_no_delete BEFORE DELETE ON query_authorization_revocation BEGIN SELECT RAISE(ABORT,'immutable query revocation'); END;
                CREATE TRIGGER IF NOT EXISTS query_authorization_consumption_no_update BEFORE UPDATE ON query_authorization_consumption BEGIN SELECT RAISE(ABORT,'immutable query consumption'); END;
                CREATE TRIGGER IF NOT EXISTS query_authorization_consumption_no_delete BEFORE DELETE ON query_authorization_consumption BEGIN SELECT RAISE(ABORT,'immutable query consumption'); END;
                CREATE TABLE IF NOT EXISTS query_billing_event (
                    provider TEXT NOT NULL, account_scope TEXT NOT NULL, event_id TEXT NOT NULL,
                    invocation_id TEXT NOT NULL, amount TEXT, currency TEXT,
                    usage_quantity TEXT, usage_unit TEXT, source TEXT NOT NULL, recorded_at REAL NOT NULL,
                    PRIMARY KEY(provider,account_scope,event_id),
                    FOREIGN KEY(invocation_id) REFERENCES query_invocation(invocation_id));
                CREATE TABLE IF NOT EXISTS query_usage_evidence (
                    evidence_id TEXT PRIMARY KEY, invocation_id TEXT NOT NULL,
                    provider TEXT NOT NULL, account_scope TEXT NOT NULL, quantity TEXT NOT NULL,
                    unit TEXT NOT NULL, source TEXT NOT NULL, recorded_at REAL NOT NULL,
                    FOREIGN KEY(invocation_id) REFERENCES query_invocation(invocation_id));
                CREATE TRIGGER IF NOT EXISTS query_usage_no_update BEFORE UPDATE ON query_usage_evidence BEGIN SELECT RAISE(ABORT,'immutable usage evidence'); END;
                CREATE TRIGGER IF NOT EXISTS query_usage_no_delete BEFORE DELETE ON query_usage_evidence BEGIN SELECT RAISE(ABORT,'immutable usage evidence'); END;
                CREATE TRIGGER IF NOT EXISTS query_billing_no_update BEFORE UPDATE ON query_billing_event BEGIN SELECT RAISE(ABORT,'immutable billing evidence'); END;
                CREATE TRIGGER IF NOT EXISTS query_billing_no_delete BEFORE DELETE ON query_billing_event BEGIN SELECT RAISE(ABORT,'immutable billing evidence'); END;
                CREATE TABLE IF NOT EXISTS local_marker (marker_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS attempt_counter (
                    operation_id TEXT NOT NULL, model TEXT NOT NULL, endpoint TEXT NOT NULL,
                    count INTEGER NOT NULL CHECK(count BETWEEN 1 AND 3),
                    PRIMARY KEY(operation_id, model, endpoint),
                    FOREIGN KEY(operation_id) REFERENCES operation(operation_id));
                CREATE TABLE IF NOT EXISTS operation_attempt (
                    operation_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence > 0),
                    attempt_id TEXT NOT NULL UNIQUE, previous_attempt_id TEXT,
                    model TEXT NOT NULL, endpoint TEXT NOT NULL, execution_digest TEXT NOT NULL,
                    PRIMARY KEY(operation_id, sequence),
                    FOREIGN KEY(operation_id) REFERENCES operation(operation_id),
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
                    FOREIGN KEY(previous_attempt_id) REFERENCES attempt(attempt_id));
                CREATE TABLE IF NOT EXISTS retry_wait (
                    operation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
                    started REAL NOT NULL, deadline REAL NOT NULL, observed REAL NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES operation(operation_id),
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id));
                CREATE TABLE IF NOT EXISTS retry_wait_attempt (
                    operation_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    started REAL NOT NULL, deadline REAL NOT NULL, observed REAL NOT NULL,
                    PRIMARY KEY(operation_id,attempt_id),
                    FOREIGN KEY(operation_id) REFERENCES operation(operation_id),
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id));
                CREATE TABLE IF NOT EXISTS attempt_transport_evidence (
                    attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
                    logical_operation_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, execution_digest TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK(phase IN ('pre_send_failure','entered')),
                    reason TEXT NOT NULL, source TEXT NOT NULL, recorded_at REAL NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
                    FOREIGN KEY(operation_id) REFERENCES operation(operation_id),
                    FOREIGN KEY(logical_operation_id) REFERENCES operation(operation_id));
                CREATE TABLE IF NOT EXISTS attempt_fault_evidence (
                    attempt_id TEXT PRIMARY KEY, logical_operation_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    execution_digest TEXT NOT NULL, category TEXT NOT NULL,
                    source TEXT NOT NULL, recorded_at REAL NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
                    FOREIGN KEY(logical_operation_id) REFERENCES operation(operation_id));
                CREATE TABLE IF NOT EXISTS execution_candidate (
                    candidate_id TEXT PRIMARY KEY, root_operation_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    source_candidate_id TEXT, snapshot_payload TEXT NOT NULL,
                    execution_digest TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(root_operation_id, execution_digest),
                    FOREIGN KEY(root_operation_id) REFERENCES operation(operation_id),
                    FOREIGN KEY(task_id, revision) REFERENCES revision(task_id, revision),
                    FOREIGN KEY(source_candidate_id) REFERENCES execution_candidate(candidate_id));
                CREATE TABLE IF NOT EXISTS candidate_attempt (
                    attempt_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    root_operation_id TEXT NOT NULL, combination_sequence INTEGER NOT NULL CHECK(combination_sequence > 0),
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
                    FOREIGN KEY(candidate_id) REFERENCES execution_candidate(candidate_id),
                    FOREIGN KEY(root_operation_id) REFERENCES operation(operation_id));
                CREATE TABLE IF NOT EXISTS recovery_observation (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root_operation_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, stage TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, execution_digest TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, endpoint TEXT NOT NULL,
                    grouping TEXT NOT NULL, upstream_id TEXT NOT NULL, source TEXT NOT NULL,
                    response_digest TEXT, remote_state TEXT NOT NULL, reason TEXT NOT NULL,
                    observed_at REAL NOT NULL, previous_id INTEGER,
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
                    FOREIGN KEY(candidate_id) REFERENCES execution_candidate(candidate_id),
                    FOREIGN KEY(root_operation_id) REFERENCES operation(operation_id),
                    FOREIGN KEY(previous_id) REFERENCES recovery_observation(observation_id));
                CREATE TRIGGER IF NOT EXISTS recovery_observation_no_update BEFORE UPDATE ON recovery_observation BEGIN SELECT RAISE(ABORT,'immutable observation'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_observation_no_delete BEFORE DELETE ON recovery_observation BEGIN SELECT RAISE(ABORT,'immutable observation'); END;
                CREATE TABLE IF NOT EXISTS artifact_locator_evidence (
                    locator_id TEXT PRIMARY KEY, root_operation_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    stage TEXT NOT NULL, attempt_id TEXT NOT NULL, execution_digest TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, endpoint TEXT NOT NULL,
                    grouping TEXT NOT NULL, scope TEXT NOT NULL, upstream_id TEXT NOT NULL,
                    observation_id INTEGER NOT NULL, artifact_identity TEXT NOT NULL,
                    locator_type TEXT NOT NULL, source TEXT NOT NULL, handle TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL,
                    FOREIGN KEY(observation_id) REFERENCES recovery_observation(observation_id),
                    FOREIGN KEY(attempt_id) REFERENCES attempt(attempt_id),
                    FOREIGN KEY(candidate_id) REFERENCES execution_candidate(candidate_id));
                CREATE TRIGGER IF NOT EXISTS artifact_locator_no_update BEFORE UPDATE ON artifact_locator_evidence BEGIN SELECT RAISE(ABORT,'immutable locator'); END;
                CREATE TRIGGER IF NOT EXISTS artifact_locator_no_delete BEFORE DELETE ON artifact_locator_evidence BEGIN SELECT RAISE(ABORT,'immutable locator'); END;
                CREATE UNIQUE INDEX IF NOT EXISTS execution_candidate_root
                    ON execution_candidate(root_operation_id) WHERE source_candidate_id IS NULL;
                CREATE TRIGGER IF NOT EXISTS execution_candidate_no_update
                    BEFORE UPDATE ON execution_candidate BEGIN SELECT RAISE(ABORT,'immutable candidate'); END;
                CREATE TRIGGER IF NOT EXISTS execution_candidate_no_delete
                    BEFORE DELETE ON execution_candidate BEGIN SELECT RAISE(ABORT,'immutable candidate'); END;
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def register_first_artifact(
        self,
        delivery_id: str,
        artifact_id: str,
        locator_id: str,
        expected_sha256: str,
        mime: str,
        artifact_root: Path,
        target: Path,
    ) -> str:
        from .first_artifact import register_anchor

        return register_anchor(
            self,
            delivery_id,
            artifact_id,
            locator_id,
            expected_sha256,
            mime,
            artifact_root,
            target,
        )

    def stage_first_artifact(
        self,
        delivery_id: str,
        artifact_root: Path,
        budget: ResourceBudget,
        *,
        _download: tuple[str, "DownloadCandidate"] | None = None,
    ) -> str:
        from .first_artifact import stage

        return stage(self, delivery_id, artifact_root, budget, _download=_download)

    def reconcile_first_artifact(
        self,
        delivery_id: str,
        artifact_root: Path,
        budget: ResourceBudget,
        *,
        _download: tuple[str, "DownloadCandidate"] | None = None,
    ) -> str:
        from .first_artifact import publish_and_reconcile

        return publish_and_reconcile(
            self, delivery_id, artifact_root, budget, _download=_download
        )

    def create_task(self, task_id: str, input_digest: str, instructions: str) -> None:
        if not all((task_id, input_digest, instructions.strip())):
            raise ValueError("Task fields must be nonempty")
        try:
            with closing(self._connect()) as db, db:
                db.execute(
                    "INSERT INTO task(task_id,input_digest,instructions) VALUES(?,?,?)",
                    (task_id, input_digest, instructions),
                )
        except sqlite3.IntegrityError:
            raise ConflictError("Task conflict") from None

    def create_revision(self, task_id: str, revision: int, digest: str) -> None:
        try:
            with closing(self._connect()) as db, db:
                db.execute(
                    "INSERT INTO revision VALUES(?,?,?)", (task_id, revision, digest)
                )
        except sqlite3.IntegrityError:
            raise ConflictError("Revision conflict") from None

    def transition_task(self, task_id: str, status: TaskStatus) -> None:
        """Advance local task state; READY is never proof of authorization."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM task WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise ConflictError("Task not found")
            check_task_transition(TaskStatus(row[0]), status)
            db.execute(
                "UPDATE task SET status=? WHERE task_id=?", (status.value, task_id)
            )

    def claim_operation(
        self,
        operation_id: str,
        task_id: str,
        revision: int,
        digest: str,
        attempt_id: str,
    ) -> bool:
        """Reserve a local operation once; this is not authorization or remote exactly-once."""
        if not all((operation_id, task_id, digest, attempt_id)):
            raise ValueError("Operation fields must be nonempty")
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT task_id,revision,digest FROM operation WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row is not None:
                    if row != (task_id, revision, digest):
                        raise ConflictError(
                            "Operation identity conflicts with existing snapshot"
                        )
                    return False
                db.execute(
                    "INSERT INTO attempt(attempt_id,task_id,revision,upstream_status,delivery_status) VALUES(?,?,?,?,?)",
                    (
                        attempt_id,
                        task_id,
                        revision,
                        "submission_uncertain",
                        "not_available",
                    ),
                )
                db.execute(
                    "INSERT INTO operation VALUES(?,?,?,?,?,?)",
                    (operation_id, task_id, revision, digest, attempt_id, 1),
                )
                return True
        except sqlite3.IntegrityError:
            raise ConflictError("Operation or attempt conflict") from None

    def get_operation(self, operation_id: str) -> "StoredAttempt | None":
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT a.upstream_status,a.upstream_id,a.delivery_status,o.attempt_count FROM attempt a JOIN operation o ON o.attempt_id=a.attempt_id WHERE o.operation_id=?",
                (operation_id,),
            ).fetchone()
        return StoredAttempt(*row) if row else None

    def attempt_chain_status(self, operation_id: str) -> str:
        """Report local chain completeness, never retry eligibility."""
        with closing(self._connect()) as db:
            root = db.execute(
                "SELECT task_id,revision,digest,attempt_id,attempt_count FROM operation WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if not root or root[4] not in (1, 2, 3):
                return "unknown"
            chain = db.execute(
                "SELECT c.sequence,c.attempt_id,c.previous_attempt_id,c.model,c.endpoint,c.execution_digest,a.previous_attempt_id,a.task_id,a.revision,o.task_id,o.revision,o.digest FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id JOIN operation o ON o.attempt_id=c.attempt_id WHERE c.operation_id=? ORDER BY c.sequence",
                (operation_id,),
            ).fetchall()
            if len(chain) != root[4] or chain[0][1] != root[3]:
                return "unknown"
            previous = None
            model, endpoint = chain[0][3:5]
            for index, item in enumerate(chain, 1):
                if (
                    item[0] != index
                    or item[2] != previous
                    or item[3:6] != (model, endpoint, root[2])
                    or item[6] != previous
                    or item[7:9] != root[:2]
                    or item[9:12] != root[:3]
                ):
                    return "unknown"
                previous = item[1]
            counts = db.execute(
                "SELECT model,endpoint,count FROM attempt_counter WHERE operation_id=?",
                (operation_id,),
            ).fetchall()
            if counts != [(model, endpoint, root[4])]:
                return "unknown"
            return {1: "initial_recorded", 2: "second_recorded", 3: "third_recorded"}[
                root[4]
            ]

    def record_upstream(
        self, attempt_id: str, status: UpstreamStatus, upstream_id: str
    ) -> None:
        """Record caller-supplied upstream evidence; callers must verify its source."""
        if not upstream_id:
            raise ValueError("Upstream task ID required")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT upstream_status,upstream_id FROM attempt WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ConflictError("Attempt not found")
            if row[1] is not None and row[1] != upstream_id:
                raise ConflictError("Upstream identity conflict")
            check_transition(UpstreamStatus(row[0]), status)
            db.execute(
                "UPDATE attempt SET upstream_status=?,upstream_id=? WHERE attempt_id=?",
                (status.value, upstream_id, attempt_id),
            )

    def record_delivery_failure(self, attempt_id: str, status: DeliveryStatus) -> None:
        """Record local failure separately; cannot claim a delivered artifact."""
        if status not in {DeliveryStatus.DOWNLOAD_FAILED, DeliveryStatus.SAVE_FAILED}:
            raise ValueError("Only local delivery failures can be recorded")
        with closing(self._connect()) as db, db:
            result = db.execute(
                "UPDATE attempt SET delivery_status=? WHERE attempt_id=? AND upstream_status='succeeded' AND delivery_status IN ('not_available','pending',?)",
                (status.value, attempt_id, status.value),
            )
            if result.rowcount != 1:
                raise ConflictError(
                    "Upstream success or valid delivery state is required"
                )

    def reconcile_delivered_artifact(
        self,
        attempt_id: str,
        artifact_id: str,
        artifact_root: Path,
        budget: ResourceBudget,
    ) -> str:
        """Persist a locally observed fault; never infer remote status or restore a file."""
        from ecommerce_product_image_creator import artifact_reader, recovery_intent

        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT x.path,x.sha256,a.delivery_status,f.expected_sha256,f.expected_mime, "
                "(SELECT COALESCE(MAX(observation_id),0) FROM artifact_integrity_observation WHERE artifact_id=x.artifact_id) "
                "FROM artifact x JOIN attempt a ON a.attempt_id=x.attempt_id "
                "JOIN first_artifact_anchor f ON f.artifact_id=x.artifact_id AND f.attempt_id=x.attempt_id "
                "WHERE x.artifact_id=? AND x.attempt_id=? AND f.target_path=x.path",
                (artifact_id, attempt_id),
            ).fetchone()
        if (
            not row
            or row[1] != row[3]
            or row[2] not in ("delivered", "missing_or_corrupt")
        ):
            raise ConflictError("artifact_identity_unverified")
        path = Path(row[0])
        if (
            not artifact_root.is_absolute()
            or not path.is_absolute()
            or not path.is_relative_to(artifact_root)
            or ".." in path.parts
        ):
            raise ConflictError("artifact_path_unverified")
        with recovery_intent._pin_directories(path.parent):
            initial = artifact_reader._file_state(
                artifact_root, path, row[1], budget, row[4]
            )["state"]
            if initial == "unsafe_path":
                raise ConflictError("artifact_file_unverified")
            pin_unavailable = False
            if initial != "missing":
                try:
                    with recovery_intent._pin_file(path):
                        pass
                except ValueError as exc:
                    if str(exc) != "stage_file_unavailable":
                        raise
                    pin_unavailable = True
            _integrity_hook("observed")
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                pinned = ExitStack()
                try:
                    current = db.execute(
                        "SELECT x.path,x.sha256,a.delivery_status,f.expected_sha256,f.expected_mime, "
                        "(SELECT COALESCE(MAX(observation_id),0) FROM artifact_integrity_observation WHERE artifact_id=x.artifact_id) "
                        "FROM artifact x JOIN attempt a ON a.attempt_id=x.attempt_id "
                        "JOIN first_artifact_anchor f ON f.artifact_id=x.artifact_id AND f.attempt_id=x.attempt_id "
                        "WHERE x.artifact_id=? AND x.attempt_id=? AND f.target_path=x.path",
                        (artifact_id, attempt_id),
                    ).fetchone()
                    if (
                        current is None
                        or current[:2] != row[:2]
                        or current[3:5] != row[3:5]
                        or current[2] not in ("delivered", "missing_or_corrupt")
                    ):
                        raise ConflictError("artifact_changed")
                    if pin_unavailable:
                        if current[5] != row[5]:
                            raise ConflictError("artifact_observation_stale")
                        try:
                            pinned.enter_context(recovery_intent._pin_file(path))
                        except ValueError as exc:
                            if str(exc) != "stage_file_unavailable":
                                raise
                        else:
                            raise ConflictError("artifact_observation_stale")
                    elif initial != "missing":
                        pinned.enter_context(recovery_intent._pin_file(path))
                    state = artifact_reader._file_state(
                        artifact_root, path, row[1], budget, row[4]
                    )["state"]
                    if state != initial:
                        raise ConflictError("artifact_observation_stale")
                    if pin_unavailable:
                        state = "file_pin_unavailable"
                    if state == "verified":
                        info = path.stat()
                        identity = (str(info.st_dev), str(info.st_ino))
                        identities = db.execute(
                            "SELECT s.file_device,s.file_inode FROM first_artifact_stage s JOIN first_artifact_anchor f ON f.delivery_id=s.delivery_id WHERE f.artifact_id=? UNION SELECT file_device,file_inode FROM artifact_recovery_reconciliation WHERE artifact_id=?",
                            (artifact_id, artifact_id),
                        ).fetchall()
                        if identity in identities:
                            db.rollback()
                            return "verified"
                        state = "identity_mismatch"
                    if state not in (
                        "missing",
                        "digest_mismatch",
                        "invalid_or_unreadable",
                        "identity_mismatch",
                        "file_pin_unavailable",
                    ):
                        raise ConflictError("artifact_file_unverified")
                    previous = db.execute(
                        "SELECT state,reason FROM artifact_integrity_observation WHERE artifact_id=? ORDER BY observation_id DESC LIMIT 1",
                        (artifact_id,),
                    ).fetchone()
                    if previous != ("missing_or_corrupt", state):
                        db.execute(
                            "INSERT INTO artifact_integrity_observation(artifact_id,attempt_id,expected_sha256,state,reason,previous_status,observed_at) VALUES(?,?,?,?,?,?,?)",
                            (
                                artifact_id,
                                attempt_id,
                                row[1],
                                "missing_or_corrupt",
                                state,
                                current[2],
                                datetime.now(UTC).timestamp(),
                            ),
                        )
                    db.execute(
                        "UPDATE attempt SET delivery_status='missing_or_corrupt' WHERE attempt_id=? AND delivery_status IN ('delivered','missing_or_corrupt')",
                        (attempt_id,),
                    )
                    _integrity_hook("before_commit")
                    if (
                        initial == "missing"
                        and artifact_reader._file_state(
                            artifact_root, path, row[1], budget, row[4]
                        )["state"]
                        != "missing"
                    ):
                        raise ConflictError("artifact_observation_stale")
                    db.commit()
                    _integrity_hook("after_commit")
                    return "missing_or_corrupt"
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    pinned.close()

    def consume_local_marker(self, marker_id: str) -> bool:
        """Atomic local marker only; never a claim that authorization was verified."""
        if not marker_id:
            raise ValueError("Marker ID is required")
        with closing(self._connect()) as db, db:
            result = db.execute(
                "INSERT OR IGNORE INTO local_marker VALUES(?)", (marker_id,)
            )
            return result.rowcount == 1

    def increment_attempt_counter(
        self, operation_id: str, model: str, endpoint: str
    ) -> int:
        """Persist a local counter; this does not authorize or send any request."""
        if not all((operation_id, model, endpoint)):
            raise ValueError("Counter identity required")
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT count FROM attempt_counter WHERE operation_id=? AND model=? AND endpoint=?",
                    (operation_id, model, endpoint),
                ).fetchone()
                count = 1 if row is None else row[0] + 1
                if count > 3:
                    raise ConflictError("Per-combination attempt count exhausted")
                db.execute(
                    "INSERT INTO attempt_counter VALUES(?,?,?,?) ON CONFLICT(operation_id,model,endpoint) DO UPDATE SET count=excluded.count",
                    (operation_id, model, endpoint, count),
                )
                return count
        except sqlite3.IntegrityError:
            raise ConflictError("Unknown operation") from None

    def record_retry_wait(
        self,
        operation_id: str,
        attempt_id: str,
        started: float,
        retry_after: str | None,
        http_status: int,
    ) -> None:
        """Persist timing only; elapsed time never authorizes another attempt."""
        from ecommerce_product_image_creator.faults import parse_retry_after

        if http_status not in (0, 429) and not 500 <= http_status <= 599:
            raise ConflictError("Unsupported wait reason")
        if not math.isfinite(started):
            raise ConflictError("Invalid wait time")
        try:
            now = datetime.fromtimestamp(started, UTC)
            delay = parse_retry_after(retry_after, now) if http_status == 429 else 5
        except (ValueError, OverflowError, OSError):
            raise ConflictError("Invalid wait time") from None
        try:
            deadline = started + delay if delay is not None else float("nan")
        except OverflowError:
            deadline = float("nan")
        if not math.isfinite(deadline):
            raise ConflictError("Valid Retry-After is required")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT a.upstream_status FROM operation o JOIN attempt a ON a.attempt_id=o.attempt_id WHERE o.operation_id=? AND a.attempt_id=?",
                (operation_id, attempt_id),
            ).fetchone()
            wait_table = "retry_wait"
            if row is None:
                row = db.execute(
                    "SELECT a.upstream_status FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id "
                    "JOIN operation o ON o.operation_id=c.operation_id "
                    "WHERE c.operation_id=? AND c.attempt_id=? AND c.sequence IN (2,3,4,5) "
                    "AND a.task_id=o.task_id AND a.revision=o.revision AND (c.execution_digest=o.digest OR EXISTS (SELECT 1 FROM candidate_attempt ca JOIN execution_candidate ec ON ec.candidate_id=ca.candidate_id WHERE ca.attempt_id=c.attempt_id AND ca.root_operation_id=o.operation_id AND ec.execution_digest=c.execution_digest AND ec.root_operation_id=o.operation_id))",
                    (operation_id, attempt_id),
                ).fetchone()
                wait_table = "retry_wait_attempt"
            if http_status == 0:
                from ecommerce_product_image_creator.send_evidence import (
                    verified_pre_send,
                    verified_recoverability,
                )

                root = db.execute(
                    "SELECT o.task_id,o.revision,c.execution_digest FROM operation o JOIN operation_attempt c ON c.operation_id=o.operation_id WHERE o.operation_id=? AND c.attempt_id=?",
                    (operation_id, attempt_id),
                ).fetchone()
                if (
                    not root
                    or not verified_pre_send(
                        db, operation_id, attempt_id, root[0], root[1], root[2]
                    )
                    or verified_recoverability(
                        db, operation_id, attempt_id, root[0], root[1], root[2]
                    )
                    != "transient_local"
                ):
                    raise ConflictError("Pre-send evidence is unavailable")
            elif row != ("failed",):
                raise ConflictError("A known failed original attempt is required")
            prior = db.execute(
                "SELECT attempt_id,started,deadline FROM retry_wait WHERE operation_id=?"
                if wait_table == "retry_wait"
                else "SELECT attempt_id,started,deadline FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
                (operation_id,)
                if wait_table == "retry_wait"
                else (operation_id, attempt_id),
            ).fetchone()
            values = (attempt_id, started, deadline)
            if prior is not None:
                if prior != values:
                    raise ConflictError("Conflicting wait record")
                return
            db.execute(
                "INSERT INTO retry_wait VALUES(?,?,?,?,?)"
                if wait_table == "retry_wait"
                else "INSERT INTO retry_wait_attempt VALUES(?,?,?,?,?)",
                (operation_id, attempt_id, started, deadline, started),
            )

    def retry_wait_status(
        self, operation_id: str, now: float, *, attempt_id: str | None = None
    ) -> str:
        """Check durable time against a high-water mark; this never grants permission."""
        if not math.isfinite(now):
            return "clock_invalid"
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT started,deadline,observed FROM retry_wait WHERE operation_id=? AND (? IS NULL OR attempt_id=?)",
                (operation_id, attempt_id, attempt_id),
            ).fetchone()
            wait_table = "retry_wait"
            if row is None and attempt_id is not None:
                row = db.execute(
                    "SELECT started,deadline,observed FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
                    (operation_id, attempt_id),
                ).fetchone()
                wait_table = "retry_wait_attempt"
            if row is None:
                return "unknown"
            started, deadline, observed = row
            if now < started or now < observed:
                return "clock_invalid"
            db.execute(
                "UPDATE retry_wait SET observed=? WHERE operation_id=?"
                if wait_table == "retry_wait"
                else "UPDATE retry_wait_attempt SET observed=? WHERE operation_id=? AND attempt_id=?",
                (now, operation_id)
                if wait_table == "retry_wait"
                else (now, operation_id, attempt_id),
            )
            return "elapsed_recheck_required" if now >= deadline else "waiting"


class StoredAttempt:
    def __init__(
        self,
        upstream_status: str,
        upstream_id: str | None,
        delivery_status: str,
        attempt_count: int,
    ) -> None:
        self.upstream_status = upstream_status
        self.upstream_id = upstream_id
        self.delivery_status = delivery_status
        self.attempt_count = attempt_count
