import asyncio
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from ecommerce_product_image_creator.preparation import read_prepared
from ecommerce_product_image_creator.storage import Store


def test_prepare_tool_is_discoverable_and_never_authorizes(tmp_path: Path):
    root = tmp_path / "input"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    refs = []
    for category in ("artwork", "form"):
        path = root / (category + ".png")
        Image.new("RGB", (4, 4), "red").save(path)
        refs.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(path),
                "mime": "image/png",
            }
        )

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ecommerce_product_image_creator.server"],
            env={
                "PRODUCT_IMAGE_INPUT_ROOT": str(root),
                "PRODUCT_IMAGE_STATE_DIR": str(state),
                "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
                "PRODUCT_IMAGE_MAX_PIXELS": "10000",
                "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
            },
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "prepare_product_image" in {tool.name for tool in tools.tools}
            result = await session.call_tool(
                "prepare_product_image",
                {
                    "references": refs,
                    "instructions": "create",
                    "prompt": "F L R B T Q",
                    "stage": "form",
                    "structure_conflicts": ["conflicting seal"],
                },
            )
            assert result.is_error is False
            assert result.structured_content is not None
            assert result.structured_content["authorization"] == "unverified"
            output = result.structured_content
            assert output["stage"] == "form"
            assert output["stage_references"] == ["form"]
            assert output["output"] == "six-view-composite"
            assert "F/L/R/B/T/Q" in output["form_output_contract"][0]
            assert "top left" in output["form_output_contract"][1]
            assert "orthographic" in output["form_output_contract"][2]
            assert output["references_summary"]["selected"]["form"]["ids"] == ["form"]
            assert output["references_summary"]["not_selected"]["artwork"]["ids"] == [
                "artwork"
            ]
            assert output["structure_conflicts"]["status"] == "pending_clarification"
            assert output["structure_conflicts"]["confirmation"] == "unverified"
            saved = read_prepared(
                Store(state / "tasks.sqlite"), output["task_id"], output["revision"]
            )
            assert saved["digest_version"] == 2
            assert saved["prompt"] == "F L R B T Q"
            assert saved["reference_bytes"][1] == Path(refs[1]["path"]).read_bytes()
            denied = await session.call_tool(
                "prepare_product_image",
                {
                    "references": refs,
                    "instructions": "create",
                    "prompt": "F L R B T Q",
                    "stage": "mock",
                },
            )
            assert denied.is_error is True

    asyncio.run(run())
