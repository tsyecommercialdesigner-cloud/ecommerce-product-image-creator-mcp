"""Non-billable stdio server with form-stage preparation."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from pydantic import ValidationError

from ecommerce_product_image_creator import offline_submission
from ecommerce_product_image_creator.artifact_reader import (
    ArtifactReadError,
    read_artifact,
)
from ecommerce_product_image_creator.domain import (
    Confirmation,
    ExecutionSnapshot,
    PromptRevision,
    Reference,
    ReferenceCategory,
    Revision,
    Task,
    TaskStatus,
)
from ecommerce_product_image_creator.preparation import (
    PreparationError,
    _observations,
    prepare_form,
    prepare_mock,
    read_prepare_response,
    read_prepared,
)
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.task_view import ReadOnlyStore, read_task_view
from ecommerce_product_image_creator.tasks import LocalEligibility, OperationScope


class _SanitizedMCPServer(MCPServer):
    """Keep rejected argument values out of client-visible SDK errors."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as error:
            if isinstance(error, UnexpectedToolError):
                raise UnexpectedToolError("Tool execution failed") from None
            if isinstance(error.__cause__, ValidationError):
                raise ToolError("Invalid tool arguments") from None
            raise


server = _SanitizedMCPServer("ecommerce-product-image-creator", version="0.1.0")


