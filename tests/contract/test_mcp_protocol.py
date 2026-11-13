import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError
from PIL import Image


def test_stdio_sdk_initialization_discovery_and_errors(tmp_path: Path) -> None:
    async def run() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-c",
                "from ecommerce_product_image_creator.server import server\ndef probe(fail: bool=False) -> str:\n if fail: raise ValueError('Probe failed')\n return 'phase2a-ok'\nserver.add_tool(probe,name='phase2a_probe')\nserver.run(transport='stdio')",
            ],
            env={"ZHENZHEN_GPTIMAGEGEN_API_KEY": "real-secret-test"},
        )
        error_path = tmp_path / "stderr.log"
        with error_path.open("w", encoding="utf-8") as errors:
            async with (
                stdio_client(params, errlog=errors) as (read, write),
                ClientSession(read, write) as session,
            ):
                info = await session.initialize()
                assert info.protocol_version
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "phase2a_probe",
                    "prepare_product_image",
                    "get_product_image_task",
                    "get_product_image_file",
                    "review_form",
                    "confirm_mock_inputs",
                    "submit_product_image",
                }
                good = await session.call_tool("phase2a_probe", {"fail": False})
                assert isinstance(good, types.CallToolResult)
                assert good.is_error is False
                bad = await session.call_tool("phase2a_probe", {"fail": True})
                assert isinstance(bad, types.CallToolResult)
                assert bad.is_error is True
                invalid = await session.call_tool(
                    "phase2a_probe", {"fail": "real-secret-test"}
                )
                assert isinstance(invalid, types.CallToolResult)
                assert invalid.is_error is True
                with pytest.raises(MCPError):
                    await session.send_request(
                        types.Request(method="invalid/method", params=None),
                        types.EmptyResult,
                    )
        logged = error_path.read_text(encoding="utf-8")
        assert "real-secret-test" not in logged
        assert "phase2a_probe" in logged
        assert "Probe failed" not in logged

    asyncio.run(run())


