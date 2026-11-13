import asyncio
import shutil
import sys
import time
from dataclasses import replace

import pytest
from integration.test_offline_submission import setup_context
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ecommerce_product_image_creator import first_authorization, offline_submission


def test_submit_tool_is_discoverable_by_sdk():
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ecommerce_product_image_creator.server"],
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert "submit_product_image" in {tool.name for tool in tools.tools}

    asyncio.run(run())


def test_conflicting_frozen_prompt_stops_before_consumption(tmp_path, monkeypatch):
    service, args = setup_context(
        tmp_path, "Use competitor brand ORBIT as the target brand."
    )
    calls = []
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda req: calls.append(req)
    )
    with pytest.raises(offline_submission.SubmissionError, match="brand"):
        offline_submission.submit_offline(service, *args)
    assert calls == []
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
def test_sdk_first_submission_with_two_isolated_authorizations(
    tmp_path, monkeypatch, outcome
):
    root = tmp_path / "input"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    service, args = setup_context(root)
    shutil.copy2(service.store.path, state / "tasks.sqlite")
    frozen_form = (root / "form.png").read_bytes()
    (root / "form.png").write_bytes(b"changed after preparation")
    snapshot = args[4]
    monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
        for kind in ("host",):
            first_authorization.record(
                db,
                first_authorization.FirstAuthorization(
                    kind + "-fixture",
                    kind,
                    "op",
                    snapshot.task_id,
                    snapshot.revision,
                    snapshot.stage,
                    snapshot.execution_digest,
                    snapshot.model,
                    snapshot.endpoint,
                    snapshot.group,
                    snapshot.operation_scope,
                    time.time() + 120,
                    "fixture",
                    kind + "-reference",
                ),
            )
    request = {
        "task_id": snapshot.task_id,
        "revision": snapshot.revision,
        "input_digest": snapshot.input_digest,
        "stage": snapshot.stage,
        "prompt_revision": snapshot.prompt_revision,
        "prompt_digest": snapshot.prompt_digest,
        "execution_digest": snapshot.execution_digest,
        "operation_id": "op",
    }
    calls = state / "fake-calls.txt"
    command = (
        "import sys; from pathlib import Path; "
        "import ecommerce_product_image_creator.offline_submission as o; "
        "import ecommerce_product_image_creator.first_authorization as a; "
        "a._verify_source=lambda db,e: e.source=='fixture'; "
        "o._verify_authorization=lambda s: True; "
        "o._verify_business_choices=lambda s: True; "
        f"def_dispatch=lambda req: (open(sys.argv[1],'a').write('1'),open(sys.argv[1]+'.body','wb').write(req.body),o.ProviderResult('{outcome}',task_id='fixture-upstream'))[2]; "
        "o._offline_dispatch=def_dispatch; "
        "from ecommerce_product_image_creator.server import main; main()"
    )
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }

    async def run():
        production = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ecommerce_product_image_creator.server"],
            env=env,
        )
        async with (
            stdio_client(production) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            assert (await session.call_tool("submit_product_image", request)).is_error
            assert (
                await session.call_tool(
                    "submit_product_image",
                    {**request, "approved": True, "authorized": True},
                )
            ).is_error
        params = StdioServerParameters(
            command=sys.executable, args=["-c", command, str(calls)], env=env
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            assert (await session.call_tool("submit_product_image", request)).is_error
            assert not calls.exists()
            with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
                assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
                first_authorization.record(
                    db,
                    first_authorization.FirstAuthorization(
                        "fee-fixture",
                        "fee",
                        "op",
                        snapshot.task_id,
                        snapshot.revision,
                        snapshot.stage,
                        snapshot.execution_digest,
                        snapshot.model,
                        snapshot.endpoint,
                        snapshot.group,
                        snapshot.operation_scope,
                        time.time() + 120,
                        "fixture",
                        "fee-reference",
                    ),
                )
                db.execute(
                    "CREATE TRIGGER test_fail_operation BEFORE UPDATE ON local_eligibility WHEN NEW.consumed=1 BEGIN SELECT RAISE(ABORT,'test rollback'); END"
                )
            # Inject a failure at each authorization insertion, including the
            # second insertion after the first was written in the transaction.
            with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
                ordered_auth = first_authorization.check(db, "op", snapshot)
            for auth_id in ordered_auth:
                with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
                    assert auth_id in {"host-fixture", "fee-fixture"}
                    db.execute(
                        "CREATE TRIGGER test_fail_auth BEFORE INSERT ON "
                        "first_authorization_consumption WHEN NEW.authorization_id="
                        f"'{auth_id}' BEGIN SELECT RAISE(ABORT,'test rollback'); END"
                    )
                assert (
                    await session.call_tool("submit_product_image", request)
                ).is_error
                with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
                    assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
                    assert db.execute("SELECT count(*) FROM operation").fetchone() == (
                        0,
                    )
                    assert db.execute(
                        "SELECT count(*) FROM first_authorization_consumption"
                    ).fetchone() == (0,)
                    assert db.execute(
                        "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
                    ).fetchone() == (0,)
                    db.execute("DROP TRIGGER test_fail_auth")
            assert (await session.call_tool("submit_product_image", request)).is_error
            with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
                assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
                assert db.execute(
                    "SELECT count(*) FROM first_authorization_consumption"
                ).fetchone() == (0,)
                assert db.execute(
                    "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
                ).fetchone() == (0,)
                db.execute("DROP TRIGGER test_fail_operation")
            assert (
                await session.call_tool(
                    "submit_product_image", {**request, "prompt_digest": "0" * 64}
                )
            ).is_error
            for changed in (
                {"stage": "mock"},
                {"stage": "invalid-stage"},
                {"revision": snapshot.revision + 1},
                {"revision": "not-an-integer"},
                {"input_digest": "0" * 64},
                {"prompt_revision": snapshot.prompt_revision + 1},
                {"execution_digest": "0" * 64},
                {"operation_id": "missing-eligibility"},
            ):
                assert (
                    await session.call_tool(
                        "submit_product_image", {**request, **changed}
                    )
                ).is_error
            missing_required = {k: v for k, v in request.items() if k != "task_id"}
            assert (
                await session.call_tool("submit_product_image", missing_required)
            ).is_error
            assert not calls.exists()
            async with (
                stdio_client(production) as (p_read, p_write),
                ClientSession(p_read, p_write) as prod_session,
            ):
                await prod_session.initialize()
                assert (
                    await prod_session.call_tool("submit_product_image", request)
                ).is_error
            # Mutate only the isolated database, restoring each original row before
            # the next case. The client still supplies the original v2 digest.
            with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
                saved_execution = db.execute(
                    "SELECT prompt,model,endpoint,grouping FROM prepared_execution"
                ).fetchone()
                saved_refs = db.execute(
                    "SELECT * FROM prepared_reference ORDER BY position"
                ).fetchall()
                saved_scope = db.execute(
                    "SELECT scope FROM prepared_scope_v2"
                ).fetchone()[0]
                saved_payload = db.execute(
                    "SELECT payload FROM prepared_snapshot_v2"
                ).fetchone()[0]
            mutations = (
                "UPDATE prepared_execution SET prompt='changed prompt'",
                "UPDATE prepared_reference SET data=X'00' WHERE position=0",
                "DELETE FROM prepared_reference WHERE position=0",
                (
                    "UPDATE prepared_reference SET position=99 WHERE position=0;"
                    "UPDATE prepared_reference SET position=0 WHERE position=1;"
                    "UPDATE prepared_reference SET position=1 WHERE position=99;"
                ),
                "UPDATE prepared_execution SET model='other-model'",
                "UPDATE prepared_execution SET endpoint='other-endpoint'",
                "UPDATE prepared_execution SET grouping='other-group'",
                "UPDATE prepared_scope_v2 SET scope='{}'",
                "DELETE FROM prepared_snapshot_v2",
            )
            for mutation in mutations:
                with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
                    db.executescript(mutation)
                assert (
                    await session.call_tool("submit_product_image", request)
                ).is_error
                assert not calls.exists()
                with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
                    assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
                    assert db.execute(
                        "SELECT count(*) FROM first_authorization_consumption"
                    ).fetchone() == (0,)
                    assert db.execute(
                        "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
                    ).fetchone() == (0,)
                    db.execute(
                        "UPDATE prepared_execution SET prompt=?,model=?,endpoint=?,grouping=?",
                        saved_execution,
                    )
                    db.execute("DELETE FROM prepared_reference")
                    db.executemany(
                        "INSERT INTO prepared_reference VALUES (?,?,?,?,?,?,?,?)",
                        saved_refs,
                    )
                    db.execute("UPDATE prepared_scope_v2 SET scope=?", (saved_scope,))
                    db.execute(
                        "INSERT OR REPLACE INTO prepared_snapshot_v2 VALUES (?,?,?)",
                        (snapshot.task_id, snapshot.revision, saved_payload),
                    )
            async with (
                stdio_client(params) as (other_read, other_write),
                ClientSession(other_read, other_write) as other_session,
            ):
                await other_session.initialize()
                competing = await asyncio.gather(
                    session.call_tool("submit_product_image", request),
                    other_session.call_tool("submit_product_image", request),
                )
            assert all(not result.is_error for result in competing), competing
            assert all(result.structured_content is not None for result in competing)
            assert sorted(
                result.structured_content["replayed"] for result in competing
            ) == [
                False,
                True,
            ]
            first = next(
                result
                for result in competing
                if not result.structured_content["replayed"]
            )
            assert (
                competing[0].structured_content["attempt_id"]
                == competing[1].structured_content["attempt_id"]
            )
            assert not first.is_error, first
            assert first.structured_content is not None
            assert first.structured_content["state"] == outcome
            assert first.structured_content["replayed"] is False
            view = await session.call_tool(
                "get_product_image_task", {"task_id": snapshot.task_id}
            )
            assert not view.is_error and view.structured_content is not None
            assert view.structured_content["current_revision"] == snapshot.revision
            assert view.structured_content["current_stage"] == "form"
            attempts = view.structured_content["attempts"]
            assert len(attempts) == 1
            assert attempts[0]["attempt_id"] == first.structured_content["attempt_id"]
            assert attempts[0]["execution_digest"] == snapshot.execution_digest
            assert attempts[0]["local_intent"] == "recorded"
            cost = attempts[0]["generation_cost"]
            assert (
                cost["attempts"][attempts[0]["attempt_id"]]["cost_status"] == "unknown"
            )
            assert cost["total_verified"] is False
            assert attempts[0]["artifacts"] == []
            # Revocation affects new execution, not the identity of an already
            # registered operation or its append-only consumption history.
            with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
                db.executemany(
                    "INSERT INTO first_authorization_revocation VALUES (?)",
                    [("host-fixture",), ("fee-fixture",)],
                )
            second = await session.call_tool("submit_product_image", request)
            assert not second.is_error, second
            assert second.structured_content is not None
            assert (
                second.structured_content["attempt_id"]
                == first.structured_content["attempt_id"]
            )
            assert second.structured_content["replayed"] is True
            assert (
                await session.call_tool(
                    "submit_product_image", {**request, "operation_id": "other"}
                )
            ).is_error

    asyncio.run(run())
    assert calls.read_text() == "1"
    assert frozen_form in (state / "fake-calls.txt.body").read_bytes()
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
        assert db.execute("SELECT count(*) FROM operation").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_consumption"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "failure", ["missing", "host_missing", "scope", "expired", "revoked", "source"]
)
def test_independent_authorization_fail_closed(tmp_path, monkeypatch, failure):
    service, args = setup_context(tmp_path)
    snapshot = args[4]
    monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
    host = first_authorization.FirstAuthorization(
        "host",
        "host",
        "op",
        snapshot.task_id,
        snapshot.revision,
        snapshot.stage,
        snapshot.execution_digest,
        snapshot.model,
        snapshot.endpoint,
        snapshot.group,
        snapshot.operation_scope,
        time.time() + 120,
        "fixture",
        "host-source",
    )
    fee = replace(
        host, authorization_id="fee", kind="fee", source_reference="fee-source"
    )
    if failure == "scope":
        fee = replace(fee, execution_digest="0" * 64)
    with service.store._connect() as db:
        if failure != "host_missing":
            first_authorization.record(db, host)
        if failure != "missing":
            first_authorization.record(db, fee)
        if failure == "revoked":
            db.execute("INSERT INTO first_authorization_revocation VALUES ('fee')")
        if failure == "expired":
            monkeypatch.setattr(
                first_authorization.time, "time", lambda: host.expires_at + 1
            )
        if failure == "source":
            monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: False)
        with pytest.raises(ValueError):
            first_authorization.check(db, "op", snapshot)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_consumption"
        ).fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)