@server.tool(
    name="submit_product_image",
    description="Submit a previously prepared first operation only with independently verified authorization.",
)
def submit_product_image(
    task_id: str,
    revision: int,
    input_digest: str,
    stage: str,
    prompt_revision: int,
    prompt_digest: str,
    execution_digest: str,
    operation_id: str,
    form_revision: int | None = None,
    form_artifact_digest: str | None = None,
) -> dict[str, Any]:
    """Recover immutable execution data; public inputs never establish trust."""
    try:
        root = Path(os.environ["PRODUCT_IMAGE_INPUT_ROOT"]).resolve(strict=True)
        state = Path(os.environ["PRODUCT_IMAGE_STATE_DIR"]).resolve(strict=True)
        if (
            not root.is_dir()
            or not state.is_dir()
            or root.is_relative_to(state)
            or state.is_relative_to(root)
            or not (state / "tasks.sqlite").is_file()
        ):
            raise ValueError("Invalid configuration")
        budget = ResourceBudget(
            int(os.environ["PRODUCT_IMAGE_MAX_INPUT_BYTES"]),
            int(os.environ["PRODUCT_IMAGE_MAX_PIXELS"]),
            int(os.environ["PRODUCT_IMAGE_MAX_DECODED_BYTES"]),
        )
        store = Store(state / "tasks.sqlite")
        saved = read_prepared(store, task_id, revision)
        if saved["digest_version"] != 2 or not saved["snapshot_payload"]:
            raise ValueError("Complete v2 snapshot required")
        snapshot = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
        if (
            snapshot.task_id,
            snapshot.revision,
            snapshot.input_digest,
            snapshot.stage,
            snapshot.prompt_revision,
            snapshot.prompt_digest,
            snapshot.execution_digest,
        ) != (
            task_id,
            revision,
            input_digest,
            stage,
            prompt_revision,
            prompt_digest,
            execution_digest,
        ):
            raise ValueError("Request does not match frozen execution")
        source = saved["form_source"]
        if stage == "mock":
            if not source or (source["form_revision"], source["sha256"]) != (
                form_revision,
                form_artifact_digest,
            ):
                raise ValueError("FORM_SOURCE_MISMATCH")
        elif (
            stage != "form"
            or form_revision is not None
            or form_artifact_digest is not None
        ):
            raise ValueError("Invalid stage source")
        with closing(store._connect()) as db:
            task_row = db.execute(
                "SELECT input_digest,instructions,status FROM task WHERE task_id=?",
                (task_id,),
            ).fetchone()
            latest = db.execute(
                "SELECT MAX(revision) FROM revision WHERE task_id=?", (task_id,)
            ).fetchone()
            eligibility = db.execute(
                "SELECT confirmation_id FROM local_eligibility WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            candidate = db.execute(
                "SELECT 1 FROM candidate_eligibility WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            retry = db.execute(
                "SELECT 1 FROM operation_attempt WHERE attempt_id=(SELECT attempt_id FROM operation WHERE operation_id=?) AND sequence<>1",
                (operation_id,),
            ).fetchone()
            risk_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_consumption'"
            ).fetchone()
            linked = (
                risk_table
                and db.execute(
                    "SELECT 1 FROM risk_consumption WHERE linked_operation=?",
                    (operation_id,),
                ).fetchone()
            )
        if latest and latest[0] != revision:
            raise ValueError("STALE_TASK_REVISION")
        if (
            not task_row
            or not latest
            or not eligibility
            or candidate
            or retry
            or linked
        ):
            raise ValueError("First-operation eligibility is unavailable")
        task = Task(task_id, task_row[0], task_row[1], TaskStatus(task_row[2]))
        rev = Revision(task_id, revision, input_digest)
        prompt = PromptRevision.from_text(
            task_id, revision, prompt_revision, stage, saved["prompt"]
        )
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
                task_id=task_id,
            )
            for item in saved["references"]
        ]
        scope = OperationScope(**saved["operation_scope"])
        confirmation = Confirmation(
            task_id,
            revision,
            input_digest,
            prompt_digest,
            prompt_revision,
            execution_digest,
            confirmation_id=eligibility[0],
        )
        service = LocalEligibility(store, root, budget)
        result = offline_submission.submit_offline(
            service,
            task,
            rev,
            refs,
            prompt,
            snapshot,
            confirmation,
            operation_id,
            scope,
            require_first_authorizations=True,
        )
        with closing(store._connect()) as db:
            boundary = db.execute(
                "SELECT phase FROM attempt_transport_evidence WHERE attempt_id=?",
                (result.attempt_id,),
            ).fetchone()
            upstream = db.execute(
                "SELECT upstream_id FROM attempt WHERE attempt_id=?",
                (result.attempt_id,),
            ).fetchone()
        return {
            "task_id": task_id,
            "revision": revision,
            "stage": stage,
            "prompt_revision": prompt_revision,
            "prompt_digest": prompt_digest,
            "execution_digest": execution_digest,
            "operation_id": operation_id,
            "attempt_id": result.attempt_id,
            "state": result.state,
            "upstream_id": upstream[0] if upstream else None,
            "intent_recorded": True,
            "replayed": result.replayed,
            "send_boundary": boundary[0] if boundary else "unknown",
            "delivery": result.local_delivery,
            "cost": "unknown",
            "human_action_required": result.state == "unknown",
        }
    except offline_submission.SubmissionError as error:
        if str(error) == "Prompt brand conflict requires clarification":
            raise ToolError("Prompt brand conflict requires clarification") from None
        if str(error) == "FORM_REVIEW_UNVERIFIED_OR_INVALID":
            raise ToolError("FORM_REVIEW_UNVERIFIED_OR_INVALID") from None
        if str(error) == "FORM_ARTIFACT_INVALID":
            raise ToolError("FORM_ARTIFACT_INVALID") from None
        if str(error) == "Business clarification is required":
            raise ToolError("BUSINESS_CLARIFICATION_REQUIRED") from None
        raise ToolError("Submission cannot be safely authorized or completed") from None
    except ValueError as error:
        if str(error) == "FORM_SOURCE_MISMATCH":
            raise ToolError("FORM_SOURCE_MISMATCH") from None
        if str(error) == "STALE_TASK_REVISION":
            raise ToolError("STALE_TASK_REVISION") from None
        raise ToolError("Submission cannot be safely authorized or completed") from None
    except (KeyError, OSError, TypeError, sqlite3.Error):
        raise ToolError("Submission cannot be safely authorized or completed") from None


@server.tool(name="phase2a_probe", description="Non-billable protocol test only.")
def phase2a_probe(fail: bool = False) -> str:
    if fail:
        raise ValueError("Probe failed")
    return "phase2a-ok"


