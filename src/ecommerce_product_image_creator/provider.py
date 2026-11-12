"""Offline GPT Image request mapping and response parsing; no network or credentials."""

import hashlib
import math
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, UnidentifiedImageError

from ecommerce_product_image_creator.preparation import (
    check_mock_review_current,
    read_prepared,
)
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.tasks import read_prompt_context

if TYPE_CHECKING:
    from .download_safety import DownloadLimits

ENDPOINT = "https://ai.t8star.org/v1/images/edits?async=true"
MODELS = frozenset({"gpt-image-2.5-sunburst", "gpt-image-2.5-flare"})


def download_artifact_candidate(*args: Any, **kwargs: Any) -> Any:
    """Delegate to the fail-closed internal candidate acquisition boundary."""
    from .download_safety import fetch_candidate

    return fetch_candidate(*args, **kwargs)


def _deliver_downloaded(
    store: Store,
    locator_id: str,
    url: str,
    digest: str,
    mime: str,
    limits: "DownloadLimits",
    budget: ResourceBudget,
    delivery_id: str,
    artifact_id: str,
    artifact_root: Path,
    target: Path,
    *,
    stage: str,
) -> str:
    """Download and delegate delivery; production verifiers fail closed."""
    from . import artifact_locator, first_artifact
    from .storage import ConflictError

    try:
        candidate = download_artifact_candidate(
            store, locator_id, url, digest, mime, limits, budget
        )
    except (OSError, ValueError, MemoryError, sqlite3.Error):
        try:
            first_artifact.record_download_failure(
                store, locator_id, url, digest, mime, stage
            )
        except (OSError, ValueError, sqlite3.Error):
            pass
        raise
    locator = artifact_locator.read_locator(store, locator_id)
    if locator is None or locator.stage != stage:
        raise ValueError("download_stage_identity_unverified")
    # No delivery identity is invented until independent source checks succeed.
    store.register_first_artifact(
        delivery_id, artifact_id, locator_id, digest, mime, artifact_root, target
    )
    download = (url, candidate)
    try:
        store.stage_first_artifact(
            delivery_id, artifact_root, budget, _download=download
        )
        return store.reconcile_first_artifact(
            delivery_id, artifact_root, budget, _download=download
        )
    except (OSError, ValueError, MemoryError, sqlite3.Error):
        # A stale source must not be allowed to mutate delivery status.  The
        # original error is retained if a competing delivery already completed.
        try:
            first_artifact.record_download_save_failure(store, delivery_id, *download)
        except (OSError, ValueError, ConflictError, sqlite3.Error, MemoryError):
            pass
        raise


def deliver_downloaded_form(*args: Any, **kwargs: Any) -> str:
    """Deliver an authorized Form result using the existing first-delivery path."""
    return _deliver_downloaded(*args, **kwargs, stage="form")


def deliver_downloaded_mock(*args: Any, **kwargs: Any) -> str:
    """Deliver an authorized Mock result with current Form review validation."""
    return _deliver_downloaded(*args, **kwargs, stage="mock")


class ProviderError(ValueError):
    """Sanitized local mapping error."""


@dataclass(frozen=True)
class ImagePart:
    ref_id: str = field(repr=False)
    mime: str
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class CandidateRequest:
    model: str
    endpoint: str
    group: str
    prompt: str = field(repr=False)
    images: tuple[ImagePart, ...] = field(repr=False)
    image_field: str = "image[]"
    protocol_verified: bool = False
    stage: str = ""

    @property
    def fields(self) -> tuple[tuple[str, str], ...]:
        return (("model", self.model), ("prompt", self.prompt))


@dataclass(frozen=True)
class ProviderResult:
    state: str
    task_id: str | None = None
    upstream_status: str | None = None
    image_urls: tuple[str, ...] = field(default=(), repr=False)
    cost: float | None = None
    result_available: bool = False
    error: str | None = None


@dataclass(frozen=True)
class SerializedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class RecoveryTickResult:
    action: str
    reason: str
    candidate_id: str | None = None
    attempt_id: str | None = None
    deadline: float | None = None