def test_stdio_business_schemas_calls_and_read_only_boundaries(tmp_path: Path) -> None:
    """Exercise the locked client against an independent default-deny server."""
    root, state = tmp_path / "input", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    refs = []
    for category in ("artwork", "form"):
        path = root / f"{category}.png"
        Image.new("RGB", (4, 4), "red").save(path)
        refs.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(path),
                "mime": "image/png",
            }
        )
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }

    async def run() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ecommerce_product_image_creator.server"],
            env=env,
        )
        stderr = tmp_path / "server-stderr.log"
        with stderr.open("w", encoding="utf-8") as errors:
            async with (
                stdio_client(params, errlog=errors) as (r, w),
                ClientSession(r, w) as client,
            ):
                init = await client.initialize()
                assert (
                    init.protocol_version
                    and init.server_info.name == "ecommerce-product-image-creator"
                )
                listing = await client.list_tools()
                tools = {item.name: item for item in listing.tools}
                assert set(tools) == {
                    "prepare_product_image",
                    "submit_product_image",
                    "get_product_image_task",
                    "get_product_image_file",
                    "review_form",
                    "confirm_mock_inputs",
                }
                assert all(
                    tools[name].description
                    and tools[name].input_schema["type"] == "object"
                    for name in tools
                )
                for name in (
                    "prepare_product_image",
                    "submit_product_image",
                    "get_product_image_task",
                ):
                    assert tools[name].output_schema == {
                        "additionalProperties": True,
                        "title": f"{name}DictOutput",
                        "type": "object",
                    }
                prepare_schema = tools["prepare_product_image"].input_schema
                submit_schema = tools["submit_product_image"].input_schema
                query_schema = tools["get_product_image_task"].input_schema
                assert set(prepare_schema["properties"]) == {
                    "references",
                    "instructions",
                    "prompt",
                    "stage",
                    "model",
                    "endpoint",
                    "grouping",
                    "task_id",
                    "artifact_id",
                    "review_id",
                    "structure_conflicts",
                    "recovery_revision",
                    "revision_decision_id",
                }
                assert not prepare_schema.get("required")
                assert (
                    prepare_schema["properties"]["model"]["default"]
                    == "gpt-image-2.5-sunburst"
                )
                assert prepare_schema["properties"]["grouping"]["default"] == "reverse"
                assert prepare_schema["properties"]["endpoint"]["default"].endswith(
                    "/v1/images/edits?async=true"
                )
                reference_array = next(
                    x
                    for x in prepare_schema["properties"]["references"]["anyOf"]
                    if x["type"] == "array"
                )
                assert reference_array["items"]["type"] == "object"
                assert reference_array["items"]["additionalProperties"] is True
                assert set(submit_schema["properties"]) == {
                    "task_id",
                    "revision",
                    "input_digest",
                    "stage",
                    "prompt_revision",
                    "prompt_digest",
                    "execution_digest",
                    "operation_id",
                    "form_revision",
                    "form_artifact_digest",
                }
                assert set(query_schema["properties"]) == {
                    "task_id",
                    "attempt_id",
                    "stage",
                    "revision",
                    "artifact_id",
                    "refresh_remote",
                }
                assert query_schema["properties"]["refresh_remote"]["default"] is False
                assert all(
                    schema.get("additionalProperties", True)
                    for schema in (prepare_schema, submit_schema, query_schema)
                )
                assert {
                    x["type"] for x in prepare_schema["properties"]["stage"]["anyOf"]
                } == {"string", "null"}
                assert all(
                    "enum" not in x
                    for x in prepare_schema["properties"]["stage"]["anyOf"]
                )
                assert {
                    x["type"] for x in query_schema["properties"]["revision"]["anyOf"]
                } == {"integer", "null"}
                assert {"references", "instructions", "prompt", "stage"} <= set(
                    prepare_schema["properties"]
                )
                assert {
                    "task_id",
                    "revision",
                    "input_digest",
                    "stage",
                    "prompt_revision",
                    "prompt_digest",
                    "execution_digest",
                    "operation_id",
                } <= set(submit_schema["required"])
                assert query_schema["required"] == ["task_id"]
                assert submit_schema["properties"]["revision"]["type"] == "integer"
                assert submit_schema["properties"]["task_id"]["type"] == "string"
                assert submit_schema["properties"]["stage"]["type"] == "string"
                assert "enum" not in submit_schema["properties"]["stage"]
                assert {
                    item["type"]
                    for item in prepare_schema["properties"]["references"]["anyOf"]
                } == {"array", "null"}
                request = {
                    "references": refs,
                    "instructions": "create",
                    "prompt": "F L R B T Q",
                    "stage": "form",
                }
                prepared = await client.call_tool("prepare_product_image", request)
                assert not prepared.is_error and isinstance(
                    prepared.structured_content, dict
                )
                result = prepared.structured_content
                assert (
                    result["stage"] == "form"
                    and result["authorization"] == "unverified"
                )
                assert (
                    result["output"] == "six-view-composite"
                    and result["cost"] == "unknown"
                )
                assert prepared.content and isinstance(
                    prepared.content[0], types.TextContent
                )
                assert json.loads(prepared.content[0].text) == result
                task_id = result["task_id"]
                read = await client.call_tool(
                    "get_product_image_task", {"task_id": task_id}
                )
                assert not read.is_error and read.structured_content is not None
                assert read.structured_content["task_id"] == task_id
                refresh = await client.call_tool(
                    "get_product_image_task",
                    {"task_id": task_id, "refresh_remote": True},
                )
                assert not refresh.is_error and refresh.structured_content is not None
                assert (
                    refresh.structured_content["team_reconciliation"]
                    == "team_refresh_disabled"
                )
                with sqlite3.connect(state / "tasks.sqlite") as db:
                    before = db.execute("SELECT count(*) FROM attempt").fetchone()
                for invalid in (
                    {**request, "stage": "bad-stage"},
                    {"instructions": "create", "prompt": "F", "stage": "form"},
                    {**request, "references": "not-a-list"},
                    {**request, "references": []},
                    {
                        "stage": "mock",
                        "task_id": task_id,
                        "artifact_id": "untrusted",
                        "review_id": "untrusted",
                        "references": [refs[0]],
                        "instructions": "create",
                        "prompt": "F",
                    },
                ):
                    response = await client.call_tool("prepare_product_image", invalid)
                    assert response.is_error is True
                assert (
                    await client.call_tool(
                        "get_product_image_task", {"task_id": "not-a-task"}
                    )
                ).is_error is True
                assert (
                    await client.call_tool("get_product_image_task", {})
                ).is_error is True
                invalid_type = await client.call_tool(
                    "get_product_image_task", {"task_id": ["isolated-sensitive-marker"]}
                )
                assert invalid_type.is_error is True
                assert "Invalid tool arguments" in invalid_type.model_dump_json()
                assert "isolated-sensitive-marker" not in invalid_type.model_dump_json()
                request_submit = {
                    name: result[name]
                    for name in (
                        "task_id",
                        "revision",
                        "input_digest",
                        "stage",
                        "prompt_revision",
                        "prompt_digest",
                        "execution_digest",
                    )
                }
                request_submit["operation_id"] = "unapproved-operation"
                rejected = await client.call_tool(
                    "submit_product_image", request_submit
                )
                assert rejected.is_error is True
                replay = await client.call_tool(
                    "submit_product_image", {**request_submit, "approved": True}
                )
                assert replay.is_error is True
                assert (
                    await client.call_tool(
                        "submit_product_image",
                        {**request_submit, "revision": "not-an-integer"},
                    )
                ).is_error is True
                with sqlite3.connect(state / "tasks.sqlite") as db:
                    assert (
                        db.execute("SELECT count(*) FROM attempt").fetchone()
                        == before
                        == (0,)
                    )
                with pytest.raises(MCPError):
                    await client.send_request(
                        types.Request(method="invalid/method", params=None),
                        types.EmptyResult,
                    )
        stderr_text = stderr.read_text(encoding="utf-8")
        assert "real-secret-test" not in stderr_text
        assert "isolated-sensitive-marker" not in stderr_text

    asyncio.run(run())


