"""Offline recovery intent for existing historical artifacts; no publication."""

import os
import secrets
import sqlite3
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from . import artifact_locator, original_query, preparation, recovery
from .storage import FileSafetyError, ResourceBudget, Store, read_reference


def register_pending(
    store: Store,
    operation_id: str,
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
) -> str:
    """Register one audited intent, revalidating source within its write transaction."""
    if not operation_id:
        raise ValueError("operation_id_required")
    with closing(store._connect()) as lookup:
        target = lookup.execute(
            "SELECT path FROM artifact WHERE artifact_id=? AND attempt_id=?",
            (artifact_id, attempt_id),
        ).fetchone()
    if target is None:
        raise ValueError("historical_artifact_unverified")
    target_path = Path(target[0])
    if (
        not artifact_root.is_absolute()
        or not target_path.is_absolute()
        or ".." in target_path.parts
        or not target_path.is_relative_to(artifact_root)
        or str(artifact_root).startswith("\\\\")
        or str(target_path).startswith("\\\\")
    ):
        raise ValueError("target_path_unverified")
    try:
        current = artifact_root
        for part in (None, *target_path.relative_to(artifact_root).parts[:-1]):
            if part is not None:
                current = current / part
            info = current.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise ValueError("target_path_unverified")
        if not target_path.resolve(strict=False).is_relative_to(
            artifact_root.resolve(strict=True)
        ):
            raise ValueError("target_path_unverified")
    except OSError:
        raise ValueError("target_path_unverified") from None
    try:
        target_path.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise ValueError("target_path_unverified") from None
    else:
        raise ValueError("target_not_missing")
    with closing(store._connect()) as monitor:
        version = monitor.execute("PRAGMA data_version").fetchone()[0]
        acquired = artifact_locator.acquire_original_bytes(
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
        if (
            not acquired.expected_digest_verified
            or not artifact_locator.verify_acquired_bytes(acquired)
        ):
            raise ValueError("historical_digest_unverified")
        with closing(store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if monitor.execute("PRAGMA data_version").fetchone()[0] != version:
                    raise ValueError("source_changed")
                _check_source(db, store, acquired)
                verified = acquired
                artifact = db.execute(
                    "SELECT path,sha256 FROM artifact WHERE artifact_id=? AND attempt_id=?",
                    (artifact_id, attempt_id),
                ).fetchone()
                if artifact is None or artifact[1] != verified.sha256:
                    raise ValueError("historical_artifact_unverified")
                try:
                    Path(artifact[0]).lstat()
                except FileNotFoundError:
                    pass
                except OSError:
                    raise ValueError("target_path_unverified") from None
                else:
                    raise ValueError("target_not_missing")
                values = (
                    artifact_id,
                    root,
                    candidate_id,
                    task_id,
                    revision,
                    stage,
                    attempt_id,
                    digest,
                    observation_id,
                    artifact[1],
                    artifact[0],
                    verified.source,
                    verified.evidence_id,
                )
                previous = db.execute(
                    "SELECT recovery_id,artifact_id,root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,observation_id,expected_sha256,target_path,source,evidence_id FROM artifact_recovery_intent WHERE artifact_id=? OR recovery_id=?",
                    (artifact_id, operation_id),
                ).fetchall()
                if previous:
                    if len(previous) != 1 or previous[0][1:] != values:
                        raise ValueError("recovery_identity_conflict")
                    db.rollback()
                    return str(previous[0][0])
                now = time.time()
                db.execute(
                    "INSERT INTO artifact_recovery_intent VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (operation_id, *values, now, now),
                )
                _stage_hook("pending_before_commit")
                db.commit()
                return operation_id
            except BaseException:
                db.rollback()
                raise


def _check_source(
    db: sqlite3.Connection,
    store: Store,
    acquired: artifact_locator.VerifiedArtifactBytes,
) -> None:
    root, candidate_id, task_id, revision, stage = (
        acquired.root,
        acquired.candidate_id,
        acquired.task_id,
        acquired.revision,
        acquired.stage,
    )
    attempt_id, digest, observation_id, artifact_id = (
        acquired.attempt_id,
        acquired.execution_digest,
        acquired.observation_id,
        acquired.artifact_id,
    )
    locator = (
        artifact_locator.read_locator(store, acquired.evidence_id)
        if acquired.source == "fixture_locator"
        else None
    )
    identity = original_query._identity(
        store, root, candidate_id, task_id, revision, attempt_id, digest
    )
    if (
        identity is None
        or identity.stage != stage
        or not original_query._chain_is_valid(db, identity)
    ):
        raise ValueError("source_changed")
    observation = db.execute(
        "SELECT observation_id,root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,provider,model,endpoint,grouping,upstream_id,source,response_digest,remote_state,reason,previous_id FROM recovery_observation WHERE observation_id=? AND attempt_id=?",
        (observation_id, attempt_id),
    ).fetchone()
    if (
        observation is None
        or observation[15] != "succeeded"
        or not recovery._verify_observation_source(identity, observation)
    ):
        raise ValueError("source_changed")
    if db.execute(
        "SELECT 1 FROM recovery_observation WHERE attempt_id=? AND remote_state IN ('conflict','failed')",
        (attempt_id,),
    ).fetchone():
        raise ValueError("source_changed")
    if acquired.source == "mock_frozen_form_input":
        review = db.execute(
            "SELECT r.task_id,r.form_revision,r.artifact_id,r.artifact_sha256,r.source,d.decision,d.evidence_reference,d.invalidation_reason,d.expires_at,EXISTS(SELECT 1 FROM form_review_invalidation i WHERE i.review_id=r.review_id) FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id WHERE r.review_id=?",
            (acquired.evidence_id,),
        ).fetchone()
        if (
            review is None
            or review[:4] != (task_id, revision, artifact_id, acquired.sha256)
            or review[5] != "accept"
            or review[7] is not None
            or (review[8] is not None and review[8] <= time.time())
            or review[9]
            or not preparation._review_is_trusted(
                review[4],
                task_id,
                acquired.evidence_id,
                acquired.sha256,
                review[6],
            )
        ):
            raise ValueError("source_changed")
        preparation._reject_review_conflict(
            db, task_id, revision, artifact_id, acquired.sha256
        )
    elif acquired.source == "fixture_locator":
        if (
            locator is None
            or locator.expires <= time.time()
            or not artifact_locator._verify_fixture_access(locator)
            or not artifact_locator._checked(db, store, locator)
        ):
            raise ValueError("source_changed")
        variants = db.execute(
            "SELECT DISTINCT artifact_identity,locator_type,scope,handle FROM artifact_locator_evidence WHERE attempt_id=?",
            (attempt_id,),
        ).fetchall()
        if variants != [
            (
                locator.artifact_identity,
                locator.locator_type,
                locator.scope,
                locator.handle,
            )
        ]:
            raise ValueError("source_changed")
    else:
        raise ValueError("source_changed")


@contextmanager
def _pin_directories(directory: Path) -> Iterator[None]:
    """Keep Windows directory ancestors open without delete sharing during staging."""
    if sys.platform != "win32" or not directory.is_absolute():
        raise ValueError("stage_platform_unverified")
    import ctypes
    from ctypes import wintypes

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    drive_type = kernel.GetDriveTypeW
    drive_type.argtypes = [wintypes.LPCWSTR]
    drive_type.restype = wintypes.UINT
    volume = kernel.GetVolumeInformationW
    volume.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    volume.restype = wintypes.BOOL
    filesystem = ctypes.create_unicode_buffer(64)
    if (
        drive_type(directory.anchor) != 3
        or not volume(
            directory.anchor, None, 0, None, None, None, filesystem, len(filesystem)
        )
        or filesystem.value.upper() != "NTFS"
    ):
        raise ValueError("stage_filesystem_unverified")
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    info.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handles: list[int] = []
    try:
        current = Path(directory.anchor)
        for part in (None, *directory.parts[1:]):
            if part is not None:
                current = current / part
            handle = create(str(current), 0x81, 0x3, None, 3, 0x02200000, None)
            if handle is None or handle == ctypes.c_void_p(-1).value:
                raise ValueError("stage_directory_unverified")
            handles.append(handle)
            attributes = AttributeTag()
            if (
                not info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes))
                or attributes.attributes & 0x400
                or not attributes.attributes & 0x10
            ):
                raise ValueError("stage_directory_unverified")
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