def test_sdk_brand_conflict_denies_before_consumption(tmp_path, monkeypatch):
    root, state = tmp_path / "input", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    service, args = setup_context(
        root, "Use competitor brand ORBIT as the target brand."
    )
    shutil.copy2(service.store.path, state / "tasks.sqlite")
    snapshot = args[4]
    monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
        for kind in ("host", "fee"):
            first_authorization.record(
                db,
                first_authorization.FirstAuthorization(
                    kind,
                    kind,
                    "op",
                    snapshot.task_id,
                    snapshot.revision,
                    snapshot.stage,
                    snapshot.execution_digest,
                    snapshot.model,
                    snapshot.endpoint,
                    snapshot.group,
                    snapshot.operation_scope,
                    time.time() + 120,
                    "fixture",
                    kind + "-source",
                ),
            )
    request = {
        "task_id": snapshot.task_id,
        "revision": snapshot.revision,
        "input_digest": snapshot.input_digest,
        "stage": snapshot.stage,
        "prompt_revision": snapshot.prompt_revision,
        "prompt_digest": snapshot.prompt_digest,
        "execution_digest": snapshot.execution_digest,
        "operation_id": "op",
    }
    command = (
        "import ecommerce_product_image_creator.offline_submission as o; "
        "import ecommerce_product_image_creator.first_authorization as a; "
        "a._verify_source=lambda db,e: True; o._verify_authorization=lambda s: True; "
        "o._verify_business_choices=lambda s: True; "
        "from ecommerce_product_image_creator.server import main; main()"
    )
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-c", command], env=env
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("submit_product_image", request)
            assert result.is_error and "brand conflict" in str(result.content)

    asyncio.run(run())
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_consumption"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (0,)
