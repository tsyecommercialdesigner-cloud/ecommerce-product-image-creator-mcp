import time

import pytest
from PIL import Image

from ecommerce_product_image_creator import mock_workflow
from ecommerce_product_image_creator.artifact_reader import (
    ArtifactReadError,
    read_artifact,
)

from .test_mock_workflow import _registered_fixture


def test_form_mock_binding_and_review(tmp_path, monkeypatch):
    store, budget, form, mock, _path, digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    review = mock_workflow.MockReview(
        "mr",
        form["task_id"],
        mock["revision"],
        "mock-artifact",
        digest,
        "accept",
        "fixture",
        time.time(),
        None,
        view="F",
        feedback="detail",
    )
    mock_workflow.save_mock_review(store, review)
    f = read_artifact(
        store,
        form["task_id"],
        "form",
        1,
        "form-attempt",
        "form-artifact",
        tmp_path,
        budget,
    )
    m = read_artifact(
        store,
        form["task_id"],
        "mock",
        mock["revision"],
        "mock-attempt",
        "mock-artifact",
        tmp_path,
        budget,
    )
    assert f["file"]["state"] == "verified" and f["review"]["decision"] == "accept"
    assert f["review"]["trust_state"] == "unverified"
    assert (
        m["file"]["state"] == "verified"
        and m["form_source"]["artifact_id"] == "form-artifact"
    )
    assert m["packaging_versions"] == [{"ref_id": "artwork", "version": "v1"}]
    assert (
        m["review"]["decision"] == "accept"
        and m["review"]["trust_state"] == "unverified"
    )
    assert m["review"]["feedback"][0]["view"] == "F"
    with pytest.raises(ArtifactReadError):
        read_artifact(
            store,
            "wrong-task",
            "mock",
            mock["revision"],
            "mock-attempt",
            "mock-artifact",
            tmp_path,
            budget,
        )
    with pytest.raises(ArtifactReadError):
        read_artifact(
            store,
            form["task_id"],
            "form",
            mock["revision"],
            "mock-attempt",
            "mock-artifact",
            tmp_path,
            budget,
        )


def test_missing_corrupt_digest_and_format(tmp_path, monkeypatch):
    store, budget, form, mock, path, _digest = _registered_fixture(
        tmp_path, monkeypatch
    )

    def read():
        return read_artifact(
            store,
            form["task_id"],
            "mock",
            mock["revision"],
            "mock-attempt",
            "mock-artifact",
            tmp_path,
            budget,
            expected_mime="image/png",
        )

    path.unlink()
    result = read()
    assert (
        result["delivery_status"] == "delivered"
        and result["file"]["state"] == "missing"
    )
    assert result["upstream_status"] == "succeeded"
    path.write_bytes(b"bad image")
    assert read()["file"]["state"] == "invalid_or_unreadable"
    Image.new("RGB", (5, 5), "red").save(path)
    assert read()["file"]["state"] == "digest_mismatch"
    Image.new("RGB", (5, 5), "red").save(path, format="JPEG")
    assert read()["file"]["state"] == "invalid_or_unreadable"
    with store._connect() as db:
        db.execute(
            "UPDATE artifact SET path=? WHERE artifact_id='mock-artifact'",
            (str(tmp_path.parent / "outside.png"),),
        )
    assert read()["file"]["state"] == "unsafe_path"


def test_review_conflict_and_invalidation(tmp_path, monkeypatch):
    store, budget, form, mock, _path, digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    for name, decision in [("one", "accept"), ("two", "reject")]:
        mock_workflow.save_mock_review(
            store,
            mock_workflow.MockReview(
                name,
                form["task_id"],
                mock["revision"],
                "mock-artifact",
                digest,
                decision,
                "fixture",
                time.time(),
                None,
            ),
        )

    def read():
        return read_artifact(
            store,
            form["task_id"],
            "mock",
            mock["revision"],
            "mock-attempt",
            "mock-artifact",
            tmp_path,
            budget,
        )

    assert read()["review"]["state"] == "conflict"
    mock_workflow.invalidate_mock_review(store, "one", "withdrawn")
    assert read()["review"]["state"] == "reject"
    mock_workflow.invalidate_mock_review(store, "two", "withdrawn")
    assert read()["review"]["state"] == "invalidated"


