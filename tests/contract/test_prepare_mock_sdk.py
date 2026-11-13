import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from ecommerce_product_image_creator.preparation import (
    prepare_form,
    read_prepare_response,
    read_prepared,
)
from ecommerce_product_image_creator.review import FormReview, save_review
from ecommerce_product_image_creator.storage import ResourceBudget, Store


def test_mock_prepare_through_sdk_with_test_only_verifier(tmp_path: Path, monkeypatch):
    from unit.revision_fixtures import confirm_fixture_revision

    root = tmp_path / "input"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    refs = []
    for category in ("artwork", "form"):
        path = root / (category + ".png")
        Image.new("RGB", (3, 3)).save(path)
        refs.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(path),
                "mime": "image/png",
            }
        )
    store = Store(state / "tasks.sqlite")
    form = prepare_form(
        store,
        root,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "form prompt",
        "model",
        "endpoint",
        "group",
        structure_conflicts=["seal differs"],
    )
    store.claim_operation("op", form["task_id"], 1, "digest", "attempt")
    artifact = state / "form.png"
    Image.new("RGB", (4, 4), "green").save(artifact)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded', delivery_status='delivered' WHERE attempt_id='attempt'"
        )
        db.execute(
            "INSERT INTO artifact VALUES(?,?,?,?)",
            ("artifact", "attempt", str(artifact), digest),
        )
    save_review(
        store,
        FormReview(
            "review",
            form["task_id"],
            1,
            "artifact",
            digest,
            "accept",
            "test-only",
            time.time(),
            None,
        ),
    )
    command = "import ecommerce_product_image_creator.preparation as p; p._review_is_trusted=lambda *args: True; import ecommerce_product_image_creator.input_revision as i; i._source_is_trusted=lambda *args: True; from ecommerce_product_image_creator.server import main; main()"
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }
    args = {
        "references": [{**refs[0], "purpose": "package front", "version": "v1"}],
        "instructions": "request",
        "prompt": "mock prompt",
        "stage": "mock",
        "task_id": form["task_id"],
        "artifact_id": "artifact",
        "review_id": "review",
    }
    budget = ResourceBudget(100000, 10000, 100000)
    args["revision_decision_id"] = confirm_fixture_revision(
        store,
        root,
        budget,
        form["task_id"],
        args["references"],
        monkeypatch,
        artifact_id="artifact",
        artifact_root=state,
    )
    first_output = {}

    async def run():
        async with (
            stdio_client(
                StdioServerParameters(
                    command=sys.executable, args=["-c", command], env=env
                )
            ) as (r, w),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            result = await session.call_tool("prepare_product_image", args)
            assert result.is_error is False
            assert result.structured_content is not None
            output = result.structured_content
            first_output.update(output)
            assert output["authorization"] == "unverified"
            assert output["references_summary"]["registered"]["form"]["ids"] == ["form"]
            assert output["references_summary"]["selected"]["form"]["ids"] == [
                "artifact"
            ]
            assert output["references_summary"]["not_selected"]["form"]["ids"] == [
                "form"
            ]
            assert output["packaging_versions"]["references"] == [
                {"ref_id": "artwork", "version": "v1"}
            ]
            assert output["packaging_versions"]["confirmation"] == "unverified"
            assert output["structure_conflicts"]["status"] == "pending_clarification"
            assert output["structure_conflicts"]["reported"] == ["seal differs"]
            assert "structure_clarification" in output["pending"]
            assert (
                read_prepare_response(store, form["task_id"], output["revision"])
                == output
            )
            code = "import json,sys; from pathlib import Path; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.preparation import read_prepare_response; print(json.dumps(read_prepare_response(Store(Path(sys.argv[1])),sys.argv[2],int(sys.argv[3]))))"
            child = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-c",
                    code,
                    str(state / "tasks.sqlite"),
                    form["task_id"],
                    str(output["revision"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            assert json.loads(child.stdout) == output
            recovered = read_prepared(store, form["task_id"], output["revision"])
            assert recovered["stage_references"] == ["artifact", "artwork"]
            assert recovered["form_source"]["sha256"] == digest
            assert recovered["digest_version"] == 2
            assert recovered["prompt"] == "mock prompt"
            assert recovered["references"][1]["purpose"] == "package front"
            assert recovered["references"][1]["version"] == "v1"
            extra_path = root / "extra.png"
            Image.new("RGB", (3, 3), "yellow").save(extra_path)
            extra = {
                "ref_id": "extra",
                "category": "artwork",
                "path": str(extra_path),
                "mime": "image/png",
            }
            two_decision = confirm_fixture_revision(
                store,
                root,
                budget,
                form["task_id"],
                [refs[0], extra],
                monkeypatch,
                artifact_id="artifact",
                artifact_root=state,
            )
            two_artworks = await session.call_tool(
                "prepare_product_image",
                {
                    **args,
                    "references": [refs[0], extra],
                    "revision_decision_id": two_decision,
                },
            )
            assert two_artworks.is_error is False
            assert two_artworks.structured_content is not None
            assert two_artworks.structured_content["stage_references"] == [
                "artifact",
                "artwork",
                "extra",
            ]
            assert (
                two_artworks.structured_content["references_summary"]["selected"][
                    "artwork"
                ]["count"]
                == 2
            )
            next_decision = confirm_fixture_revision(
                store,
                root,
                budget,
                form["task_id"],
                args["references"],
                monkeypatch,
                artifact_id="artifact",
                artifact_root=state,
            )
            next_result = await session.call_tool(
                "prepare_product_image", {**args, "revision_decision_id": next_decision}
            )
            assert next_result.is_error is False
            assert next_result.structured_content is not None
            next_revision = next_result.structured_content["revision"]
            assert next_revision != output["revision"]
            assert (
                read_prepared(store, form["task_id"], output["revision"])["form_source"]
                == recovered["form_source"]
            )
            bad = await session.call_tool(
                "prepare_product_image", {**args, "references": [refs[0], refs[1]]}
            )
            assert bad.is_error is True
            missing = await session.call_tool(
                "prepare_product_image", {**args, "review_id": "absent"}
            )
            assert missing.is_error is True
            wrong_artifact = await session.call_tool(
                "prepare_product_image", {**args, "artifact_id": "other"}
            )
            assert wrong_artifact.is_error is True
            wrong_task = await session.call_tool(
                "prepare_product_image", {**args, "task_id": "other"}
            )
            assert wrong_task.is_error is True
            for decision in ("reject", "pending"):
                with store._connect() as db:
                    db.execute(
                        "UPDATE form_review_detail SET decision=? WHERE review_id='review'",
                        (decision,),
                    )
                denied = await session.call_tool("prepare_product_image", args)
                assert denied.is_error is True
            with store._connect() as db:
                db.execute(
                    "UPDATE form_review_detail SET decision='accept', expires_at=? WHERE review_id='review'",
                    (time.time() - 1,),
                )
            assert (
                await session.call_tool("prepare_product_image", args)
            ).is_error is True
            with store._connect() as db:
                db.execute(
                    "UPDATE form_review_detail SET expires_at=NULL, invalidation_reason='withdrawn' WHERE review_id='review'"
                )
            assert (
                await session.call_tool("prepare_product_image", args)
            ).is_error is True
            with store._connect() as db:
                db.execute(
                    "UPDATE form_review_detail SET invalidation_reason=NULL WHERE review_id='review'"
                )
            original = artifact.read_bytes()
            Image.new("RGB", (4, 4), "red").save(artifact)
            assert (
                await session.call_tool("prepare_product_image", args)
            ).is_error is True
            artifact.write_bytes(original)

        async with (
            stdio_client(
                StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "ecommerce_product_image_creator.server"],
                    env=env,
                )
            ) as (r, w),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            denied = await session.call_tool("prepare_product_image", args)
            assert denied.is_error is True
            assert "unverified" in str(denied.content)
            recovered = await session.call_tool(
                "prepare_product_image",
                {
                    "task_id": form["task_id"],
                    "recovery_revision": first_output["revision"],
                },
            )
            assert recovered.is_error is False
            assert recovered.structured_content == first_output

    asyncio.run(run())