def _stage_hook(phase: str) -> None:
    """Internal fault-injection point, never selected through MCP."""


def stage_pending(
    store: Store, operation_id: str, artifact_root: Path, budget: ResourceBudget
) -> str:
    """Stage validated bytes for an existing recovery; never publish an Artifact."""
    with closing(store._connect()) as monitor:
        version = monitor.execute("PRAGMA data_version").fetchone()[0]
        row = monitor.execute(
            "SELECT artifact_id,root_operation_id,candidate_id,task_id,revision,stage,attempt_id,execution_digest,observation_id,expected_sha256,target_path,source,evidence_id,state FROM artifact_recovery_intent WHERE recovery_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("pending_unavailable")
        (
            artifact_id,
            root,
            candidate,
            task,
            revision,
            stage,
            attempt,
            digest,
            observation,
            expected,
            target,
            source,
            evidence,
            state,
        ) = row
        target_path = Path(target)
        if (
            not artifact_root.is_absolute()
            or not target_path.is_absolute()
            or not target_path.is_relative_to(artifact_root)
            or ".." in target_path.parts
            or str(artifact_root).startswith("\\\\")
        ):
            raise ValueError("target_path_unverified")
        try:
            current = artifact_root
            for part in (None, *target_path.relative_to(artifact_root).parts[:-1]):
                if part is not None:
                    current = current / part
                info = current.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    raise ValueError("target_path_unverified")
            if not target_path.resolve(strict=False).is_relative_to(
                artifact_root.resolve(strict=True)
            ):
                raise ValueError("target_path_unverified")
        except OSError:
            raise ValueError("target_path_unverified") from None
        with _pin_directories(target_path.parent):
            acquired = artifact_locator.acquire_original_bytes(
                store,
                root,
                candidate,
                task,
                revision,
                stage,
                attempt,
                digest,
                observation,
                artifact_id=artifact_id,
                artifact_root=artifact_root,
                budget=budget,
            )
            if (
                not artifact_locator.verify_acquired_bytes(acquired)
                or not acquired.expected_digest_verified
                or (acquired.sha256, acquired.source, acquired.evidence_id)
                != (expected, source, evidence)
            ):
                raise ValueError("source_changed")
            if state == "staged":
                prior = monitor.execute(
                    "SELECT relative_path,sha256,mime,byte_count,width,height,file_device,file_inode,file_mtime_ns FROM artifact_recovery_stage WHERE recovery_id=?",
                    (operation_id,),
                ).fetchone()
                if prior is None:
                    raise ValueError("stage_record_missing")
                staged = artifact_root / prior[0]
                if (
                    not staged.is_relative_to(artifact_root)
                    or ".." in Path(prior[0]).parts
                ):
                    raise ValueError("stage_path_unverified")
                image = read_reference(artifact_root, staged, prior[2], budget)
                info = staged.lstat()
                if (
                    image.sha256,
                    len(image.data),
                    str(info.st_dev),
                    str(info.st_ino),
                    info.st_mtime_ns,
                ) != (prior[1], prior[3], prior[6], prior[7], prior[8]):
                    raise ValueError("stage_file_changed")
                measured = artifact_locator.validate_image_bytes(
                    image.data, image.mime, budget
                )
                if (measured.width, measured.height) != (prior[4], prior[5]):
                    raise ValueError("stage_file_changed")
                with closing(store._connect()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        if (
                            monitor.execute("PRAGMA data_version").fetchone()[0]
                            != version
                        ):
                            raise ValueError("source_changed")
                        _check_source(db, store, acquired)
                        actual = staged.lstat()
                        if (
                            str(actual.st_dev),
                            str(actual.st_ino),
                            actual.st_size,
                            actual.st_mtime_ns,
                        ) != (prior[6], prior[7], prior[3], prior[8]):
                            raise ValueError("stage_file_changed")
                    finally:
                        db.rollback()
                return "staged"
            if state != "pending":
                raise ValueError("pending_unavailable")
            stage_path = target_path.parent / (
                ".recovery-" + secrets.token_hex(24) + ".tmp"
            )
            try:
                fd = os.open(
                    stage_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except OSError:
                raise ValueError("stage_create_failed") from None
            try:
                with os.fdopen(fd, "wb") as handle:
                    midpoint = len(acquired.data) // 2
                    handle.write(acquired.data[:midpoint])
                    _stage_hook("partial_write")
                    handle.write(acquired.data[midpoint:])
                    handle.flush()
                    os.fsync(handle.fileno())
                _stage_hook("written")
                image = read_reference(artifact_root, stage_path, acquired.mime, budget)
                measured = artifact_locator.validate_image_bytes(
                    image.data, image.mime, budget
                )
                info = stage_path.lstat()
                if (
                    image.sha256 != expected
                    or len(image.data) != acquired.size
                    or (measured.width, measured.height)
                    != (acquired.width, acquired.height)
                ):
                    raise ValueError("stage_digest_mismatch")
                _stage_hook("verified")
                with closing(store._connect()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        if (
                            monitor.execute("PRAGMA data_version").fetchone()[0]
                            != version
                        ):
                            raise ValueError("source_changed")
                        _check_source(db, store, acquired)
                        current = db.execute(
                            "SELECT artifact_id,attempt_id,execution_digest,expected_sha256,target_path,source,evidence_id,state FROM artifact_recovery_intent WHERE recovery_id=?",
                            (operation_id,),
                        ).fetchone()
                        if current != (
                            artifact_id,
                            attempt,
                            digest,
                            expected,
                            target,
                            source,
                            evidence,
                            "pending",
                        ):
                            raise ValueError("recovery_identity_conflict")
                        actual = stage_path.lstat()
                        if (
                            actual.st_dev,
                            actual.st_ino,
                            actual.st_size,
                            actual.st_mtime_ns,
                        ) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                            raise ValueError("stage_file_changed")
                        try:
                            target_path.lstat()
                        except FileNotFoundError:
                            pass
                        else:
                            raise ValueError("target_not_missing")
                        db.execute(
                            "INSERT INTO artifact_recovery_stage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                operation_id,
                                str(stage_path.relative_to(artifact_root)),
                                image.sha256,
                                image.mime,
                                len(image.data),
                                measured.width,
                                measured.height,
                                str(info.st_dev),
                                str(info.st_ino),
                                info.st_mtime_ns,
                                time.time(),
                            ),
                        )
                        db.execute(
                            "UPDATE artifact_recovery_intent SET state='staged',updated_at=? WHERE recovery_id=? AND state='pending'",
                            (time.time(), operation_id),
                        )
                        _stage_hook("before_commit")
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
                _stage_hook("after_commit")
                return "staged"
            except (OSError, MemoryError):
                raise ValueError("stage_io_failed") from None


@contextmanager
def _pin_file(path: Path) -> Iterator[None]:
    """Hold a Windows read handle that denies write and delete sharing."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ValueError("stage_file_unavailable")
    try:
        yield
    finally:
        close(handle)


def _publication_hook(phase: str) -> None:
    """Isolated fault injection; never exposed through MCP."""


def publish_staged(
    store: Store, operation_id: str, artifact_root: Path, budget: ResourceBudget
) -> str:
    """Link a verified staged inode into its historical target without replacement."""
    with closing(store._connect()) as monitor:
        version = monitor.execute("PRAGMA data_version").fetchone()[0]
        row = monitor.execute(
            "SELECT i.artifact_id,i.root_operation_id,i.candidate_id,i.task_id,i.revision,i.stage,i.attempt_id,i.execution_digest,i.observation_id,i.expected_sha256,i.target_path,i.source,i.evidence_id,i.state,s.relative_path,s.sha256,s.mime,s.byte_count,s.width,s.height,s.file_device,s.file_inode,s.file_mtime_ns FROM artifact_recovery_intent i JOIN artifact_recovery_stage s ON s.recovery_id=i.recovery_id WHERE i.recovery_id=?",
            (operation_id,),
        ).fetchone()
        if row is None or row[13] != "staged":
            raise ValueError("staged_unavailable")
        (
            artifact_id,
            root,
            candidate,
            task,
            revision,
            stage,
            attempt,
            digest,
            observation,
            expected,
            target,
            source,
            evidence,
        ) = row[:13]
        target_path = Path(target)
        relative_stage = Path(row[14])
        staged = artifact_root / relative_stage
        if (
            not artifact_root.is_absolute()
            or not target_path.is_absolute()
            or not target_path.is_relative_to(artifact_root)
            or relative_stage.is_absolute()
            or ".." in relative_stage.parts
            or staged.parent != target_path.parent
            or ".." in target_path.parts
            or str(artifact_root).startswith("\\\\")
        ):
            raise ValueError("target_path_unverified")
        with _pin_directories(target_path.parent):
            if source == "mock_frozen_form_input":
                with closing(store._connect()) as source_db:
                    source_assessment = artifact_locator._mock_copy(
                        source_db,
                        store,
                        task,
                        revision,
                        attempt,
                        artifact_id,
                        observation,
                        budget,
                    )
                if (
                    source_assessment is None
                    or source_assessment.state != "recover_from_local_bytes"
                    or source_assessment.source_review_id != evidence
                    or source_assessment.artifact_sha256 != expected
                ):
                    raise ValueError("source_changed")
            original = monitor.execute(
                "SELECT path,sha256 FROM artifact WHERE artifact_id=? AND attempt_id=?",
                (artifact_id, attempt),
            ).fetchone()
            if original != (target, expected) or row[15] != expected:
                raise ValueError("historical_artifact_unverified")
            image = read_reference(artifact_root, staged, row[16], budget)
            measured = artifact_locator.validate_image_bytes(
                image.data, image.mime, budget
            )
            initial = staged.lstat()
            if (
                image.sha256,
                len(image.data),
                measured.width,
                measured.height,
                str(initial.st_dev),
                str(initial.st_ino),
                initial.st_mtime_ns,
            ) != (expected, row[17], row[18], row[19], row[20], row[21], row[22]):
                raise ValueError("stage_file_changed")
            # A read-only attribute discourages accidental writes via either link;
            # it is not an ACL or protection from a same-account adversary.
            try:
                os.chmod(staged, stat.S_IREAD)
            except OSError:
                raise ValueError("stage_readonly_unavailable") from None
            with _pin_file(staged):
                again = read_reference(artifact_root, staged, row[16], budget)
                info = staged.lstat()
                if again.sha256 != expected or (
                    str(info.st_dev),
                    str(info.st_ino),
                    info.st_size,
                    info.st_mtime_ns,
                ) != (row[20], row[21], row[17], row[22]):
                    raise ValueError("stage_file_changed")
                # This internal identity is reconstructed only after byte and inode
                # verification; _check_source independently verifies all provenance.
                verified = artifact_locator.VerifiedArtifactBytes(
                    task,
                    revision,
                    stage,
                    root,
                    candidate,
                    attempt,
                    digest,
                    artifact_id,
                    observation,
                    source,
                    evidence,
                    expected,
                    image.mime,
                    measured.width,
                    measured.height,
                    len(image.data),
                    True,
                    image.data,
                )
                _publication_hook("before_link")
                linked_now = False
                with closing(store._connect()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        if (
                            monitor.execute("PRAGMA data_version").fetchone()[0]
                            != version
                        ):
                            raise ValueError("source_changed")
                        _check_source(db, store, verified)
                        current = db.execute(
                            "SELECT path,sha256 FROM artifact WHERE artifact_id=? AND attempt_id=?",
                            (artifact_id, attempt),
                        ).fetchone()
                        if current != (target, expected):
                            raise ValueError("historical_artifact_unverified")
                        try:
                            target_path.lstat()
                        except FileNotFoundError:
                            try:
                                os.link(staged, target_path)
                                linked_now = True
                            except FileExistsError:
                                raise ValueError("target_conflict") from None
                            except OSError:
                                raise ValueError("publication_failed") from None
                        db.rollback()
                    except BaseException:
                        db.rollback()
                        raise
                _publication_hook("after_link")
                try:
                    target_image = read_reference(
                        artifact_root, target_path, image.mime, budget
                    )
                    target_info = target_path.lstat()
                except OSError:
                    raise ValueError("target_unverified") from None
                except ValueError:
                    raise ValueError("target_conflict") from None
                if target_image.sha256 != expected:
                    raise ValueError("target_conflict")
                if (str(target_info.st_dev), str(target_info.st_ino)) != (
                    row[20],
                    row[21],
                ):
                    return "pending_reconciliation"
                with closing(store._connect()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        if (
                            monitor.execute("PRAGMA data_version").fetchone()[0]
                            != version
                        ):
                            raise ValueError("source_changed_after_publication")
                        _check_source(db, store, verified)
                        again_info = target_path.lstat()
                        if (
                            str(again_info.st_dev),
                            str(again_info.st_ino),
                            again_info.st_size,
                            again_info.st_mtime_ns,
                        ) != (row[20], row[21], row[17], row[22]):
                            raise ValueError("target_unverified")
                        existing = db.execute(
                            "SELECT target_path,sha256,file_device,file_inode,file_size,file_mtime_ns FROM artifact_recovery_publication WHERE recovery_id=?",
                            (operation_id,),
                        ).fetchone()
                        values = (target, expected, row[20], row[21], row[17], row[22])
                        if existing is None and not linked_now:
                            db.rollback()
                            return "pending_reconciliation"
                        if existing is None:
                            db.execute(
                                "INSERT INTO artifact_recovery_publication VALUES (?,?,?,?,?,?,?,?)",
                                (operation_id, *values, time.time()),
                            )
                        elif existing != values:
                            raise ValueError("publication_conflict")
                        _publication_hook("before_commit")
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
                _publication_hook("after_commit")
                return "published"


def _verify_reconciliation_policy(
    evidence: artifact_locator.LocatorEvidence,
    staged_at: float,
    publication_at: float | None,
) -> bool:
    """No production authority for historical remote evidence is established."""
    return False


def _reconciliation_hook(phase: str) -> None:
    """Isolated fault injection, never exposed through MCP."""


def reconcile_published(
    store: Store, operation_id: str, artifact_root: Path, budget: ResourceBudget
) -> str:
    """Recheck a historical artifact and record an offline reconciliation.

    The result is historical evidence, not an assertion of future file integrity.
    """
    from .artifact_reader import read_artifact
    from .storage import _record_verified_artifact_recovery

    with closing(store._connect()) as monitor:
        version = monitor.execute("PRAGMA data_version").fetchone()[0]
        row = monitor.execute(
            "SELECT i.artifact_id,i.root_operation_id,i.candidate_id,i.task_id,i.revision,i.stage,i.attempt_id,i.execution_digest,i.observation_id,i.expected_sha256,i.target_path,i.source,i.evidence_id,i.state,s.relative_path,s.sha256,s.mime,s.byte_count,s.width,s.height,s.file_device,s.file_inode,s.file_mtime_ns,s.created_at FROM artifact_recovery_intent i JOIN artifact_recovery_stage s ON s.recovery_id=i.recovery_id WHERE i.recovery_id=?",
            (operation_id,),
        ).fetchone()
        if row is None or row[13] != "staged":
            raise ValueError("staged_unavailable")
        (
            artifact_id,
            root,
            candidate,
            task,
            revision,
            stage,
            attempt,
            digest,
            observation,
            expected,
            target,
            source,
            evidence,
        ) = row[:13]
        target_path = Path(target)
        relative_stage = Path(row[14])
        staged = artifact_root / relative_stage
        if (
            not artifact_root.is_absolute()
            or not target_path.is_absolute()
            or not target_path.is_relative_to(artifact_root)
            or relative_stage.is_absolute()
            or ".." in relative_stage.parts
            or staged.parent != target_path.parent
            or ".." in target_path.parts
            or str(artifact_root).startswith("\\\\")
        ):
            raise ValueError("target_path_unverified")
        with _pin_directories(target_path.parent):
            publication_at: float | None = None
            if source == "mock_frozen_form_input":
                with closing(store._connect()) as source_db:
                    source_check = artifact_locator._mock_copy(
                        source_db,
                        store,
                        task,
                        revision,
                        attempt,
                        artifact_id,
                        observation,
                        budget,
                    )
                if (
                    source_check is None
                    or source_check.state != "recover_from_local_bytes"
                    or source_check.source_review_id != evidence
                    or source_check.artifact_sha256 != expected
                ):
                    raise ValueError("source_unverified")
            elif source == "fixture_locator":
                locator = artifact_locator.read_locator(store, evidence)
                recorded_time = monitor.execute(
                    "SELECT published_at FROM artifact_recovery_publication WHERE recovery_id=?",
                    (operation_id,),
                ).fetchone()
                publication_at = recorded_time[0] if recorded_time else None
                if (
                    locator is None
                    or not _verify_reconciliation_policy(
                        locator, row[23], publication_at
                    )
                    or not (locator.created <= row[23] <= locator.expires)
                ):
                    raise ValueError("current_policy_unverified")
            else:
                raise ValueError("source_unverified")
            if (
                monitor.execute(
                    "SELECT path,sha256 FROM artifact WHERE artifact_id=? AND attempt_id=?",
                    (artifact_id, attempt),
                ).fetchone()
                != (target, expected)
                or row[15] != expected
            ):
                raise ValueError("historical_artifact_unverified")
            try:
                target_path.lstat()
            except FileNotFoundError:
                raise ValueError("target_missing") from None
            except OSError:
                raise ValueError("target_unverified") from None
            with _pin_file(staged), _pin_file(target_path):
                try:
                    stage_image = read_reference(artifact_root, staged, row[16], budget)
                    target_image = read_reference(
                        artifact_root, target_path, row[16], budget
                    )
                except FileSafetyError:
                    raise ValueError("current_file_invalid") from None
                info = staged.lstat()
                final = target_path.lstat()
                identity = (
                    str(final.st_dev),
                    str(final.st_ino),
                    final.st_size,
                    final.st_mtime_ns,
                )
                expected_identity = (row[20], row[21], row[17], row[22])
                if (
                    stage_image.sha256 != expected
                    or target_image.sha256 != expected
                    or (
                        str(info.st_dev),
                        str(info.st_ino),
                        info.st_size,
                        info.st_mtime_ns,
                    )
                    != expected_identity
                    or identity != expected_identity
                ):
                    raise ValueError("file_identity_or_digest_conflict")
                measured = artifact_locator.validate_image_bytes(
                    target_image.data, target_image.mime, budget
                )
                if (measured.width, measured.height) != (row[18], row[19]):
                    raise ValueError("file_integrity_conflict")
                verified = artifact_locator.VerifiedArtifactBytes(
                    task,
                    revision,
                    stage,
                    root,
                    candidate,
                    attempt,
                    digest,
                    artifact_id,
                    observation,
                    source,
                    evidence,
                    expected,
                    target_image.mime,
                    measured.width,
                    measured.height,
                    len(target_image.data),
                    True,
                    target_image.data,
                )
                pre_report = read_artifact(
                    store,
                    task,
                    stage,
                    revision,
                    attempt,
                    artifact_id,
                    artifact_root,
                    budget,
                )
                if pre_report["file"]["state"] != "verified":
                    raise ValueError("current_file_unverified")
                _reconciliation_hook("before_commit")
                with closing(store._connect()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        if (
                            monitor.execute("PRAGMA data_version").fetchone()[0]
                            != version
                        ):
                            raise ValueError("source_changed")
                        if source == "fixture_locator":
                            current = artifact_locator.read_locator(store, evidence)
                            if (
                                current is None
                                or not _verify_reconciliation_policy(
                                    current, row[23], publication_at
                                )
                                or not artifact_locator._checked(db, store, current)
                            ):
                                raise ValueError("current_policy_unverified")
                            variants = db.execute(
                                "SELECT DISTINCT artifact_identity,locator_type,scope,handle FROM artifact_locator_evidence WHERE attempt_id=?",
                                (attempt,),
                            ).fetchall()
                            if variants != [
                                (
                                    current.artifact_identity,
                                    current.locator_type,
                                    current.scope,
                                    current.handle,
                                )
                            ]:
                                raise ValueError("source_changed")
                        else:
                            _check_source(db, store, verified)
                        actual = target_path.lstat()
                        stage_actual = staged.lstat()
                        if (
                            str(actual.st_dev),
                            str(actual.st_ino),
                            actual.st_size,
                            actual.st_mtime_ns,
                        ) != expected_identity or (
                            str(stage_actual.st_dev),
                            str(stage_actual.st_ino),
                            stage_actual.st_size,
                            stage_actual.st_mtime_ns,
                        ) != expected_identity:
                            raise ValueError("file_identity_or_digest_conflict")
                        pub = db.execute(
                            "SELECT target_path,sha256,file_device,file_inode,file_size,file_mtime_ns FROM artifact_recovery_publication WHERE recovery_id=?",
                            (operation_id,),
                        ).fetchone()
                        values = (target, expected, row[20], row[21], row[17], row[22])
                        if pub is not None and pub != values:
                            raise ValueError("publication_conflict")
                        basis = "published" if pub else "observed_link"
                        if pub is None:
                            existing = db.execute(
                                "SELECT target_path,sha256,file_device,file_inode,file_size,file_mtime_ns FROM artifact_recovery_link_observation WHERE recovery_id=?",
                                (operation_id,),
                            ).fetchone()
                            if existing is None:
                                db.execute(
                                    "INSERT INTO artifact_recovery_link_observation VALUES (?,?,?,?,?,?,?,?,?)",
                                    (
                                        operation_id,
                                        *values,
                                        time.time(),
                                        "matching_ntfs_link_observed",
                                    ),
                                )
                            elif existing != values:
                                raise ValueError("publication_conflict")
                        record = (
                            artifact_id,
                            attempt,
                            candidate,
                            digest,
                            task,
                            revision,
                            stage,
                            expected,
                            target,
                            row[20],
                            row[21],
                            row[17],
                            row[22],
                            basis,
                        )
                        existing_reconciliation = db.execute(
                            "SELECT artifact_id,attempt_id,candidate_id,execution_digest,task_id,revision,stage,expected_sha256,target_path,file_device,file_inode,file_size,file_mtime_ns,basis FROM artifact_recovery_reconciliation WHERE recovery_id=?",
                            (operation_id,),
                        ).fetchone()
                        if existing_reconciliation is None:
                            db.execute(
                                "INSERT INTO artifact_recovery_reconciliation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (
                                    operation_id,
                                    *record,
                                    time.time(),
                                    "verified_artifact",
                                ),
                            )
                        elif existing_reconciliation != record:
                            raise ValueError("reconciliation_conflict")
                        _record_verified_artifact_recovery(
                            db,
                            artifact_id,
                            attempt,
                            expected,
                            "verified_historical_recovery",
                        )
                        _reconciliation_hook("during_transaction")
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
                _reconciliation_hook("after_commit")
                report = read_artifact(
                    store,
                    task,
                    stage,
                    revision,
                    attempt,
                    artifact_id,
                    artifact_root,
                    budget,
                )
                if report["file"]["state"] != "verified":
                    raise ValueError("current_file_unverified")
                return "reconciled"