def test_reopen_history_and_no_implicit_generation(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys

    store, _budget, form, _mock, _path, _digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    code = """import json,sys
from pathlib import Path
from ecommerce_product_image_creator.storage import Store,ResourceBudget
from ecommerce_product_image_creator.artifact_reader import read_artifact
result=read_artifact(Store(Path(sys.argv[1])),sys.argv[2],"form",1,"form-attempt","form-artifact",Path(sys.argv[3]),ResourceBudget(100000,10000,100000))
print(json.dumps(result))"""
    run = subprocess.run(
        [sys.executable, "-c", code, str(store.path), form["task_id"], str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    value = json.loads(run.stdout)
    assert value["file"]["state"] == "verified"
    assert value["review"]["decision"] == "accept"
    assert str(tmp_path) not in run.stdout
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 2


def test_invalid_source_and_changed_read(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import artifact_reader
    from ecommerce_product_image_creator.storage import FileSafetyError

    store, budget, form, mock, _path, _digest = _registered_fixture(
        tmp_path, monkeypatch
    )

    def changed(*args):
        raise FileSafetyError("Input changed during reading")

    monkeypatch.setattr(artifact_reader, "read_reference", changed)
    data = read_artifact(
        store,
        form["task_id"],
        "mock",
        mock["revision"],
        "mock-attempt",
        "mock-artifact",
        tmp_path,
        budget,
    )
    assert data["file"]["state"] == "invalid_or_unreadable"
    assert data["review_applicability"] == "invalid_artifact"
    with store._connect() as db:
        db.execute(
            "UPDATE artifact SET sha256='wrong' WHERE artifact_id='form-artifact'"
        )
    with pytest.raises(ArtifactReadError):
        read_artifact(
            store,
            form["task_id"],
            "mock",
            mock["revision"],
            "mock-attempt",
            "mock-artifact",
            tmp_path,
            budget,
        )


def test_review_expired_and_version_isolation(tmp_path, monkeypatch):
    store, budget, form, mock, _path, digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    mock_workflow.save_mock_review(
        store,
        mock_workflow.MockReview(
            "old",
            form["task_id"],
            mock["revision"],
            "mock-artifact",
            digest,
            "accept",
            "fixture",
            time.time() - 10,
            None,
            expires_at=time.time() - 1,
        ),
    )
    data = read_artifact(
        store,
        form["task_id"],
        "mock",
        mock["revision"],
        "mock-attempt",
        "mock-artifact",
        tmp_path,
        budget,
    )
    assert data["review"]["state"] == "expired"
    assert data["review"]["trust_state"] == "unverified"
    with pytest.raises(ArtifactReadError):
        read_artifact(
            store,
            form["task_id"],
            "mock",
            mock["revision"] + 1,
            "mock-attempt",
            "mock-artifact",
            tmp_path,
            budget,
        )


def test_mcp_artifact_response_and_recovery(tmp_path, monkeypatch):
    import asyncio
    import base64
    import hashlib
    import json
    import sys

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    state = tmp_path / "state"
    root = tmp_path / "input"
    state.mkdir()
    root.mkdir()
    store, _budget, form, mock, path, _digest = _registered_fixture(state, monkeypatch)
    from ecommerce_product_image_creator.business_choices import _table
    from ecommerce_product_image_creator.preview_evidence import persist_preview

    with store._connect() as db:
        _table(db)
        db.execute(
            "INSERT INTO business_choice VALUES(?,?,?,?,?,?,?,?,?,0)",
            (
                "fixture-preview",
                form["task_id"],
                1,
                form["input_digest"],
                form["execution_digest"],
                "uncertainty",
                json.dumps(
                    {
                        "conflicts": ["fixture conflict"],
                        "assumptions": {"bottom": "W"},
                        "mode": "preview",
                    }
                ),
                "fixture",
                time.time(),
            ),
        )
        persist_preview(
            db,
            "form-op",
            "form-attempt",
            {
                "status": "preview",
                "task_id": form["task_id"],
                "revision": 1,
                "execution_digest": form["execution_digest"],
                "decision_id": "fixture-preview",
                "conflicts": ["fixture conflict"],
                "assumptions": {"bottom": "W"},
                "source": "fixture",
                "source_trust": "unverified",
                "structure_verified": False,
            },
        )
    import sqlite3

    with sqlite3.connect(state / "tasks.sqlite") as target, store._connect() as source:
        source.backup(target)
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ecommerce_product_image_creator.server"],
        env=env,
    )

    async def run():
        for _ in range(2):
            async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
                await session.initialize()
                for stage, revision, attempt, artifact in [
                    ("form", 1, "form-attempt", "form-artifact"),
                    ("mock", mock["revision"], "mock-attempt", "mock-artifact"),
                ]:
                    args = {
                        "task_id": form["task_id"],
                        "stage": stage,
                        "revision": revision,
                        "attempt_id": attempt,
                        "artifact_id": artifact,
                    }
                    result = await session.call_tool("get_product_image_task", args)
                    assert not result.is_error
                    data = result.structured_content
                    assert data is not None
                    assert data["file"]["state"] == "verified"
                    assert data["review"]["trust_state"] == "unverified"
                    assert data["mime"]["historical"] == "unknown"
                    if stage == "form":
                        assert data["preview"]["decision_id"] == "fixture-preview"
                        assert data["preview"]["assumptions"] == {"bottom": "W"}
                        assert data["preview"]["structure_verified"] is False
                    else:
                        assert data["preview"]["status"] == "unknown"
                    ref = data["artifact_reference"]
                    blob = base64.b64decode(ref.split(",", 1)[1], validate=True)
                    assert hashlib.sha256(blob).hexdigest() == data["digest"]
                    assert str(state) not in str(data)
                    if stage == "mock":
                        assert data["form_source"]["form_revision"] == 1
                denied = await session.call_tool(
                    "get_product_image_task",
                    {
                        "task_id": "wrong",
                        "stage": "mock",
                        "revision": mock["revision"],
                        "attempt_id": "mock-attempt",
                        "artifact_id": "mock-artifact",
                    },
                )
                assert denied.is_error
                task_view = await session.call_tool(
                    "get_product_image_task", {"task_id": form["task_id"]}
                )
                assert not task_view.is_error
                assert task_view.structured_content["task_id"] == form["task_id"]
                assert {
                    item["stage"] for item in task_view.structured_content["revisions"]
                } == {"form", "mock"}
        with sqlite3.connect(state / "tasks.sqlite") as db:
            db.execute(
                "UPDATE form_review_detail SET invalidation_reason='withdrawn' WHERE review_id='form-review'"
            )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            form_result = await session.call_tool(
                "get_product_image_task",
                {
                    "task_id": form["task_id"],
                    "stage": "form",
                    "revision": 1,
                    "attempt_id": "form-attempt",
                    "artifact_id": "form-artifact",
                },
            )
            assert form_result.structured_content["review"]["state"] == "invalidated"
            args = {
                "task_id": form["task_id"],
                "stage": "mock",
                "revision": mock["revision"],
                "attempt_id": "mock-attempt",
                "artifact_id": "mock-artifact",
            }
            path.write_bytes(b"corrupt")
            result = await session.call_tool("get_product_image_task", args)
            assert result.structured_content["file"]["state"] == "invalid_or_unreadable"
            assert result.structured_content["artifact_reference"] is None
            task_view = await session.call_tool(
                "get_product_image_task", {"task_id": form["task_id"]}
            )
            mock_attempt = next(
                a
                for a in task_view.structured_content["attempts"]
                if a["attempt_id"] == "mock-attempt"
            )
            assert mock_attempt["artifacts"][0]["delivery_status"] == "delivered"
            assert (
                mock_attempt["artifacts"][0]["file"]["state"] == "invalid_or_unreadable"
            )
            Image.new("RGB", (5, 5), "red").save(path)
            result = await session.call_tool("get_product_image_task", args)
            assert result.structured_content["file"]["state"] == "digest_mismatch"
            assert result.structured_content["artifact_reference"] is None
        path.unlink()
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(
                "get_product_image_task",
                {
                    "task_id": form["task_id"],
                    "stage": "mock",
                    "revision": mock["revision"],
                    "attempt_id": "mock-attempt",
                    "artifact_id": "mock-artifact",
                },
            )
            data = result.structured_content
            assert data is not None and data["delivery_status"] == "delivered"
            assert (
                data["file"]["state"] == "missing"
                and data["artifact_reference"] is None
            )

    asyncio.run(run())
