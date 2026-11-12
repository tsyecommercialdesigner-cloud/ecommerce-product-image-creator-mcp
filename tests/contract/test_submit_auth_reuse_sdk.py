import asyncio
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from integration.test_linked_atomic_submission import (
    linked_context as risk_linked_context,  # noqa: F401
)
from integration.test_offline_submission import setup_context
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ecommerce_product_image_creator import first_authorization, offline_submission
from ecommerce_product_image_creator.tasks import LocalEligibility


@pytest.mark.parametrize("reused_kind", ["host", "fee"])
def test_consumed_authorization_cannot_authorize_other_operation(
    tmp_path, monkeypatch, reused_kind
):
    root, state = tmp_path / "input", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    service, first = setup_context(root)
    original_prepare = LocalEligibility.prepare

    def prepare_second(self, *args, **kwargs):
        changed = list(args)
        changed[5] = replace(changed[5], confirmation_id="fixture-confirmation-b")
        changed[6] = "op-b"
        return original_prepare(self, *changed, **kwargs)

    with monkeypatch.context() as isolated:
        isolated.setattr(LocalEligibility, "prepare", prepare_second)
        _, second = setup_context(root)
    a, b = first[4], second[4]
    assert a.task_id != b.task_id
    shutil.copy2(service.store.path, state / "tasks.sqlite")
    monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
    with sqlite3.connect(state / "tasks.sqlite") as db:
        for role in ("host", "fee"):
            evidence = first_authorization.FirstAuthorization(
                role + "-a",
                role,
                "op",
                a.task_id,
                a.revision,
                a.stage,
                a.execution_digest,
                a.model,
                a.endpoint,
                a.group,
                a.operation_scope,
                time.time() + 300,
                "fixture",
                role,
            )
            first_authorization.record(db, evidence)
            if role != reused_kind:
                first_authorization.record(
                    db,
                    replace(
                        evidence,
                        authorization_id=role + "-b",
                        operation_id="op-b",
                        task_id=b.task_id,
                        revision=b.revision,
                        execution_digest=b.execution_digest,
                    ),
                )

    def request(snapshot, operation):
        return {
            "task_id": snapshot.task_id,
            "revision": snapshot.revision,
            "input_digest": snapshot.input_digest,
            "stage": snapshot.stage,
            "prompt_revision": snapshot.prompt_revision,
            "prompt_digest": snapshot.prompt_digest,
            "execution_digest": snapshot.execution_digest,
            "operation_id": operation,
        }

    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }
    calls, trace = state / "calls", state / "trace"
    check_spy = (
        "def check(*args,**kwargs):\n"
        " try:\n  return original(*args,**kwargs)\n"
        " except ValueError as e:\n  Path(sys.argv[2]).write_text(str(e))\n  raise"
    )
    command = (
        "import sys; from pathlib import Path; "
        "import ecommerce_product_image_creator.first_authorization as a; "
        "import ecommerce_product_image_creator.offline_submission as o; "
        "a._verify_source=lambda db,e:e.source=='fixture'; "
        "o._verify_authorization=lambda s:True; o._verify_business_choices=lambda s:True; "
        "o._offline_dispatch=lambda req:(open(sys.argv[1],'a').write('1'),o.ProviderResult('accepted',task_id='fake'))[1]; "
        "original=a.check; exec(" + repr(check_spy) + "); a.check=check; "
        "from ecommerce_product_image_creator.server import main; main()"
    )

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", command, str(calls), str(trace)],
            env=env,
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            accepted = await session.call_tool("submit_product_image", request(a, "op"))
            assert (
                not accepted.is_error
                and accepted.structured_content["state"] == "accepted"
            )
            # The public API has no authorization ID field; an extra caller claim
            # cannot move the already consumed evidence to a different scope.
            denied = await session.call_tool(
                "submit_product_image",
                {
                    **request(b, "op-b"),
                    reused_kind + "_authorization_id": reused_kind + "-a",
                },
            )
            assert denied.is_error
            replay = await session.call_tool("submit_product_image", request(a, "op"))
            assert not replay.is_error and replay.structured_content["replayed"] is True
            assert (
                replay.structured_content["attempt_id"]
                == accepted.structured_content["attempt_id"]
            )

    asyncio.run(run())
    assert trace.read_text() == "Independent authorization missing"
    assert calls.read_text() == "1"
    with sqlite3.connect(state / "tasks.sqlite") as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM operation").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_consumption"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op-b'"
        ).fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO first_authorization_consumption VALUES (?,?,?)",
                (reused_kind + "-a", "op-b", "other-attempt"),
            )