def test_stdio_raw_stdout_protocol_and_eof_shutdown(tmp_path: Path) -> None:
    """Capture the actual subprocess streams without an SDK parser hiding bytes."""
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "offline-contract", "version": "1"},
        },
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ecommerce_product_image_creator.server"],
        input=json.dumps(init) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    frames = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert frames and all(item.get("jsonrpc") == "2.0" for item in frames)
    assert any(item.get("id") == 1 and "result" in item for item in frames)
    assert "Traceback" not in proc.stderr


def test_sdk_cancel_pending_probe_and_session_remains_usable(tmp_path: Path) -> None:
    """An isolated async replacement makes cancellation observable, not billable."""
    stopped = tmp_path / "cancel-observed.txt"
    child = r"""
import asyncio,sys
from pathlib import Path
from mcp.server.mcpserver import Context
from ecommerce_product_image_creator.server import server,main
async def slow_probe(ctx: Context) -> str:
    await ctx.report_progress(1, 2)
    try:
        await asyncio.Event().wait()
    finally:
        Path(sys.argv[1]).write_text('cancelled', encoding='utf-8')
server.add_tool(slow_probe, name='phase2a_probe')
main()
"""

    async def run() -> None:
        started = asyncio.Event()

        async def progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            del progress, total, message
            started.set()

        params = StdioServerParameters(
            command=sys.executable, args=["-c", child, str(stopped)]
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as client:
            await client.initialize()
            pending = asyncio.create_task(
                client.call_tool("phase2a_probe", {}, progress_callback=progress)
            )
            await asyncio.wait_for(started.wait(), timeout=15)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

            async def await_stopped() -> None:
                while not stopped.exists():
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(await_stopped(), timeout=15)
            assert stopped.read_text(encoding="utf-8") == "cancelled"
            assert len((await client.list_tools()).tools) == 7

    asyncio.run(run())


def test_stdio_unexpected_process_exit_is_observable() -> None:
    """Drive real stdio, then crash only the isolated protocol probe process."""
    child = r"""
import os
from ecommerce_product_image_creator.server import server,main
def crash_probe() -> str:
    os._exit(17)
server.add_tool(crash_probe, name='phase2a_probe')
main()
"""

    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            child,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert (
            proc.stdin is not None
            and proc.stdout is not None
            and proc.stderr is not None
        )
        try:
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "isolated-crash", "version": "1"},
                },
            }
            proc.stdin.write((json.dumps(init) + "\n").encode())
            await proc.stdin.drain()
            reply = json.loads(await asyncio.wait_for(proc.stdout.readline(), 15))
            assert reply["id"] == 1 and "result" in reply
            proc.stdin.write(
                (
                    json.dumps(
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}
                    )
                    + "\n"
                ).encode()
            )
            proc.stdin.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "phase2a_probe", "arguments": {}},
                        }
                    )
                    + "\n"
                ).encode()
            )
            await proc.stdin.drain()
            assert await asyncio.wait_for(proc.wait(), 15) == 17
            remaining = (await proc.stdout.read()).decode("utf-8")
            assert all(
                json.loads(line).get("jsonrpc") == "2.0"
                for line in remaining.splitlines()
                if line.strip()
            )
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    asyncio.run(run())


def test_unexpected_tool_failure_does_not_leak_exception_details(
    tmp_path: Path,
) -> None:
    child = r"""
from ecommerce_product_image_creator.server import server,main
def failing_probe() -> str:
    raise RuntimeError('isolated-exception-secret-marker')
server.add_tool(failing_probe, name='phase2a_probe')
main()
"""

    async def run() -> None:
        params = StdioServerParameters(command=sys.executable, args=["-c", child])
        log = tmp_path / "unexpected-stderr.txt"
        with log.open("w", encoding="utf-8") as errors:
            async with (
                stdio_client(params, errlog=errors) as (r, w),
                ClientSession(r, w) as client,
            ):
                await client.initialize()
                response = await client.call_tool("phase2a_probe", {})
                assert response.is_error is True
                assert (
                    "isolated-exception-secret-marker" not in response.model_dump_json()
                )
        assert "isolated-exception-secret-marker" not in log.read_text(encoding="utf-8")

    asyncio.run(run())