@server.tool(
    name="get_product_image_task",
    description="Read local task history or inspect one locally registered artifact.",
)
def get_product_image_task(
    task_id: str,
    attempt_id: str | None = None,
    stage: str | None = None,
    revision: int | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """Read locally stored task evidence or one artifact; never call a provider."""
    single_artifact = any(x is not None for x in (stage, revision, artifact_id))
    if single_artifact and (
        stage not in ("form", "mock")
        or revision is None
        or not attempt_id
        or not artifact_id
    ):
        raise ToolError(
            "Single artifact query requires stage, revision, attempt ID and artifact ID"
        )
    try:
        root = Path(os.environ["PRODUCT_IMAGE_INPUT_ROOT"]).resolve(strict=True)
        state = Path(os.environ["PRODUCT_IMAGE_STATE_DIR"]).resolve(strict=True)
        if (
            not root.is_dir()
            or not state.is_dir()
            or root.is_relative_to(state)
            or state.is_relative_to(root)
            or not (state / "tasks.sqlite").is_file()
        ):
            raise ValueError("Invalid configuration")
        budget = ResourceBudget(
            int(os.environ["PRODUCT_IMAGE_MAX_INPUT_BYTES"]),
            int(os.environ["PRODUCT_IMAGE_MAX_PIXELS"]),
            int(os.environ["PRODUCT_IMAGE_MAX_DECODED_BYTES"]),
        )
        if not single_artifact:
            return read_task_view(
                ReadOnlyStore(state / "tasks.sqlite"),
                task_id,
                state,
                budget,
                attempt_id=attempt_id,
            )
        assert (
            stage in ("form", "mock")
            and revision is not None
            and attempt_id
            and artifact_id
        )
        return read_artifact(
            ReadOnlyStore(state / "tasks.sqlite"),
            task_id,
            stage,
            revision,
            attempt_id,
            artifact_id,
            state,
            budget,
            include_content=True,
        )
    except (ArtifactReadError, KeyError, OSError, ValueError, sqlite3.Error):
        raise ToolError("Artifact query could not be safely completed") from None


@server.tool(
    name="prepare_product_image",
    description="Prepare an offline stage snapshot; no image generation or authorization.",
)
def prepare_product_image(
    references: list[dict[str, Any]] | None = None,
    instructions: str | None = None,
    prompt: str | None = None,
    stage: str | None = None,
    model: str = "gpt-image-2.5-sunburst",
    endpoint: str = "https://ai.t8star.org/v1/images/edits?async=true",
    grouping: str = "reverse",
    task_id: str | None = None,
    artifact_id: str | None = None,
    review_id: str | None = None,
    structure_conflicts: list[str] | None = None,
    recovery_revision: int | None = None,
    revision_decision_id: str | None = None,
) -> dict[str, Any]:
    try:
        root = Path(os.environ["PRODUCT_IMAGE_INPUT_ROOT"]).resolve(strict=True)
        state = Path(os.environ["PRODUCT_IMAGE_STATE_DIR"]).resolve(strict=True)
        budget = ResourceBudget(
            int(os.environ["PRODUCT_IMAGE_MAX_INPUT_BYTES"]),
            int(os.environ["PRODUCT_IMAGE_MAX_PIXELS"]),
            int(os.environ["PRODUCT_IMAGE_MAX_DECODED_BYTES"]),
        )
        if (
            not root.is_dir()
            or not state.is_dir()
            or state.is_relative_to(root)
            or root.is_relative_to(state)
        ):
            raise ValueError("Invalid input and state directories")
        if recovery_revision is not None:
            if (
                not task_id
                or recovery_revision < 1
                or any(
                    x is not None
                    for x in (
                        references,
                        instructions,
                        prompt,
                        stage,
                        artifact_id,
                        review_id,
                        structure_conflicts,
                        revision_decision_id,
                    )
                )
            ):
                raise PreparationError("Recovery requires only task ID and revision")
            return read_prepare_response(
                Store(state / "tasks.sqlite"), task_id, recovery_revision
            )
        if (
            references is None
            or instructions is None
            or prompt is None
            or stage is None
        ):
            raise PreparationError(
                "Preparation requires references, instructions, prompt and stage"
            )
        if stage == "mock":
            if not task_id or not artifact_id or not review_id:
                raise PreparationError(
                    "Mock preparation requires a form artifact and review"
                )
            return prepare_mock(
                Store(state / "tasks.sqlite"),
                root,
                budget,
                task_id,
                artifact_id,
                review_id,
                references,
                prompt,
                model,
                endpoint,
                grouping,
                artifact_root=state,
                instructions=instructions,
                structure_conflicts=structure_conflicts,
                _revision_decision_id=revision_decision_id,
            )
        if stage != "form":
            raise PreparationError("Unsupported stage")
        return prepare_form(
            Store(state / "tasks.sqlite"),
            root,
            budget,
            references,
            instructions,
            prompt,
            model,
            endpoint,
            grouping,
            task_id=task_id,
            structure_conflicts=structure_conflicts,
            _revision_decision_id=revision_decision_id,
        )
    except PreparationError as error:
        raise ToolError(str(error)) from None
    except (KeyError, OSError, ValueError):
        raise ToolError("Preparation configuration or input is invalid") from None


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