@pytest.mark.parametrize("missing", ["fee", "host"])
def test_risk_and_fee_ids_cannot_replace_first_authorization(
    risk_linked_context,  # noqa: F811
    tmp_path,
    monkeypatch,
    missing,
):
    service, _, original, original_calls = risk_linked_context
    assert original.state == "unknown" and len(original_calls) == 1
    original_prepare = LocalEligibility.prepare

    def prepare_second(self, *args, **kwargs):
        changed = list(args)
        changed[5] = replace(changed[5], confirmation_id="second-confirmation")
        changed[6] = "second-op"
        return original_prepare(self, *changed, **kwargs)

    with monkeypatch.context() as isolated:
        isolated.setattr(LocalEligibility, "prepare", prepare_second)
        _, second = setup_context(tmp_path)
    snap = second[4]
    input_root, state = tmp_path / "input-root", tmp_path / "state"
    input_root.mkdir()
    state.mkdir()
    shutil.copy2(service.store.path, state / "tasks.sqlite")
    monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
    role = "host" if missing == "fee" else "fee"
    with sqlite3.connect(state / "tasks.sqlite") as db:
        assert db.execute(
            "SELECT count(*) FROM risk_reservation WHERE authorization_id='risk-1'"
        ).fetchone() == (1,)
        first_authorization.record(
            db,
            first_authorization.FirstAuthorization(
                "available",
                role,
                "second-op",
                snap.task_id,
                snap.revision,
                snap.stage,
                snap.execution_digest,
                snap.model,
                snap.endpoint,
                snap.group,
                snap.operation_scope,
                time.time() + 300,
                "fixture",
                role,
            ),
        )
        before = db.execute("SELECT * FROM risk_reservation").fetchall()
    request = {
        "task_id": snap.task_id,
        "revision": snap.revision,
        "input_digest": snap.input_digest,
        "stage": snap.stage,
        "prompt_revision": snap.prompt_revision,
        "prompt_digest": snap.prompt_digest,
        "execution_digest": snap.execution_digest,
        "operation_id": "second-op",
        missing + "_authorization_id": "risk-1" if missing == "fee" else "available",
    }
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(input_root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }
    calls, trace = state / "calls", state / "trace"
    spy = (
        "def check(*args,**kwargs):\n"
        " try:\n  return original(*args,**kwargs)\n"
        " except ValueError as e:\n  Path(sys.argv[2]).write_text(str(e))\n  raise"
    )
    command = (
        "import sys; from pathlib import Path; "
        "import ecommerce_product_image_creator.first_authorization as a; "
        "import ecommerce_product_image_creator.offline_submission as o; "
        "a._verify_source=lambda db,e:e.source=='fixture'; "
        "o._verify_authorization=lambda s:True; o._verify_business_choices=lambda s:True; "
        "o._offline_dispatch=lambda req:Path(sys.argv[1]).write_text('sent'); "
        "original=a.check; exec(" + repr(spy) + "); a.check=check; "
        "from ecommerce_product_image_creator.server import main; main()"
    )

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", command, str(calls), str(trace)],
            env=env,
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("submit_product_image", request)
            assert result.is_error

    asyncio.run(run())
    assert trace.read_text() == "Independent authorization missing"
    assert not calls.exists()
    with sqlite3.connect(state / "tasks.sqlite") as db:
        assert db.execute("SELECT * FROM risk_reservation").fetchall() == before
        assert db.execute("SELECT count(*) FROM risk_consumption").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_consumption"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='second-op'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("kind", ["host", "fee"])
def test_revocation_after_final_check_is_ordered_after_fake_dispatch(
    tmp_path, monkeypatch, kind
):
    service, args = setup_context(tmp_path)
    snap = args[4]
    monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    with service.store._connect() as db:
        for role in ("host", "fee"):
            first_authorization.record(
                db,
                first_authorization.FirstAuthorization(
                    role,
                    role,
                    "op",
                    snap.task_id,
                    snap.revision,
                    snap.stage,
                    snap.execution_digest,
                    snap.model,
                    snap.endpoint,
                    snap.group,
                    snap.operation_scope,
                    time.time() + 300,
                    "fixture",
                    role,
                ),
            )
    checked, attempting, proceed = Event(), Event(), Event()
    order = []

    def before_dispatch():
        checked.set()
        assert proceed.wait(10)

    def fake_dispatch(_):
        order.append("dispatch")
        return offline_submission.ProviderResult("accepted", task_id="fake")

    def revoke():
        with service.store._connect() as db:
            attempting.set()
            db.execute("INSERT INTO first_authorization_revocation VALUES (?)", (kind,))
        order.append("revoked")

    monkeypatch.setattr(offline_submission, "_before_offline_dispatch", before_dispatch)
    monkeypatch.setattr(offline_submission, "_offline_dispatch", fake_dispatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        submission = pool.submit(
            offline_submission.submit_offline,
            service,
            *args,
            require_first_authorizations=True,
        )
        assert checked.wait(10)
        revocation = pool.submit(revoke)
        assert attempting.wait(10)
        assert not revocation.done()
        proceed.set()
        assert submission.result(timeout=15).state == "accepted"
        revocation.result(timeout=15)
    assert order == ["dispatch", "revoked"]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT phase FROM attempt_transport_evidence"
        ).fetchone() == ("entered",)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_consumption"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_revocation"
        ).fetchone() == (1,)
