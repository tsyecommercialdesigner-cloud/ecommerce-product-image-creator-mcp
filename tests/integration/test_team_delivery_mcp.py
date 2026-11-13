"""End-to-end Team delivery, with an isolated local HTTPS artifact server."""

import asyncio
import base64
import hashlib
import sqlite3
import sys
import time
from io import BytesIO
from pathlib import Path

import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from ecommerce_product_image_creator.review import (
    FormReview,
    invalidate_review,
    save_review,
)
from ecommerce_product_image_creator.storage import Store

from .test_pinned_https import tls_server  # noqa: F401


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "pending",
        "running",
        "mismatch",
        "timeout",
        "download_failed",
        "bad_digest",
        "save_failed",
        "mock",
        "team-native",
        "team-native-mock",
        "team-native-legacy-mock",
        "team-native-mismatch",
        "team-native-top-mismatch",
        "team-native-host",
        "team-native-download-failed",
        "team-native-save-failed",
        "team-native-retry",
        "team-native-corrupt",
    ],
)
def test_team_mcp_query_and_delivery(tmp_path: Path, tls_server, scenario):  # noqa: F811
    legacy_review = scenario == "team-native-legacy-mock"
    if legacy_review:
        scenario = "team-native-mock"
    cert, port, tls = tls_server
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
    image = BytesIO()
    Image.new("RGB", (5, 5), "blue").save(image, format="PNG")
    content = image.getvalue()
    tls["body"] = content
    digest = (
        "0" * 64 if scenario == "bad_digest" else hashlib.sha256(content).hexdigest()
    )
    if scenario in {
        "download_failed",
        "team-native-download-failed",
        "team-native-retry",
    }:
        tls["status"] = b"500 Internal Server Error"
    post_count, query_count = state / "post-count", state / "query-count"
    code = r"""
import ssl,sys
from pathlib import Path
from ecommerce_product_image_creator import config,team_transport,team_delivery,original_query,query_registration,recovery,artifact_locator,first_artifact,download_safety,pinned_https,preparation
if not sys.argv[6].startswith('team-native'):
    preparation._review_is_trusted=lambda *a: True
config.load_config=lambda:config.Config('fake-key')
class Response:
    status=200
    def __init__(self, method): self.method=method
    def read(self,n):
        if self.method=='GET':
            import json
            with open(sys.argv[2], 'a') as f: f.write('1')
            if sys.argv[6]=='timeout': raise TimeoutError('fixture')
            status={'pending':'NOT_START','running':'IN_PROGRESS'}.get(sys.argv[6],'SUCCESS')
            count=len(Path(sys.argv[1]).read_text())
            task='fake-upstream' + ('-2' if count>1 else '')
            original_id='wrong-id' if sys.argv[6] in ('mismatch','team-native-mismatch') else task
            payload={'data':{'task_id':original_id,'status':status,'data':{'data':[{'url':'https://artifact.test/image'}]}}}
            if sys.argv[6]=='team-native-top-mismatch': payload['task_id']='wrong-id'
            return json.dumps(payload).encode()
        count=len(Path(sys.argv[1]).read_text())
        return ('{"task_id":"fake-upstream' + ('-2' if count>1 else '') + '"}').encode()
class Connection:
    def request(self,method,*a,**kw):
        self.method=method
        if method=='POST':
            with open(sys.argv[1], 'a') as f: f.write('1')
    def getresponse(self): return Response(self.method)
    def close(self): pass
team_transport._open_connection=lambda *a: Connection()
if not sys.argv[6].startswith('team-native'):
    artifact_locator._verify_locator_source=lambda *a:True
    artifact_locator._verify_fixture_access=lambda *a:True
    artifact_locator._verify_fixture_content=lambda e,b: __import__('hashlib').sha256(b).hexdigest()==sys.argv[3]
    first_artifact._verify_digest_source=lambda e,d,m:d==sys.argv[3] and m=='image/png'
    download_safety._verify_download_scope=lambda e,u,d,m:u=='https://artifact.test/image' and d==sys.argv[3] and m=='image/png'
download_safety._addresses=lambda *a:('127.0.0.1',)
pinned_https._allow_test_loopback=lambda address,port: str(address)=='127.0.0.1'
pinned_https._ssl_context=lambda:ssl.create_default_context(cafile=sys.argv[4])
download_safety._open_pinned=lambda url,host,addresses,limits:pinned_https.open_pinned(url,host,addresses,limits,port=int(sys.argv[5]))
if not sys.argv[6].startswith('team-native'):
    team_delivery._output_evidence=lambda i,o:team_delivery.OutputEvidence('https://artifact.test/image',sys.argv[3],'image/png','fixture-source','fixture-handle')
def save_fault(phase):
    if sys.argv[6] in ('save_failed','team-native-save-failed') and phase=='partial_write': raise OSError('fixture')
first_artifact._hook=save_fault
from ecommerce_product_image_creator.server import main
main()
"""
    env = {
        "TEAM_MODE": "true",
        "TEAM_GATEWAY_URL": "https://ai.t8star.org/v1/images/edits?async=true",
        "ENABLE_REAL_GENERATION": "true",
        "ENABLE_TEAM_QUERY": "true",
        "ENABLE_TEAM_DOWNLOAD": "true",
        **(
            {
                "TEAM_DOWNLOAD_HOSTS": "other.test"
                if scenario == "team-native-host"
                else "artifact.test"
            }
            if scenario.startswith("team-native")
            else {}
        ),
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-c",
                code,
                str(post_count),
                str(query_count),
                digest,
                str(cert),
                str(port),
                scenario,
            ],
            env=env,
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            prep = await session.call_tool(
                "prepare_product_image",
                {
                    "references": refs,
                    "instructions": "test",
                    "prompt": "F L R B T Q",
                    "stage": "form",
                },
            )
            assert not prep.is_error and prep.structured_content
            args = {
                k: prep.structured_content[k]
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
            submit = await session.call_tool("submit_product_image", args)
            assert not submit.is_error and submit.structured_content
            local = await session.call_tool(
                "get_product_image_task", {"task_id": args["task_id"]}
            )
            assert not local.is_error and not query_count.exists()
            view = await session.call_tool(
                "get_product_image_task",
                {"task_id": args["task_id"], "refresh_remote": True},
            )
            assert not view.is_error and view.structured_content
            if scenario == "team-native-retry":
                assert (
                    view.structured_content.get("team_reconciliation")
                    == "team_reconciliation_incomplete"
                )
                assert (
                    view.structured_content["attempts"][0]["delivery_status"]
                    == "download_failed"
                )
                tls["status"] = b"200 OK"
                view = await session.call_tool(
                    "get_product_image_task",
                    {"task_id": args["task_id"], "refresh_remote": True},
                )
                assert not view.is_error and view.structured_content
            if scenario in {
                "download_failed",
                "bad_digest",
                "save_failed",
                "team-native-download-failed",
                "team-native-save-failed",
            }:
                assert (
                    view.structured_content.get("team_reconciliation")
                    == "team_reconciliation_incomplete"
                )
            else:
                assert "team_reconciliation" not in view.structured_content, (
                    view.structured_content
                )
            attempts = view.structured_content["attempts"]
            assert len(attempts) == 1
            if scenario in {
                "success",
                "mock",
                "team-native",
                "team-native-mock",
                "team-native-retry",
                "team-native-corrupt",
            }:
                assert attempts[0]["delivery_status"] == "delivered"
                assert len(attempts[0]["artifacts"]) == 1
                returned_path = attempts[0]["artifacts"][0]["file"]["path"]
                assert Path(returned_path).is_file()
                assert Path(returned_path).read_bytes() == content
                original = await session.call_tool(
                    "get_product_image_file",
                    {
                        "task_id": args["task_id"],
                        "attempt_id": submit.structured_content["attempt_id"],
                        "stage": "form",
                        "revision": 1,
                        "artifact_id": attempts[0]["artifacts"][0]["artifact_id"],
                    },
                )
                originals = [
                    p for p in original.content if isinstance(p, types.ImageContent)
                ]
                assert not original.is_error and len(originals) == 1
                assert base64.b64decode(originals[0].data) == content
                assert originals[0].mime_type == "image/png"
                pictures = [
                    part
                    for part in view.content
                    if isinstance(part, types.ImageContent)
                ]
                assert len(pictures) == 1
                with Image.open(BytesIO(base64.b64decode(pictures[0].data))) as preview:
                    assert max(preview.size) <= 1024
                    assert pictures[0].mime_type in {"image/jpeg", "image/png"}
                assert pictures[0].data not in str(view.structured_content)
                markdown = [
                    part.text
                    for part in view.content
                    if isinstance(part, types.TextContent) and "![商品图](" in part.text
                ]
                assert len(markdown) == 1
                assert attempts[0]["artifacts"][0]["artifact_id"] in markdown[0]
                if scenario.startswith("team-native"):
                    assert (
                        attempts[0]["artifacts"][0]["integrity_provenance"][
                            "supplier_sha256"
                        ]
                        == "not_verified"
                    )
                again = await session.call_tool(
                    "get_product_image_task",
                    {"task_id": args["task_id"], "refresh_remote": True},
                )
                assert not again.is_error
                if scenario == "team-native-corrupt":
                    Path(returned_path).write_bytes(b"corrupt")
                    broken = await session.call_tool(
                        "get_product_image_task",
                        {
                            "task_id": args["task_id"],
                            "attempt_id": submit.structured_content["attempt_id"],
                            "stage": "form",
                            "revision": 1,
                            "artifact_id": attempts[0]["artifacts"][0]["artifact_id"],
                        },
                    )
                    assert broken.is_error
                    broken_original = await session.call_tool(
                        "get_product_image_file",
                        {
                            "task_id": args["task_id"],
                            "attempt_id": submit.structured_content["attempt_id"],
                            "stage": "form",
                            "revision": 1,
                            "artifact_id": attempts[0]["artifacts"][0]["artifact_id"],
                        },
                    )
                    assert broken_original.is_error
                    assert not any(
                        isinstance(part, types.ImageContent) for part in broken.content
                    )
                    Path(returned_path).write_bytes(content)
                if scenario in {"mock", "team-native-mock"}:
                    artifact = attempts[0]["artifacts"][0]["artifact_id"]
                    if scenario == "team-native-mock":
                        missing_review = await session.call_tool(
                            "prepare_product_image",
                            {
                                "task_id": args["task_id"],
                                "stage": "mock",
                                "artifact_id": artifact,
                                "review_id": "not-a-review",
                                "references": [refs[1]],
                                "instructions": "test",
                                "prompt": "mock F L R B T Q",
                            },
                        )
                        assert missing_review.is_error
                        invalid_review = await session.call_tool(
                            "review_form",
                            {
                                "task_id": args["task_id"],
                                "revision": args["revision"],
                                "attempt_id": submit.structured_content["attempt_id"],
                                "artifact_id": "wrong-artifact",
                                "decision": "accept",
                            },
                        )
                        assert invalid_review.is_error
                        if legacy_review:
                            review_store = Store(state / "tasks.sqlite")
                            for case in (
                                "rejected",
                                "untrusted",
                                "expired",
                                "invalidated",
                            ):
                                bad_id = "legacy-" + case
                                now = time.time()
                                save_review(
                                    review_store,
                                    FormReview(
                                        bad_id,
                                        args["task_id"],
                                        args["revision"],
                                        artifact,
                                        digest,
                                        "reject" if case == "rejected" else "accept",
                                        "fixture"
                                        if case == "untrusted"
                                        else "team-responsible-user",
                                        now - 2,
                                        "concept_prototype;internal_mock_allowed;unverified=back,sides,zipper",
                                        expires_at=now - 1
                                        if case == "expired"
                                        else None,
                                    ),
                                )
                                if case == "invalidated":
                                    invalidate_review(review_store, bad_id, "fixture")
                                denied = await session.call_tool(
                                    "confirm_mock_inputs",
                                    {
                                        "task_id": args["task_id"],
                                        "artifact_id": artifact,
                                        "review_id": bad_id,
                                        "references": [refs[1]],
                                    },
                                )
                                assert denied.is_error
                                if case != "invalidated":
                                    invalidate_review(review_store, bad_id, "fixture")
                            review_id = "legacy-team-review"
                            save_review(
                                Store(state / "tasks.sqlite"),
                                FormReview(
                                    review_id,
                                    args["task_id"],
                                    args["revision"],
                                    artifact,
                                    digest,
                                    "accept",
                                    "team-responsible-user",
                                    time.time(),
                                    "concept_prototype;internal_mock_allowed;unverified=back,sides,zipper",
                                ),
                            )
                        else:
                            member_review = await session.call_tool(
                                "review_form",
                                {
                                    "task_id": args["task_id"],
                                    "revision": args["revision"],
                                    "attempt_id": submit.structured_content[
                                        "attempt_id"
                                    ],
                                    "artifact_id": artifact,
                                    "decision": "accept",
                                },
                            )
                            assert (
                                not member_review.is_error
                                and member_review.structured_content
                            )
                            review_id = member_review.structured_content["review_id"]
                        changed = root / "new-artwork.png"
                        Image.new("RGB", (4, 4), "green").save(changed)
                        mock_refs = [{**refs[1], "path": str(changed)}]
                        unconfirmed = await session.call_tool(
                            "prepare_product_image",
                            {
                                "task_id": args["task_id"],
                                "stage": "mock",
                                "artifact_id": artifact,
                                "review_id": review_id,
                                "references": mock_refs,
                                "instructions": "test",
                                "prompt": "mock F L R B T Q",
                            },
                        )
                        assert unconfirmed.is_error
                        wrong_source = await session.call_tool(
                            "confirm_mock_inputs",
                            {
                                "task_id": args["task_id"],
                                "artifact_id": "wrong-artifact",
                                "review_id": review_id,
                                "references": mock_refs,
                            },
                        )
                        assert wrong_source.is_error
                        declaration = await session.call_tool(
                            "confirm_mock_inputs",
                            {
                                "task_id": args["task_id"],
                                "artifact_id": artifact,
                                "review_id": review_id,
                                "references": mock_refs,
                            },
                        )
                        assert (
                            not declaration.is_error and declaration.structured_content
                        )
                        decision_id = declaration.structured_content[
                            "revision_decision_id"
                        ]
                        stale_inputs = await session.call_tool(
                            "prepare_product_image",
                            {
                                "task_id": args["task_id"],
                                "stage": "mock",
                                "artifact_id": artifact,
                                "review_id": review_id,
                                "references": [refs[1]],
                                "instructions": "test",
                                "prompt": "mock F L R B T Q",
                                "revision_decision_id": decision_id,
                            },
                        )
                        assert stale_inputs.is_error
                        assert (
                            post_count.read_text() == "1"
                            and query_count.read_text() == "1"
                        )
                        strict_params = StdioServerParameters(
                            command=sys.executable,
                            args=params.args,
                            env={**env, "TEAM_MODE": "false"},
                        )
                        async with (
                            stdio_client(strict_params) as (sr, sw),
                            ClientSession(sr, sw) as strict,
                        ):
                            await strict.initialize()
                            denied_review = await strict.call_tool(
                                "review_form",
                                {
                                    "task_id": args["task_id"],
                                    "revision": args["revision"],
                                    "attempt_id": submit.structured_content[
                                        "attempt_id"
                                    ],
                                    "artifact_id": artifact,
                                    "decision": "accept",
                                },
                            )
                            denied_input = await strict.call_tool(
                                "confirm_mock_inputs",
                                {
                                    "task_id": args["task_id"],
                                    "artifact_id": artifact,
                                    "review_id": review_id,
                                    "references": mock_refs,
                                },
                            )
                            assert denied_review.is_error and denied_input.is_error
                    else:
                        review_id, mock_refs, decision_id = (
                            "test-review",
                            [refs[1]],
                            None,
                        )
                        save_review(
                            Store(state / "tasks.sqlite"),
                            FormReview(
                                "test-review",
                                args["task_id"],
                                args["revision"],
                                artifact,
                                digest,
                                "accept",
                                "team-responsible-user"
                                if scenario.startswith("team-native")
                                else "fixture",
                                time.time(),
                                "concept_prototype;internal_mock_allowed;unverified=back,sides,zipper"
                                if scenario.startswith("team-native")
                                else None,
                            ),
                        )
                    reviewed = await session.call_tool(
                        "get_product_image_task", {"task_id": args["task_id"]}
                    )
                    assert reviewed.structured_content
                    review = reviewed.structured_content["attempts"][0]["artifacts"][0][
                        "review"
                    ]
                    if scenario == "team-native-mock":
                        assert review["usage_scope"] == "concept_prototype"
                        assert review["mock_permitted"] is True
                        assert review["unverified_structure"] == (
                            ["back", "sides", "zipper"]
                            if legacy_review
                            else "not_specified"
                        )
                        assert review["trust_state"] == "unverified"
                    mock = await session.call_tool(
                        "prepare_product_image",
                        {
                            "task_id": args["task_id"],
                            "stage": "mock",
                            "artifact_id": artifact,
                            "review_id": review_id,
                            "references": mock_refs,
                            "instructions": "test",
                            "prompt": "mock F L R B T Q",
                            **(
                                {"revision_decision_id": decision_id}
                                if decision_id
                                else {}
                            ),
                        },
                    )
                    assert not mock.is_error and mock.structured_content, mock
                    m = mock.structured_content
                    if decision_id:
                        replay = await session.call_tool(
                            "prepare_product_image",
                            {
                                "task_id": args["task_id"],
                                "stage": "mock",
                                "artifact_id": artifact,
                                "review_id": review_id,
                                "references": mock_refs,
                                "instructions": "test",
                                "prompt": "mock F L R B T Q",
                                "revision_decision_id": decision_id,
                            },
                        )
                        assert replay.is_error
                    mock_args = {
                        k: m[k]
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
                    mock_args.update(
                        operation_id="team-mock-operation",
                        form_revision=args["revision"],
                        form_artifact_digest=digest,
                    )
                    submitted = await session.call_tool(
                        "submit_product_image", mock_args
                    )
                    assert not submitted.is_error, submitted
                    mv = await session.call_tool(
                        "get_product_image_task",
                        {
                            "task_id": args["task_id"],
                            "refresh_remote": True,
                        },
                    )
                    assert not mv.is_error and mv.structured_content, mv
                    mock_attempt = mv.structured_content["attempts"][-1]
                    assert mock_attempt["delivery_status"] == "delivered", mv
                    assert len(mock_attempt["artifacts"]) == 1
                    assert mock_attempt["artifacts"][0]["artifact_id"] != artifact
                    await session.call_tool(
                        "get_product_image_task",
                        {
                            "task_id": args["task_id"],
                            "refresh_remote": True,
                        },
                    )
            else:
                assert attempts[0]["artifacts"] == []
                assert not any(
                    isinstance(part, types.ImageContent) for part in view.content
                )
                assert not any(
                    isinstance(part, types.TextContent) and "![商品图](" in part.text
                    for part in view.content
                )

    asyncio.run(run())
    with sqlite3.connect(state / "tasks.sqlite") as db:
        rows = db.execute("SELECT path,sha256 FROM artifact").fetchall()
        if scenario in {
            "success",
            "mock",
            "team-native",
            "team-native-mock",
            "team-native-retry",
            "team-native-corrupt",
        }:
            assert (
                len(rows) == (2 if scenario in {"mock", "team-native-mock"} else 1)
                and rows[0][1] == digest
            )
            assert Path(rows[0][0]).read_bytes() == content
            assert db.execute("SELECT delivery_status FROM attempt").fetchone() == (
                "delivered",
            )
            if scenario.startswith("team-native"):
                evidence = db.execute(
                    "SELECT local_sha256,supplier_sha256,url_sha256 FROM team_artifact_evidence"
                ).fetchall()
                assert len(evidence) == (2 if scenario == "team-native-mock" else 1)
                assert all(row[0] == digest and row[1] is None for row in evidence)
                assert all(
                    row[2] == hashlib.sha256(b"https://artifact.test/image").hexdigest()
                    for row in evidence
                )
        else:
            assert rows == []
            if scenario in {
                "download_failed",
                "bad_digest",
                "save_failed",
                "team-native-download-failed",
                "team-native-save-failed",
            }:
                assert db.execute(
                    "SELECT remote_state FROM recovery_observation"
                ).fetchone() == ("succeeded",)
                expected = (
                    "save_failed"
                    if scenario in {"save_failed", "team-native-save-failed"}
                    else "download_failed"
                )
                assert db.execute("SELECT delivery_status FROM attempt").fetchone() == (
                    expected,
                )
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (
            2 if scenario in {"mock", "team-native-mock"} else 1,
        )
    assert post_count.read_text() == (
        "11" if scenario in {"mock", "team-native-mock"} else "1"
    )
    assert query_count.read_text() == (
        "11" if scenario in {"mock", "team-native-mock", "team-native-retry"} else "1"
    )
    assert len(tls["requests"]) == (
        (2 if scenario in {"mock", "team-native-mock", "team-native-retry"} else 1)
        if scenario
        in {
            "success",
            "mock",
            "team-native",
            "team-native-mock",
            "team-native-retry",
            "team-native-corrupt",
            "team-native-download-failed",
            "team-native-save-failed",
            "download_failed",
            "bad_digest",
            "save_failed",
        }
        else 0
    )
