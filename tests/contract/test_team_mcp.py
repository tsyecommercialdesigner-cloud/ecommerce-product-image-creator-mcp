"""Team submission through the SDK, SQLite and an isolated HTTPS replacement."""

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image


@pytest.mark.parametrize(
    "outcome", ["accepted", "default", "timeout", "off", "missing"]
)
def test_team_mcp_one_post_and_replay(tmp_path: Path, outcome: str):
    root, state = tmp_path / "input", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    refs = []
    for category in ("form", "artwork"):
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
    calls = state / "calls"
    code = """
import sys
from pathlib import Path
from ecommerce_product_image_creator import config, team_transport
def fixture_config():
    if sys.argv[2] == "missing": raise config.ConfigError("fixture credential missing")
    return config.Config("fake-credential")
config.load_config = fixture_config
class Response:
    status = 200
    def read(self, n): return b'{"task_id":"fake-upstream"}'
class Connection:
    def request(self, method, path, **kwargs):
        with open(sys.argv[1], "ab") as f: f.write(b"1")
        Path(sys.argv[1] + ".body").write_bytes(kwargs["body"])
        if sys.argv[2] == "timeout": raise TimeoutError("fixture")
    def getresponse(self): return Response()
    def close(self): pass
team_transport._open_connection = lambda *args: Connection()
from ecommerce_product_image_creator.server import main
main()
"""
    env = {
        "TEAM_MODE": "true",
        "ENABLE_REAL_GENERATION": "true",
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }
    if outcome == "off":
        env["TEAM_MODE"] = "false"
    elif outcome == "default":
        del env["TEAM_MODE"]

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-c", code, str(calls), outcome], env=env
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            prep = await session.call_tool(
                "prepare_product_image",
                {
                    "references": refs,
                    "instructions": "offline test",
                    "prompt": "F L R B T Q",
                    "stage": "form",
                },
            )
            assert not prep.is_error and prep.structured_content
            prepared = prep.structured_content
            frozen = (root / "form.png").read_bytes()
            (root / "form.png").write_bytes(b"changed after prepare")
            args = {
                k: prepared[k]
                for k in (
                    "task_id",
                    "revision",
                    "stage",
                    "input_digest",
                    "prompt_revision",
                    "prompt_digest",
                    "execution_digest",
                )
            }
            args["operation_id"] = "team-operation"
            first = await session.call_tool("submit_product_image", args)
            if outcome in {"off", "missing"}:
                assert first.is_error
                assert not calls.exists()
                return
            assert not first.is_error, first
            assert first.structured_content
            assert first.structured_content["state"] == (
                "unknown" if outcome == "timeout" else "accepted"
            )
            again = await session.call_tool("submit_product_image", args)
            assert not again.is_error
            another = await session.call_tool(
                "submit_product_image", {**args, "operation_id": "another-operation"}
            )
            assert another.is_error
            assert calls.read_bytes() == b"1"
            body = Path(str(calls) + ".body").read_bytes()
            assert b"gpt-image-2.5-sunburst" in body and b"F L R B T Q" in body
            assert frozen in body and b"changed after prepare" not in body

    asyncio.run(run())
    with sqlite3.connect(state / "tasks.sqlite") as db:
        if outcome in {"off", "missing"}:
            assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
            return
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (1,)
        row = db.execute("SELECT upstream_status,upstream_id FROM attempt").fetchone()
        assert row == (
            ("unknown", None) if outcome == "timeout" else ("accepted", "fake-upstream")
        )
