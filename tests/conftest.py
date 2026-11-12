"""Propagate the optional CI-only guard through the MCP SDK's filtered environment."""

import os
import sys

import pytest


@pytest.fixture(autouse=True)
def _propagate_ci_guard(monkeypatch):
    if os.environ.get("CI_NETWORK_GUARD") != "1":
        return
    from mcp.client import stdio

    guard = sys.modules.get("sitecustomize")
    assert guard is not None and guard.__file__ and "ci_guard" in guard.__file__
    assert os.environ.get("PYTHONPATH")
    original = stdio.get_default_environment

    def guarded_environment():
        return original() | {
            "PYTHONPATH": os.environ["PYTHONPATH"],
            "CI_NETWORK_GUARD": "1",
        }

    monkeypatch.setattr(stdio, "get_default_environment", guarded_environment)
