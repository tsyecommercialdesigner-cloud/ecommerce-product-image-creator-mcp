"""Verify the CI-only guard in the parent, spawned worker, and MCP subprocess."""

import asyncio
import multiprocessing
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _probe(output):
    try:
        with socket.socket() as sock:
            sock.connect(("192.0.2.1", 443))
    except PermissionError as exc:
        output.send((socket.socket.connect.__module__, str(exc)))
    finally:
        output.close()


def test_parent_blocks_socket_and_dns_before_network():
    assert socket.socket.connect.__module__ == "sitecustomize"
    with socket.socket() as sock:
        with pytest.raises(PermissionError, match="ci_network_guard"):
            sock.connect(("192.0.2.1", 443))
        with pytest.raises(PermissionError, match="ci_network_guard"):
            sock.connect_ex(("192.0.2.1", 443))
    with pytest.raises(PermissionError, match="ci_network_guard"):
        socket.getaddrinfo("outside.invalid", 443)
    with pytest.raises(PermissionError, match="ci_network_guard"):
        socket.gethostbyname("outside.invalid")
    with pytest.raises(PermissionError, match="ci_network_guard"):
        socket.getnameinfo(("192.0.2.1", 443), 0)
    with (
        socket.socket(type=socket.SOCK_DGRAM) as sock,
        pytest.raises(PermissionError, match="ci_network_guard"),
    ):
        sock.sendto(b"test", ("192.0.2.1", 53))


def test_spawn_worker_inherits_guard():
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_probe, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(15)
        module, message = parent.recv()
        assert module == "sitecustomize" and "ci_network_guard" in message
        process.join(15)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(15)
        parent.close()


def test_mcp_server_subprocess_inherits_guard():
    code = """
import socket
from mcp.server import MCPServer
assert socket.socket.connect.__module__ == "sitecustomize"
try:
    socket.socket().connect(("192.0.2.1", 443))
except PermissionError as error:
    assert "ci_network_guard" in str(error)
else:
    raise AssertionError("guard missing")
MCPServer("guard-probe").run(transport="stdio")
"""

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-c", code])
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            result = await session.initialize()
            assert result.server_info.name == "guard-probe"

    asyncio.run(run())


def test_guard_preflight_detects_unprotected_python():
    command = [
        sys.executable,
        "-I",
        "-c",
        "import socket; assert socket.socket.connect.__module__ == 'sitecustomize'",
    ]
    result = subprocess.run(command, capture_output=True, check=False, timeout=15)
    assert result.returncode != 0


def test_guard_enabled_from_expected_directory():
    assert os.environ.get("CI_NETWORK_GUARD") == "1"
    guard = sys.modules.get("sitecustomize")
    assert guard is not None and guard.__file__
    assert Path(guard.__file__).parent.name == "ci_guard"
