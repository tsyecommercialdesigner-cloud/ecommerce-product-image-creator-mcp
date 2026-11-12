from PIL import Image

from ecommerce_product_image_creator.preparation import read_prepared
from ecommerce_product_image_creator.storage import Store


def test_observation_input_through_mcp_sdk(tmp_path):
    import asyncio
    import sys

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    root = tmp_path / "input"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    refs = []
    for name in ("artwork", "form"):
        path = root / (name + ".png")
        Image.new("RGB", (3, 3)).save(path)
        refs.append(
            {"ref_id": name, "category": name, "path": str(path), "mime": "image/png"}
        )
    refs[1]["observations"] = [{"subject": "bottom", "status": "unknown"}]
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ecommerce_product_image_creator.server"],
            env=env,
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(
                "prepare_product_image",
                {
                    "references": refs,
                    "instructions": "request",
                    "prompt": "prompt",
                    "stage": "form",
                },
            )
            assert not result.is_error and result.structured_content is not None
            stage_context = result.structured_content["stage_context"]
            assert stage_context["unobserved_structure"]["bottom"] == "unknown"
            assert stage_context["references"][0]["ref_id"] == "form"
            recovered = await session.call_tool(
                "prepare_product_image",
                {
                    "task_id": result.structured_content["task_id"],
                    "recovery_revision": 1,
                },
            )
            assert not recovered.is_error and recovered.structured_content is not None
            assert recovered.structured_content["stage_context"] == stage_context
            conflict = await session.call_tool(
                "prepare_product_image",
                {
                    "references": refs,
                    "instructions": "request",
                    "prompt": "Use competitor brand ORBIT as the target brand.",
                    "stage": "form",
                    "task_id": result.structured_content["task_id"],
                },
            )
            assert not conflict.is_error and conflict.structured_content is not None
            assert (
                conflict.structured_content["stage_context"][
                    "prompt_semantic_consistency"
                ]
                == "conflict"
            )
            assert (
                "prompt_brand_clarification" in conflict.structured_content["pending"]
            )
            saved = read_prepared(
                Store(state / "tasks.sqlite"), result.structured_content["task_id"], 1
            )
            assert saved["references"][1]["observations"][0]["status"] == "unknown"

    asyncio.run(run())