def recovery_tick(
    service: Any, root: str, tick_id: str, *, clock: Callable[[], float]
) -> RecoveryTickResult:
    """Advance at most one authorized offline retry, using persisted identities."""
    import json
    import sqlite3
    from datetime import UTC, datetime
    from pathlib import Path

    from . import candidate_preflight, faults, retry_scheduler, retry_scope
    from .domain import (
        Confirmation,
        Reference,
        ReferenceCategory,
        Revision,
        Task,
        TaskStatus,
    )
    from .execution_candidates import read_candidate
    from .preparation import _observations
    from .send_evidence import verified_pre_send, verified_recoverability
    from .tasks import OperationScope, read_prompt_context

    if not root or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tick_id):
        return RecoveryTickResult("stop", "invalid_identity")
    operation_id = (
        "auto-"
        + hashlib.sha256(
            json.dumps([root, tick_id], separators=(",", ":")).encode()
        ).hexdigest()
    )
    try:
        now = clock()
        if not math.isfinite(now):
            return RecoveryTickResult("stop", "invalid_clock")
        with closing(service.store._connect()) as db:
            original = db.execute(
                "SELECT task_id,revision,attempt_count FROM operation WHERE operation_id=?",
                (root,),
            ).fetchone()
            if not original:
                return RecoveryTickResult("stop", "root_unavailable")
            if (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='risk_consumption'"
                ).fetchone()
                and db.execute(
                    "SELECT 1 FROM risk_consumption WHERE linked_operation=?", (root,)
                ).fetchone()
            ):
                return RecoveryTickResult("stop", "linked_risk_one_attempt_limit")
            task_row = db.execute(
                "SELECT input_digest,instructions,status FROM task WHERE task_id=?",
                (original[0],),
            ).fetchone()
            latest = db.execute(
                "SELECT a.attempt_id,a.upstream_status,a.upstream_id,c.execution_digest,c.model,c.endpoint FROM operation_attempt c JOIN attempt a ON a.attempt_id=c.attempt_id WHERE c.operation_id=? ORDER BY c.sequence DESC LIMIT 1",
                (root,),
            ).fetchone()
            candidate_rows = db.execute(
                "SELECT candidate_id FROM execution_candidate WHERE root_operation_id=?",
                (root,),
            ).fetchall()
            has_scope = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='retry_scope_evidence'"
            ).fetchone()
            scope_rows = (
                db.execute("SELECT scope_id,kind FROM retry_scope_evidence").fetchall()
                if has_scope
                else []
            )
            replay = db.execute(
                "SELECT attempt_id FROM operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
            prior_reservation = (
                db.execute(
                    "SELECT candidate_id,host_id,cost_id FROM retry_scope_reservation WHERE operation_id=? AND root=?",
                    (operation_id, root),
                ).fetchone()
                if has_scope
                else None
            )
        if not task_row or not latest:
            return RecoveryTickResult("stop", "history_unavailable")
        candidates = [read_candidate(service.store, row[0]) for row in candidate_rows]
        if replay:
            if not prior_reservation:
                return RecoveryTickResult("stop", "retry_reservation_mismatch")
            candidate = next(
                (c for c in candidates if c.candidate_id == prior_reservation[0]), None
            )
            if candidate is None:
                return RecoveryTickResult("stop", "candidate_unavailable")
            host_id, cost_id = prior_reservation[1:]
        else:
            attempt, state, upstream, digest, _, _ = latest
            if state == "succeeded":
                return RecoveryTickResult(
                    "stop", "already_succeeded", attempt_id=attempt
                )
            with closing(service.store._connect()) as db:
                pre_send = verified_pre_send(
                    db, root, attempt, original[0], original[1], digest
                )
                recoverable = (
                    verified_recoverability(
                        db, root, attempt, original[0], original[1], digest
                    )
                    if pre_send
                    else None
                )
                if pre_send and recoverable != "transient_local":
                    return RecoveryTickResult(
                        "stop", "failure_not_recoverable", attempt_id=attempt
                    )
                if not pre_send and state in {
                    "unknown",
                    "submission_uncertain",
                    "accepted",
                    "pending",
                    "running",
                }:
                    return RecoveryTickResult(
                        faults.classify_transport(
                            "read_timeout", evidence="read_timeout", task_id=upstream
                        ).next_action,
                        "original_operation_unresolved",
                        attempt_id=attempt,
                    )
                if not pre_send:
                    if state != "failed" or not upstream:
                        return RecoveryTickResult(
                            "stop", "failure_unverified", attempt_id=attempt
                        )
                    observation = retry_scheduler._load_observation(db, root, attempt)
                    if (
                        not observation
                        or observation.task_id != upstream
                        or faults.classify_query(
                            200, observation.payload, upstream
                        ).state
                        != "failed"
                        or not (
                            observation.failure_status == 429
                            or 500 <= observation.failure_status <= 599
                        )
                    ):
                        return RecoveryTickResult(
                            "stop", "failure_unverified", attempt_id=attempt
                        )
                    started, header, status = (
                        observation.observed_at,
                        observation.retry_after,
                        observation.failure_status,
                    )
                else:
                    proof = db.execute(
                        "SELECT recorded_at FROM attempt_transport_evidence WHERE attempt_id=?",
                        (attempt,),
                    ).fetchone()
                    if not proof:
                        return RecoveryTickResult(
                            "stop", "failure_unverified", attempt_id=attempt
                        )
                    started, header, status = proof[0], None, 0
            retry_after_delay = (
                faults.parse_retry_after(header, datetime.fromtimestamp(started, UTC))
                if status == 429
                else None
            )
            if status == 429 and retry_after_delay is None:
                return RecoveryTickResult(
                    "stop", "retry_after_required", attempt_id=attempt
                )
            if original[2] >= 6:
                return RecoveryTickResult(
                    "stop", "retry_budget_exhausted", attempt_id=attempt
                )
            target_model = (
                candidate_preflight.SUNBURST
                if original[2] < 3
                else candidate_preflight.FLARE
            )
            selected = [
                c
                for c in candidates
                if c.snapshot.model == target_model
                and c.snapshot.endpoint == ENDPOINT
                and c.snapshot.group == "reverse"
                and (
                    (
                        target_model == candidate_preflight.SUNBURST
                        and c.source_candidate_id is None
                    )
                    or (
                        target_model == candidate_preflight.FLARE
                        and c.source_candidate_id is not None
                    )
                )
            ]
            if len(selected) != 1:
                return RecoveryTickResult(
                    "stop", "candidate_unavailable_or_ambiguous", attempt_id=attempt
                )
            candidate = selected[0]
            budget = candidate_preflight.budget_view(
                service.store, root, candidate.candidate_id
            )
            if not budget.complete or not budget.allows_evaluation:
                return RecoveryTickResult(
                    "stop", "retry_budget_unavailable", attempt_id=attempt
                )
            matches: dict[str, list[str]] = {"host": [], "cost": []}
            with closing(service.store._connect()) as db:
                for sid, kind in scope_rows:
                    if kind not in matches:
                        continue
                    try:
                        value = retry_scope._read(db, sid, kind)
                    except (ValueError, TypeError, sqlite3.Error):
                        continue
                    if value.root == root and any(
                        (g.candidate_id, g.digest, g.model, g.endpoint, g.group)
                        == (
                            candidate.candidate_id,
                            candidate.snapshot.execution_digest,
                            candidate.snapshot.model,
                            candidate.snapshot.endpoint,
                            candidate.snapshot.group,
                        )
                        for g in value.grants
                    ):
                        matches[kind].append(sid)
            if len(matches["host"]) != 1 or len(matches["cost"]) != 1:
                return RecoveryTickResult(
                    "stop",
                    "scope_unavailable_or_ambiguous",
                    candidate.candidate_id,
                    attempt,
                )
            host_id, cost_id = matches["host"][0], matches["cost"][0]
            wait = service.store.retry_wait_status(root, now, attempt_id=attempt)
            if wait == "unknown":
                service.store.record_retry_wait(root, attempt, started, header, status)
                wait = service.store.retry_wait_status(root, now, attempt_id=attempt)
            with closing(service.store._connect()) as db:
                deadline_row = db.execute(
                    "SELECT deadline FROM retry_wait WHERE operation_id=? AND attempt_id=? UNION ALL SELECT deadline FROM retry_wait_attempt WHERE operation_id=? AND attempt_id=?",
                    (root, attempt, root, attempt),
                ).fetchone()
            if status == 429 and (
                deadline_row is None
                or retry_after_delay is None
                or deadline_row[0] < started + retry_after_delay
            ):
                return RecoveryTickResult(
                    "stop", "retry_wait_conflict", candidate.candidate_id, attempt
                )
            if wait == "waiting":
                return RecoveryTickResult(
                    "waiting",
                    "deadline_not_reached",
                    candidate.candidate_id,
                    attempt,
                    deadline_row[0] if deadline_row else None,
                )
            if wait != "elapsed_recheck_required":
                return RecoveryTickResult(
                    "stop", "wait_unverified", candidate.candidate_id, attempt
                )
        saved = read_prepared(service.store, original[0], original[1])
        prompt = read_prompt_context(service.store, original[0], original[1]).prompt
        snapshot = candidate.snapshot
        task = Task(original[0], task_row[0], task_row[1], TaskStatus(task_row[2]))
        revision = Revision(original[0], original[1], snapshot.input_digest)
        refs = [
            Reference(
                item["ref_id"],
                ReferenceCategory(item["category"]),
                Path("."),
                item["mime"],
                item["byte_count"],
                item["sha256"],
                view=item["view"],
                region=item["region"],
                version=item["version"],
                importance=item["importance"],
                purpose=item.get("purpose"),
                observations=_observations(item.get("observations")),
                task_id=original[0],
            )
            for item in saved["references"]
        ]
        confirmation = Confirmation(
            original[0],
            original[1],
            snapshot.input_digest,
            snapshot.prompt_digest,
            snapshot.prompt_revision,
            snapshot.execution_digest,
            confirmation_id="auto-confirm-" + operation_id[5:],
        )
        result = retry_scheduler.scoped_retry_once(
            clock,
            service,
            task,
            revision,
            refs,
            prompt,
            snapshot,
            confirmation,
            operation_id,
            OperationScope(**saved["operation_scope"]),
            root=root,
            candidate_id=candidate.candidate_id,
            host_id=host_id,
            cost_id=cost_id,
        )
        return RecoveryTickResult(
            result.action,
            result.reason,
            candidate.candidate_id,
            result.result.attempt_id if result.result else None,
        )
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError):
        return RecoveryTickResult("stop", "local_checks_failed")


