"""Non-billable form-stage preparation with transactionally stored input bytes."""

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from ecommerce_product_image_creator.config import team_mode_enabled
from ecommerce_product_image_creator.domain import (
    ExecutionSnapshot,
    InputValidationError,
    Observation,
    ObservationSource,
    ObservationStatus,
    PromptRevision,
    Reference,
    ReferenceCategory,
    Revision,
    digest_references,
    validate_task_input,
)
from ecommerce_product_image_creator.storage import (
    FileSafetyError,
    ResourceBudget,
    Store,
    read_reference,
)
from ecommerce_product_image_creator.tasks import FORM_OUTPUT_CONTRACT, OperationScope


class PreparationError(ValueError):
    """Sanitized, actionable local input or state failure."""


def _observations(value: Any) -> tuple[Observation, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("Invalid observations")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {
            "subject",
            "status",
            "source",
            "source_ref_id",
            "description",
        }:
            raise ValueError("Invalid observation")
        source = item.get("source")
        result.append(
            Observation(
                item["subject"],
                ObservationStatus(item["status"]),
                ObservationSource(source) if source is not None else None,
                item.get("source_ref_id"),
                item.get("description"),
            )
        )
    return tuple(result)


def _tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS prepared_execution (
        task_id TEXT NOT NULL, revision INTEGER NOT NULL, stage TEXT NOT NULL,
        prompt TEXT NOT NULL, prompt_digest TEXT NOT NULL, execution_digest TEXT NOT NULL,
        model TEXT NOT NULL, endpoint TEXT NOT NULL, grouping TEXT NOT NULL,
        PRIMARY KEY(task_id,revision), FOREIGN KEY(task_id,revision) REFERENCES revision(task_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS prepared_reference (
        task_id TEXT NOT NULL, revision INTEGER NOT NULL, position INTEGER NOT NULL,
        ref_id TEXT NOT NULL, category TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL,
        data BLOB NOT NULL, PRIMARY KEY(task_id,revision,position),
        FOREIGN KEY(task_id,revision) REFERENCES prepared_execution(task_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS prepared_snapshot_v2 (
        task_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(task_id,revision), FOREIGN KEY(task_id,revision) REFERENCES prepared_execution(task_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS prepared_scope_v2 (
        task_id TEXT NOT NULL, revision INTEGER NOT NULL, scope TEXT NOT NULL,
        PRIMARY KEY(task_id,revision), FOREIGN KEY(task_id,revision) REFERENCES prepared_snapshot_v2(task_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS prepared_input_v2 (
        task_id TEXT NOT NULL, revision INTEGER NOT NULL, context TEXT NOT NULL,
        PRIMARY KEY(task_id,revision), FOREIGN KEY(task_id,revision) REFERENCES prepared_snapshot_v2(task_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS form_review (
        review_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, form_revision INTEGER NOT NULL,
        artifact_id TEXT NOT NULL, artifact_sha256 TEXT NOT NULL, source TEXT NOT NULL,
        FOREIGN KEY(artifact_id) REFERENCES artifact(artifact_id),
        FOREIGN KEY(task_id,form_revision) REFERENCES revision(task_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS form_review_invalidation (
        review_id TEXT PRIMARY KEY, FOREIGN KEY(review_id) REFERENCES form_review(review_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS prepared_mock_source (
        task_id TEXT NOT NULL, revision INTEGER NOT NULL, form_revision INTEGER NOT NULL,
        artifact_id TEXT NOT NULL, review_id TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,
        PRIMARY KEY(task_id,revision), FOREIGN KEY(task_id,revision) REFERENCES prepared_execution(task_id,revision),
        FOREIGN KEY(review_id) REFERENCES form_review(review_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS prepared_conflicts (
        task_id TEXT NOT NULL, revision INTEGER NOT NULL, reported TEXT NOT NULL,
        PRIMARY KEY(task_id,revision), FOREIGN KEY(task_id,revision) REFERENCES prepared_execution(task_id,revision))""")


def _conflicts(value: list[str] | None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise PreparationError("Invalid structure conflict description")
    return list(value)


def _preserved_inputs(
    db: sqlite3.Connection,
    task_id: str,
    revision: int,
    refs: list[Reference],
    categories: set[str],
) -> bool:
    """Compare frozen registered inputs in their original order."""
    old = db.execute(
        "SELECT context FROM prepared_input_v2 WHERE task_id=? AND revision=?",
        (task_id, revision),
    ).fetchone()
    if not old:
        return False
    before = [
        r for r in json.loads(old[0])["references"] if r["category"] in categories
    ]
    after = [
        r
        for r in json.loads(_input_context(refs, "comparison"))["references"]
        if r["category"] in categories
    ]
    return before == after


def _input_context(refs: list[Reference], instructions: str) -> str:
    return json.dumps(
        {
            "instructions": instructions,
            "references": [
                {
                    "ref_id": ref.ref_id,
                    "category": ref.category.value,
                    "mime": ref.mime,
                    "byte_count": ref.byte_count,
                    "sha256": ref.sha256,
                    "view": ref.view,
                    "region": ref.region,
                    "version": ref.version,
                    "importance": ref.importance,
                    "purpose": ref.purpose,
                    **(
                        {
                            "observations": [
                                {
                                    "subject": o.subject,
                                    "status": o.status.value,
                                    "source": o.source.value if o.source else None,
                                    "source_ref_id": o.source_ref_id,
                                    "description": o.description,
                                }
                                for o in ref.observations
                            ]
                        }
                        if ref.observations
                        else {}
                    ),
                }
                for ref in refs
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def prepare_form(
    store: Store,
    root: Path,
    budget: ResourceBudget,
    refs: list[dict[str, Any]],
    instructions: str,
    prompt: str,
    model: str,
    endpoint: str,
    grouping: str,
    *,
    task_id: str | None = None,
    structure_conflicts: list[str] | None = None,
    _revision_decision_id: str | None = None,
) -> dict[str, Any]:
    """Freeze task references; form execution includes form references only."""
    if not all(
        isinstance(x, str) and x.strip()
        for x in (instructions, prompt, model, endpoint, grouping)
    ):
        raise PreparationError(
            "instructions, prompt, model, endpoint and group must be nonempty"
        )
    existing = task_id is not None
    reported_conflicts = _conflicts(structure_conflicts)
    task_id = task_id or uuid.uuid4().hex
    prepared: list[Reference] = []
    frozen: list[bytes] = []
    for index, item in enumerate(refs):
        try:
            category = ReferenceCategory(item["category"])
            ref_id = item["ref_id"]
            image = read_reference(root, Path(item["path"]), item["mime"], budget)
            prepared.append(
                Reference(
                    ref_id,
                    category,
                    Path(item["path"]),
                    image.mime,
                    len(image.data),
                    image.sha256,
                    view=item.get("view"),
                    region=item.get("region"),
                    version=item.get("version"),
                    importance=item.get("importance"),
                    purpose=item.get("purpose"),
                    observations=_observations(item.get("observations")),
                    task_id=task_id,
                )
            )
            frozen.append(image.data)
        except (KeyError, ValueError, FileSafetyError, TypeError):
            raise PreparationError(f"Invalid reference at index {index}") from None
    try:
        validate_task_input(prepared, instructions)
    except InputValidationError as error:
        if error.category:
            raise PreparationError(
                f"Invalid reference count: {error.category}"
            ) from None
        raise PreparationError("Invalid task inputs") from None
    input_digest = digest_references(prepared, instructions)
    prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    stage_refs = [ref for ref in prepared if ref.category == ReferenceCategory.FORM]
    try:
        with closing(store._connect()) as db, db:
            _tables(db)
            db.execute("BEGIN IMMEDIATE")
            revision = 1
            needs_revision = False
            if existing:
                earlier = db.execute(
                    """SELECT c.reported FROM prepared_execution p
                       JOIN revision r ON r.task_id=p.task_id AND r.revision=p.revision
                       JOIN prepared_conflicts c ON c.task_id=p.task_id AND c.revision=p.revision
                       WHERE p.task_id=? AND p.stage='form' AND r.input_digest=?
                       ORDER BY p.revision DESC LIMIT 1""",
                    (task_id, input_digest),
                ).fetchone()
                if earlier:
                    reported_conflicts = list(
                        dict.fromkeys(
                            [*_conflicts(json.loads(earlier[0])), *reported_conflicts]
                        )
                    )
                current = db.execute(
                    "SELECT status FROM task WHERE task_id=?", (task_id,)
                ).fetchone()
                latest = db.execute(
                    "SELECT MAX(revision) FROM prepared_execution WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if (
                    current is None
                    or current[0] not in {"draft", "awaiting_confirmation", "ready"}
                    or latest is None
                    or latest[0] is None
                ):
                    raise PreparationError("Existing task cannot be revised")
                prior_form = db.execute(
                    "SELECT MAX(revision) FROM prepared_execution WHERE task_id=? AND stage='form'",
                    (task_id,),
                ).fetchone()
                needs_revision = bool(
                    prior_form
                    and prior_form[0]
                    and not _preserved_inputs(
                        db, task_id, prior_form[0], prepared, {"form", "artwork"}
                    )
                )
                if _revision_decision_id and not needs_revision:
                    raise PreparationError(
                        "Input revision decision does not match a change"
                    )
                if needs_revision:
                    from ecommerce_product_image_creator.input_revision import (
                        check_input_revision_locked,
                    )

                    check_input_revision_locked(
                        db,
                        store,
                        _revision_decision_id,
                        task_id,
                        int(latest[0]),
                        json.loads(_input_context(prepared, instructions))[
                            "references"
                        ],
                    )
                # A recorded subset proposal may only be applied via its checked
                # decision path; a plain revision must not silently bypass it.
                if (
                    not needs_revision
                    and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subset_proposal'"
                    ).fetchone()
                ):
                    proposals = db.execute(
                        """SELECT p.candidates FROM subset_proposal p
                           JOIN prepared_execution e ON e.task_id=p.task_id AND e.revision=p.revision
                           LEFT JOIN subset_decision d ON d.proposal_id=p.proposal_id
                           WHERE p.task_id=? AND e.stage='form' AND d.target_revision IS NULL""",
                        (task_id,),
                    ).fetchall()
                    present = {ref.ref_id for ref in stage_refs}
                    if any(
                        not set(json.loads(row[0])).issubset(present)
                        for row in proposals
                    ):
                        raise PreparationError(
                            "Subset decision must be applied through its checked revision"
                        )
                revision = int(latest[0]) + 1
                db.execute(
                    "UPDATE task SET input_digest=?,instructions=?,status='awaiting_confirmation' WHERE task_id=?",
                    (input_digest, instructions, task_id),
                )
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_eligibility'"
                ).fetchone():
                    db.execute(
                        "UPDATE local_eligibility SET invalidated=1 WHERE task_id=? AND consumed=0",
                        (task_id,),
                    )
            else:
                db.execute(
                    "INSERT INTO task(task_id,input_digest,instructions) VALUES(?,?,?)",
                    (task_id, input_digest, instructions),
                )
            prompt_revision = PromptRevision.from_text(
                task_id, revision, revision, "form", prompt
            )
            scope = OperationScope("generate", None, "six-view-composite", 3)
            snapshot = ExecutionSnapshot.create(
                Revision(task_id, revision, input_digest),
                prompt_revision,
                "form",
                model,
                endpoint,
                grouping,
                references=tuple((ref.ref_id, ref.sha256) for ref in stage_refs),
                operation_scope=scope.digest(),
            )
            execution_digest = snapshot.execution_digest
            db.execute(
                "INSERT INTO revision VALUES(?,?,?)", (task_id, revision, input_digest)
            )
            db.execute(
                "INSERT INTO prepared_execution VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    revision,
                    "form",
                    prompt,
                    prompt_digest,
                    execution_digest,
                    model,
                    endpoint,
                    grouping,
                ),
            )
            db.execute(
                "INSERT INTO prepared_snapshot_v2 VALUES(?,?,?)",
                (task_id, revision, snapshot.to_payload()),
            )
            db.execute(
                "INSERT INTO prepared_input_v2 VALUES(?,?,?)",
                (task_id, revision, _input_context(prepared, instructions)),
            )
            db.execute(
                "INSERT INTO prepared_conflicts VALUES(?,?,?)",
                (task_id, revision, json.dumps(reported_conflicts, ensure_ascii=False)),
            )
            db.execute(
                "INSERT INTO prepared_scope_v2 VALUES(?,?,?)",
                (
                    task_id,
                    revision,
                    json.dumps(
                        {
                            "operation": scope.operation,
                            "target_view": scope.target_view,
                            "output": scope.output,
                            "max_attempts": scope.max_attempts,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            for i, (ref, data) in enumerate(zip(prepared, frozen, strict=True)):
                db.execute(
                    "INSERT INTO prepared_reference VALUES(?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        revision,
                        i,
                        ref.ref_id,
                        ref.category.value,
                        ref.mime,
                        ref.sha256,
                        data,
                    ),
                )
            if existing and needs_revision:
                changed = db.execute(
                    "UPDATE input_revision_decision SET target_revision=?,target_execution_digest=? WHERE decision_id=? AND target_revision IS NULL AND invalidated=0",
                    (revision, execution_digest, _revision_decision_id),
                )
                if changed.rowcount != 1:
                    raise PreparationError(
                        "Input revision decision is already consumed"
                    )
    except sqlite3.Error:
        raise PreparationError("Unable to persist preparation") from None
    result = {
        "task_id": task_id,
        "revision": revision,
        "stage": "form",
        "input_digest": input_digest,
        "prompt_revision": revision,
        "prompt_digest": prompt_digest,
        "execution_digest": execution_digest,
        "model": model,
        "endpoint": endpoint,
        "group": grouping,
        "output": "six-view-composite",
        "operation_scope": {
            "operation": scope.operation,
            "target_view": scope.target_view,
            "output": scope.output,
            "max_attempts": scope.max_attempts,
        },
        "stage_references": [ref.ref_id for ref in stage_refs],
        "cost": "unknown",
        "duration": "unknown",
        "authorization": "unverified",
        "pending": ["form_review", "business_authorization"],
        "form_output_contract": list(FORM_OUTPUT_CONTRACT),
    }
    result.update(_response_details(store, task_id, revision))
    if result["stage_context"]["prompt_semantic_conflicts"]:
        result["pending"].append("prompt_brand_clarification")
    if result["structure_conflicts"]["reported"]:
        result["pending"].append("structure_clarification")
    if result["packaging_versions"]["status"] == "multiple_declared_versions":
        result["pending"].append("packaging_version_confirmation")
    return result


def read_prepared(store: Store, task_id: str, revision: int) -> dict[str, Any]:
    """Read persisted snapshot only after verifying every stored byte digest."""
    try:
        with closing(store._connect()) as db:
            row = db.execute(
                "SELECT p.prompt,p.prompt_digest,p.execution_digest,p.stage,p.model,p.endpoint,p.grouping,r.input_digest FROM prepared_execution p JOIN revision r ON p.task_id=r.task_id AND p.revision=r.revision WHERE p.task_id=? AND p.revision=?",
                (task_id, revision),
            ).fetchone()
            refs = db.execute(
                "SELECT sha256,data,ref_id,category,mime FROM prepared_reference WHERE task_id=? AND revision=? ORDER BY position",
                (task_id, revision),
            ).fetchall()
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prepared_snapshot_v2'"
            ).fetchone()
            v2 = (
                db.execute(
                    "SELECT payload FROM prepared_snapshot_v2 WHERE task_id=? AND revision=?",
                    (task_id, revision),
                ).fetchone()
                if table
                else None
            )
            scope_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prepared_scope_v2'"
            ).fetchone()
            scope_row = (
                db.execute(
                    "SELECT scope FROM prepared_scope_v2 WHERE task_id=? AND revision=?",
                    (task_id, revision),
                ).fetchone()
                if scope_table
                else None
            )
            context_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prepared_input_v2'"
            ).fetchone()
            context_row = (
                db.execute(
                    "SELECT context FROM prepared_input_v2 WHERE task_id=? AND revision=?",
                    (task_id, revision),
                ).fetchone()
                if context_table
                else None
            )
            source_row = (
                db.execute(
                    "SELECT form_revision,artifact_id,review_id,artifact_sha256 FROM prepared_mock_source WHERE task_id=? AND revision=?",
                    (task_id, revision),
                ).fetchone()
                if row and row[3] == "mock"
                else None
            )
            source_review = (
                db.execute(
                    "SELECT task_id,form_revision,artifact_id,artifact_sha256 FROM form_review WHERE review_id=?",
                    (source_row[2],),
                ).fetchone()
                if source_row
                else None
            )
        if (
            row is None
            or not refs
            or hashlib.sha256(row[0].encode("utf-8")).hexdigest() != row[1]
            or any(hashlib.sha256(ref[1]).hexdigest() != ref[0] for ref in refs)
        ):
            raise PreparationError("Stored snapshot integrity failure")
        payload = [
            task_id,
            revision,
            row[3],
            row[7],
            row[1],
            row[4],
            row[5],
            row[6],
            "six-view-composite",
            [(ref[2], ref[0]) for ref in refs if ref[3] == "form"],
        ]
        legacy_digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        scope_data: dict[str, Any] | None = None
        context_data: dict[str, Any] | None = None
        if v2:
            try:
                snapshot = ExecutionSnapshot.from_payload(v2[0])
                if scope_row is None:
                    raise ValueError("Missing scope")
                if context_row is None:
                    raise ValueError("Missing input context")
                context_data = json.loads(context_row[0])
                if (
                    not isinstance(context_data, dict)
                    or not isinstance(context_data.get("references"), list)
                    or not isinstance(context_data.get("instructions"), str)
                ):
                    raise TypeError("Invalid input context")
                context_refs = [
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
                        task_id=task_id,
                    )
                    for item in context_data["references"]
                ]
                if digest_references(context_refs, context_data["instructions"]) != row[
                    7
                ] or [
                    (
                        ref.ref_id,
                        ref.category.value,
                        ref.mime,
                        ref.sha256,
                        ref.byte_count,
                    )
                    for ref in context_refs
                ] != [(ref[2], ref[3], ref[4], ref[0], len(ref[1])) for ref in refs]:
                    raise ValueError("Input context mismatch")
                scope_data = json.loads(scope_row[0])
                if not isinstance(scope_data, dict):
                    raise TypeError("Invalid scope")
                scope = OperationScope(
                    scope_data["operation"],
                    scope_data["target_view"],
                    scope_data["output"],
                    scope_data["max_attempts"],
                )
                if scope.digest() != snapshot.operation_scope:
                    raise ValueError("Scope mismatch")
            except (ValueError, TypeError, KeyError):
                raise PreparationError("Stored snapshot integrity failure") from None
            if (
                snapshot.task_id,
                snapshot.revision,
                snapshot.stage,
                snapshot.input_digest,
                snapshot.prompt_digest,
                snapshot.model,
                snapshot.endpoint,
                snapshot.group,
                snapshot.references,
                snapshot.execution_digest,
            ) != (
                task_id,
                revision,
                row[3],
                row[7],
                row[1],
                row[4],
                row[5],
                row[6],
                tuple(
                    (ref[2], ref[0])
                    for ref in refs
                    if row[3] == "mock" or ref[3] == "form"
                ),
                row[2],
            ):
                raise PreparationError("Stored snapshot integrity failure")
            if row[3] == "mock" and (
                source_row is None
                or not refs
                or source_row[1] != refs[0][2]
                or source_row[3] != refs[0][0]
                or source_review
                != (task_id, source_row[0], source_row[1], source_row[3])
            ):
                raise PreparationError("Stored mock source integrity failure")
        elif legacy_digest != row[2]:
            raise PreparationError("Stored snapshot integrity failure")
        return {
            "prompt": row[0],
            "prompt_digest": row[1],
            "execution_digest": row[2],
            "stage": row[3],
            "model": row[4],
            "endpoint": row[5],
            "group": row[6],
            "input_digest": row[7],
            "output": "six-view-composite",
            "stage_references": [
                x[2] for x in refs if row[3] == "mock" or x[3] == "form"
            ],
            "reference_digests": [x[0] for x in refs],
            "reference_bytes": [x[1] for x in refs],
            "digest_version": 2 if v2 else 0,
            "snapshot_payload": v2[0] if v2 else None,
            "operation_scope": scope_data if v2 else None,
            "form_source": {
                "form_revision": source_row[0],
                "artifact_id": source_row[1],
                "review_id": source_row[2],
                "sha256": source_row[3],
            }
            if source_row
            else None,
            "instructions": context_data["instructions"] if context_data else None,
            "references": context_data["references"] if context_data else None,
        }
    except sqlite3.Error:
        raise PreparationError("Unable to read preparation") from None


def _response_details(store: Store, task_id: str, revision: int) -> dict[str, Any]:
    from ecommerce_product_image_creator.tasks import read_prompt_context

    saved = read_prepared(store, task_id, revision)
    source = saved["form_source"]
    source_revision = source["form_revision"] if source else revision
    registered_snapshot = (
        read_prepared(store, task_id, source_revision) if source else saved
    )
    registered = registered_snapshot["references"]
    current = saved["references"]
    if registered is None or current is None:
        raise PreparationError("Reference summary unavailable for legacy snapshot")
    selected_ids = set(saved["stage_references"])
    selected = [r for r in current if r["ref_id"] in selected_ids]
    selected_keys = {(r["ref_id"], r["sha256"]) for r in selected}

    def counts(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            category: {"count": len(ids), "ids": ids}
            for category in ("artwork", "form")
            for ids in [[r["ref_id"] for r in items if r["category"] == category]]
        }

    not_selected = [
        r for r in registered if (r["ref_id"], r["sha256"]) not in selected_keys
    ]
    with closing(store._connect()) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prepared_conflicts'"
        ).fetchone()
        row = (
            db.execute(
                "SELECT reported FROM prepared_conflicts WHERE task_id=? AND revision=?",
                (task_id, revision),
            ).fetchone()
            if table
            else None
        )
    try:
        reported = _conflicts(json.loads(row[0])) if row else []
    except (ValueError, TypeError):
        raise PreparationError("Invalid stored structure conflicts") from None
    artwork = [r for r in current if r["category"] == "artwork"]
    return {
        "stage_context": read_prompt_context(store, task_id, revision).host_context(),
        "references_summary": {
            "registered_source_revision": source_revision,
            "registered": counts(registered),
            "selected": counts(selected),
            "not_selected": counts(not_selected),
        },
        "packaging_versions": {
            "references": [
                {"ref_id": r["ref_id"], "version": r.get("version")} for r in artwork
            ],
            "confirmation": "unverified",
            "status": "multiple_declared_versions"
            if len({r["version"] for r in artwork if r.get("version")}) > 1
            else "unconfirmed",
        },
        "structure_conflicts": {
            "status": "pending_clarification" if reported else "unknown",
            "reported": reported,
            "confirmation": "unverified",
        },
    }


def read_prepare_response(store: Store, task_id: str, revision: int) -> dict[str, Any]:
    """Reconstruct a historical prepare response without granting authorization."""
    saved = read_prepared(store, task_id, revision)
    if saved["digest_version"] != 2:
        raise PreparationError("Response unavailable for legacy snapshot")
    snapshot = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
    response = {
        "task_id": task_id,
        "revision": revision,
        "stage": saved["stage"],
        "input_digest": saved["input_digest"],
        "prompt_revision": snapshot.prompt_revision,
        "prompt_digest": saved["prompt_digest"],
        "execution_digest": saved["execution_digest"],
        "model": saved["model"],
        "endpoint": saved["endpoint"],
        "group": saved["group"],
        "output": saved["output"],
        "operation_scope": saved["operation_scope"],
        "stage_references": saved["stage_references"],
        "cost": "unknown",
        "duration": "unknown",
        "authorization": "unverified",
        "pending": ["form_review", "business_authorization"]
        if saved["stage"] == "form"
        else ["business_authorization"],
    }
    if saved["form_source"]:
        response.update(
            {
                "form_revision": saved["form_source"]["form_revision"],
                "review_id": saved["form_source"]["review_id"],
            }
        )
    if saved["stage"] == "form":
        response["form_output_contract"] = list(FORM_OUTPUT_CONTRACT)
    response.update(_response_details(store, task_id, revision))
    if response["stage_context"]["prompt_semantic_conflicts"]:
        response["pending"].append("prompt_brand_clarification")
    if response["structure_conflicts"]["reported"]:
        response["pending"].append("structure_clarification")
    if response["packaging_versions"]["status"] == "multiple_declared_versions":
        response["pending"].append("packaging_version_confirmation")
    return response


def _review_is_trusted(
    source: str,
    task_id: str,
    review_id: str,
    artifact_sha256: str,
    evidence_reference: str | None,
) -> bool:
    """Accept explicit local Team operator declarations; Strict stays closed."""
    return (
        team_mode_enabled()
        and source == "team-responsible-user"
        and bool(task_id and review_id and artifact_sha256)
        and evidence_reference
        in {
            "concept_prototype;internal_mock_allowed;unverified=back,sides,zipper",
            "concept_prototype;internal_mock_allowed;unverified=not_specified",
        }
    )


def _reject_review_conflict(
    db: sqlite3.Connection,
    task_id: str,
    form_revision: int,
    artifact_id: str,
    digest: str,
) -> None:
    decisions = db.execute(
        """SELECT DISTINCT d.decision FROM form_review r
        JOIN form_review_detail d ON d.review_id=r.review_id
        LEFT JOIN form_review_invalidation i ON i.review_id=r.review_id
        WHERE r.task_id=? AND r.form_revision=? AND r.artifact_id=? AND r.artifact_sha256=?
          AND d.decision IN ('accept','reject') AND d.invalidation_reason IS NULL
          AND i.review_id IS NULL AND (d.expires_at IS NULL OR d.expires_at>?)""",
        (task_id, form_revision, artifact_id, digest, time.time()),
    ).fetchall()
    if len(decisions) > 1:
        raise PreparationError(
            "Form review has conflicting decisions; clarification required"
        )


def check_mock_review_current(
    db: sqlite3.Connection, task_id: str, revision: int
) -> None:
    """Recheck live review in the caller's write transaction; historical reads remain available."""
    row = db.execute(
        """SELECT s.form_revision,s.artifact_id,s.artifact_sha256,s.review_id,
        r.source,d.decision,d.invalidation_reason,d.expires_at,d.evidence_reference,
        a.sha256,t.task_id,t.revision,t.upstream_status,t.delivery_status
        FROM prepared_mock_source s JOIN form_review r ON r.review_id=s.review_id
        JOIN form_review_detail d ON d.review_id=r.review_id
        JOIN artifact a ON a.artifact_id=s.artifact_id JOIN attempt t ON t.attempt_id=a.attempt_id
        WHERE s.task_id=? AND s.revision=? AND r.task_id=s.task_id
        AND r.form_revision=s.form_revision AND r.artifact_id=s.artifact_id
        AND r.artifact_sha256=s.artifact_sha256""",
        (task_id, revision),
    ).fetchone()
    if not row:
        raise PreparationError("Form review association unavailable")
    (
        form_rev,
        artifact_id,
        digest,
        review_id,
        source,
        decision,
        reason,
        expiry,
        evidence,
        artifact_digest,
        owner,
        attempt_revision,
        upstream,
        delivery,
    ) = row
    _reject_review_conflict(db, task_id, form_rev, artifact_id, digest)
    latest_form = db.execute(
        "SELECT MAX(revision) FROM prepared_execution WHERE task_id=? AND stage='form'",
        (task_id,),
    ).fetchone()
    latest_review = db.execute(
        "SELECT r.review_id FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id WHERE r.task_id=? AND r.form_revision=? ORDER BY r.rowid DESC LIMIT 1",
        (task_id, form_rev),
    ).fetchone()
    invalidated = db.execute(
        "SELECT 1 FROM form_review_invalidation WHERE review_id=?", (review_id,)
    ).fetchone()
    if (
        latest_form != (form_rev,)
        or latest_review != (review_id,)
        or invalidated
        or reason is not None
        or decision != "accept"
        or (expiry is not None and expiry <= time.time())
        or artifact_digest != digest
        or owner != task_id
        or attempt_revision != form_rev
        or upstream != "succeeded"
        or delivery != "delivered"
        or not _review_is_trusted(source, task_id, review_id, digest, evidence)
    ):
        raise PreparationError("Form review is no longer valid or verified")


def prepare_mock(
    store: Store,
    root: Path,
    budget: ResourceBudget,
    task_id: str,
    artifact_id: str,
    review_id: str,
    artwork: list[dict[str, Any]],
    prompt: str,
    model: str,
    endpoint: str,
    grouping: str,
    *,
    artifact_root: Path | None = None,
    instructions: str | None = None,
    structure_conflicts: list[str] | None = None,
    _revision_decision_id: str | None = None,
) -> dict[str, Any]:
    """Prepare only from persisted form evidence. The default trust boundary denies all reviews."""
    reported_conflicts = _conflicts(structure_conflicts)
    if not 1 <= len(artwork) <= 2 or not all(
        (
            task_id,
            artifact_id,
            review_id,
            prompt.strip(),
            model.strip(),
            endpoint.strip(),
            grouping.strip(),
        )
    ):
        raise PreparationError("Invalid mock preparation input")
    try:
        with closing(store._connect()) as db, db:
            _tables(db)
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT instructions,status FROM task WHERE task_id=?", (task_id,)
            ).fetchone()
            record = db.execute(
                """SELECT r.form_revision,r.artifact_sha256,r.source,a.path,a.sha256,t.revision,t.task_id,t.upstream_status,t.delivery_status,p.stage
                FROM form_review r JOIN artifact a ON a.artifact_id=r.artifact_id JOIN attempt t ON t.attempt_id=a.attempt_id
                JOIN prepared_execution p ON p.task_id=t.task_id AND p.revision=t.revision
                WHERE r.review_id=? AND r.task_id=? AND r.artifact_id=?""",
                (review_id, task_id, artifact_id),
            ).fetchone()
            latest = db.execute(
                "SELECT MAX(form_revision) FROM form_review WHERE task_id=?", (task_id,)
            ).fetchone()
            latest_form = db.execute(
                "SELECT MAX(revision) FROM prepared_execution WHERE task_id=? AND stage='form'",
                (task_id,),
            ).fetchone()
            invalidated = db.execute(
                "SELECT 1 FROM form_review_invalidation WHERE review_id=?", (review_id,)
            ).fetchone()
            detail_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='form_review_detail'"
            ).fetchone()
            detail = (
                db.execute(
                    "SELECT decision,invalidation_reason,expires_at,evidence_reference FROM form_review_detail WHERE review_id=?",
                    (review_id,),
                ).fetchone()
                if detail_table
                else None
            )
            if (
                not task
                or task[1] not in {"draft", "awaiting_confirmation", "ready"}
                or (instructions is not None and instructions != task[0])
                or not record
                or record[0] != record[5]
                or record[6] != task_id
                or record[7:10] != ("succeeded", "delivered", "form")
                or record[1] != record[4]
            ):
                raise PreparationError(
                    "Form artifact or task association is unavailable"
                )
            if (
                not latest
                or latest[0] != record[0]
                or not latest_form
                or latest_form[0] != record[0]
            ):
                raise PreparationError("Form review refers to a stale version")
            if detail is None:
                raise PreparationError("Form review decision is unavailable")
            _reject_review_conflict(db, task_id, record[0], artifact_id, record[1])
            current_review = db.execute(
                "SELECT r.review_id FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id WHERE r.task_id=? AND r.form_revision=? ORDER BY r.rowid DESC LIMIT 1",
                (task_id, record[0]),
            ).fetchone()
            if current_review != (review_id,):
                raise PreparationError("Form review has been superseded")
            if invalidated is not None or detail[1] is not None:
                raise PreparationError("Form review is invalidated")
            if detail[0] != "accept":
                raise PreparationError("Form review is not accepted")
            if detail[2] is not None and detail[2] <= time.time():
                raise PreparationError("Form review has expired")
            if not _review_is_trusted(
                record[2], task_id, review_id, record[1], detail[3]
            ):
                raise PreparationError("Form review source is unverified")
            try:
                image = read_reference(
                    artifact_root or root, Path(record[3]), "image/png", budget
                )
            except FileSafetyError:
                image = read_reference(
                    artifact_root or root, Path(record[3]), "image/jpeg", budget
                )
            if image.sha256 != record[1]:
                raise PreparationError("Form artifact digest mismatch")
            refs = [
                Reference(
                    artifact_id,
                    ReferenceCategory.FORM,
                    Path(record[3]),
                    image.mime,
                    len(image.data),
                    image.sha256,
                    task_id=task_id,
                )
            ]
            frozen = [image.data]
            for index, item in enumerate(artwork):
                try:
                    if item["category"] != "artwork":
                        raise ValueError("category")
                    value = read_reference(
                        root, Path(item["path"]), item["mime"], budget
                    )
                    refs.append(
                        Reference(
                            item["ref_id"],
                            ReferenceCategory.ARTWORK,
                            Path(item["path"]),
                            value.mime,
                            len(value.data),
                            value.sha256,
                            task_id=task_id,
                            view=item.get("view"),
                            region=item.get("region"),
                            version=item.get("version"),
                            importance=item.get("importance"),
                            purpose=item.get("purpose"),
                            observations=_observations(item.get("observations")),
                        )
                    )
                    frozen.append(value.data)
                except (KeyError, ValueError, OSError):
                    raise PreparationError(
                        f"Invalid artwork at index {index}"
                    ) from None
            if len({ref.ref_id for ref in refs}) != len(refs):
                raise PreparationError("Duplicate reference identifier")
            input_digest = digest_references(refs, task[0])
            latest_revision = db.execute(
                "SELECT MAX(revision) FROM revision WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            needs_revision = not _preserved_inputs(
                db, task_id, latest_revision, refs, {"artwork"}
            )
            if _revision_decision_id and not needs_revision:
                raise PreparationError(
                    "Input revision decision does not match a change"
                )
            if needs_revision:
                from ecommerce_product_image_creator.input_revision import (
                    check_input_revision_locked,
                )

                check_input_revision_locked(
                    db,
                    store,
                    _revision_decision_id,
                    task_id,
                    int(latest_revision),
                    json.loads(_input_context(refs, task[0]))["references"],
                )
            if (
                not needs_revision
                and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subset_proposal'"
                ).fetchone()
            ):
                proposals = db.execute(
                    """SELECT p.candidates FROM subset_proposal p
                       JOIN prepared_execution e ON e.task_id=p.task_id AND e.revision=p.revision
                       LEFT JOIN subset_decision d ON d.proposal_id=p.proposal_id
                       WHERE p.task_id=? AND e.stage='mock' AND d.target_revision IS NULL""",
                    (task_id,),
                ).fetchall()
                present = {ref.ref_id for ref in refs}
                if any(
                    not set(json.loads(row[0])).issubset(present) for row in proposals
                ):
                    raise PreparationError(
                        "Subset decision must be applied through its checked revision"
                    )
            revision = int(latest_revision) + 1
            prompt_revision = PromptRevision.from_text(
                task_id, revision, revision, "mock", prompt
            )
            scope = OperationScope("generate", None, "six-view-composite", 3)
            snapshot = ExecutionSnapshot.create(
                Revision(task_id, revision, input_digest),
                prompt_revision,
                "mock",
                model,
                endpoint,
                grouping,
                references=tuple((ref.ref_id, ref.sha256) for ref in refs),
                operation_scope=scope.digest(),
            )
            db.execute(
                "UPDATE task SET input_digest=?,status='awaiting_confirmation' WHERE task_id=?",
                (input_digest, task_id),
            )
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_eligibility'"
            ).fetchone():
                db.execute(
                    "UPDATE local_eligibility SET invalidated=1 WHERE task_id=? AND consumed=0",
                    (task_id,),
                )
            db.execute(
                "INSERT INTO revision VALUES(?,?,?)", (task_id, revision, input_digest)
            )
            db.execute(
                "INSERT INTO prepared_execution VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    revision,
                    "mock",
                    prompt,
                    prompt_revision.prompt_digest,
                    snapshot.execution_digest,
                    model,
                    endpoint,
                    grouping,
                ),
            )
            db.execute(
                "INSERT INTO prepared_snapshot_v2 VALUES(?,?,?)",
                (task_id, revision, snapshot.to_payload()),
            )
            db.execute(
                "INSERT INTO prepared_input_v2 VALUES(?,?,?)",
                (task_id, revision, _input_context(refs, task[0])),
            )
            inherited = db.execute(
                "SELECT reported FROM prepared_conflicts WHERE task_id=? AND revision=?",
                (task_id, record[0]),
            ).fetchone()
            prior = json.loads(inherited[0]) if inherited else []
            reported_conflicts = list(dict.fromkeys([*prior, *reported_conflicts]))
            db.execute(
                "INSERT INTO prepared_conflicts VALUES(?,?,?)",
                (task_id, revision, json.dumps(reported_conflicts, ensure_ascii=False)),
            )
            db.execute(
                "INSERT INTO prepared_scope_v2 VALUES(?,?,?)",
                (
                    task_id,
                    revision,
                    json.dumps(
                        {
                            "operation": scope.operation,
                            "target_view": scope.target_view,
                            "output": scope.output,
                            "max_attempts": scope.max_attempts,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            db.execute(
                "INSERT INTO prepared_mock_source VALUES(?,?,?,?,?,?)",
                (task_id, revision, record[0], artifact_id, review_id, record[1]),
            )
            for index, (ref, data) in enumerate(zip(refs, frozen, strict=True)):
                db.execute(
                    "INSERT INTO prepared_reference VALUES(?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        revision,
                        index,
                        ref.ref_id,
                        ref.category.value,
                        ref.mime,
                        ref.sha256,
                        data,
                    ),
                )
            if needs_revision:
                changed = db.execute(
                    "UPDATE input_revision_decision SET target_revision=?,target_execution_digest=? WHERE decision_id=? AND target_revision IS NULL AND invalidated=0",
                    (revision, snapshot.execution_digest, _revision_decision_id),
                )
                if changed.rowcount != 1:
                    raise PreparationError(
                        "Input revision decision is already consumed"
                    )
        result = {
            "task_id": task_id,
            "revision": revision,
            "stage": "mock",
            "input_digest": input_digest,
            "prompt_revision": revision,
            "prompt_digest": prompt_revision.prompt_digest,
            "execution_digest": snapshot.execution_digest,
            "model": model,
            "endpoint": endpoint,
            "group": grouping,
            "output": "six-view-composite",
            "operation_scope": {
                "operation": scope.operation,
                "target_view": scope.target_view,
                "output": scope.output,
                "max_attempts": scope.max_attempts,
            },
            "stage_references": [ref.ref_id for ref in refs],
            "form_revision": record[0],
            "review_id": review_id,
            "authorization": "unverified",
            "cost": "unknown",
            "duration": "unknown",
            "pending": ["business_authorization"],
        }
        result.update(_response_details(store, task_id, revision))
        if result["stage_context"]["prompt_semantic_conflicts"]:
            result["pending"].append("prompt_brand_clarification")
        if result["structure_conflicts"]["reported"]:
            result["pending"].append("structure_clarification")
        if result["packaging_versions"]["status"] == "multiple_declared_versions":
            result["pending"].append("packaging_version_confirmation")
        return result
    except (sqlite3.Error, OSError, FileSafetyError):
        raise PreparationError("Unable to prepare mock safely") from None