def build_request(store: Store, task_id: str, revision: int) -> CandidateRequest:
    """Build an unverified candidate from frozen bytes; does not authorize or send."""
    context = read_prompt_context(store, task_id, revision)
    snap = context.snapshot
    if (
        snap.digest_version != 2
        or snap.model not in MODELS
        or snap.endpoint != ENDPOINT
        or snap.group != "reverse"
    ):
        raise ProviderError("Unsupported or unverified provider combination")
    if snap.stage == "mock":
        with closing(store._connect()) as db:
            try:
                check_mock_review_current(db, task_id, revision)
            except ValueError:
                raise ProviderError("Form review is not currently verified") from None
    saved = read_prepared(store, task_id, revision)
    refs = saved["references"]
    values = list(zip(refs, saved["reference_bytes"], strict=True))
    selected = []
    for ref_id, digest in snap.references:
        matches = [(meta, data) for meta, data in values if meta["ref_id"] == ref_id]
        if len(matches) != 1:
            raise ProviderError("Frozen input association mismatch")
        meta, data = matches[0]
        if (
            meta["mime"] not in {"image/png", "image/jpeg"}
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ProviderError("Frozen input content mismatch")
        selected.append(ImagePart(ref_id, meta["mime"], digest, data))
    return CandidateRequest(
        snap.model,
        snap.endpoint,
        snap.group,
        context.prompt.text,
        tuple(selected),
        stage=snap.stage,
    )


def serialize_request(candidate: CandidateRequest) -> SerializedRequest:
    """Encode an offline multipart candidate; this function does not transmit it."""
    if (
        candidate.endpoint != ENDPOINT
        or candidate.model not in MODELS
        or candidate.group != "reverse"
        or candidate.image_field != "image[]"
        or not candidate.prompt.strip()
        or (candidate.stage == "form" and not 1 <= len(candidate.images) <= 6)
        or (candidate.stage == "mock" and not 2 <= len(candidate.images) <= 3)
        or candidate.stage not in {"form", "mock"}
    ):
        raise ProviderError("Invalid candidate for offline serialization")
    values = [candidate.model.encode("utf-8"), candidate.prompt.encode("utf-8")]
    for image in candidate.images:
        if (
            image.mime not in {"image/png", "image/jpeg"}
            or hashlib.sha256(image.data).hexdigest() != image.sha256
        ):
            raise ProviderError("Frozen input content mismatch")
        try:
            with Image.open(BytesIO(image.data)) as decoded:
                if (
                    decoded.format
                    != {"image/png": "PNG", "image/jpeg": "JPEG"}[image.mime]
                ):
                    raise ProviderError("Frozen input MIME mismatch")
                decoded.verify()
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            raise ProviderError("Frozen input is not a valid image") from None
        values.append(image.data)
    digest = hashlib.sha256()
    for value in values:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    seed = digest.hexdigest()
    boundary = f"product-image-{seed}"
    counter = 0
    while any(("--" + boundary).encode("ascii") in value for value in values):
        counter += 1
        boundary = f"product-image-{seed}-{counter}"
    chunks: list[bytes] = []
    for name, value in candidate.fields:
        chunks.extend(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    for index, image in enumerate(candidate.images, 1):
        extension = "png" if image.mime == "image/png" else "jpg"
        chunks.extend(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="image-{index:02}.{extension}"\r\nContent-Type: {image.mime}\r\n\r\n'.encode(),
                image.data,
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return SerializedRequest(
        "POST",
        candidate.endpoint,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        b"".join(chunks),
    )


def _id(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
        else None
    )


def transport_unknown() -> ProviderResult:
    return ProviderResult("unknown", error="Remote execution cannot be determined")


def build_query_path(task_id: str) -> str:
    if not _id(task_id):
        raise ProviderError("Invalid upstream task identifier")
    return f"/v1/images/tasks/{task_id}"


def parse_submission(http_status: int, payload: Any) -> ProviderResult:
    if http_status != 200 or not isinstance(payload, dict):
        return transport_unknown()
    if "code" in payload and payload["code"] != "success":
        return transport_unknown()
    raw_data = payload.get("data")
    candidates = [payload.get("task_id")]
    if isinstance(raw_data, str):
        candidates.append(raw_data)
    elif isinstance(raw_data, dict):
        candidates.append(raw_data.get("task_id"))
    ids = {_id(value) for value in candidates if value is not None}
    if None in ids or len(ids) != 1:
        return transport_unknown()
    return ProviderResult("accepted", task_id=next(iter(ids)))


def parse_query(http_status: int, payload: Any, task_id: str) -> ProviderResult:
    if not _id(task_id) or http_status != 200 or not isinstance(payload, dict):
        return transport_unknown()
    if "code" in payload and payload["code"] != "success":
        return transport_unknown()
    data = payload.get("data")
    if not isinstance(data, dict) or _id(data.get("task_id")) != task_id:
        return transport_unknown()
    status = data.get("status")
    states = {
        "NOT_START": "pending",
        "IN_PROGRESS": "running",
        "FAILURE": "failed",
        "SUCCESS": "succeeded",
    }
    if not isinstance(status, str) or status not in states:
        return transport_unknown()
    cost = data.get("cost")
    try:
        amount = (
            float(cost)
            if isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0
            else None
        )
    except (OverflowError, ValueError):
        amount = None
    urls: tuple[str, ...] = ()
    inner = data.get("data")
    if (
        status == "SUCCESS"
        and isinstance(inner, dict)
        and isinstance(inner.get("data"), list)
    ):
        items = inner["data"]
        if all(
            isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]
            for item in items
        ):
            urls = tuple(item["url"] for item in items)
    return ProviderResult(
        states[status],
        task_id=task_id,
        upstream_status=status,
        image_urls=urls,
        cost=amount,
        result_available=len(urls) == 1,
        error="Upstream reported failure" if status == "FAILURE" else None,
    )
