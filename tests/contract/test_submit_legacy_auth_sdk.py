import asyncio
import hashlib
import json
import shutil
import sys
import time
from dataclasses import replace

import pytest
from integration.test_offline_submission import setup_context
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from ecommerce_product_image_creator import first_authorization
from ecommerce_product_image_creator.domain import ExecutionSnapshot
from ecommerce_product_image_creator.preparation import read_prepared
from ecommerce_product_image_creator.storage import Store


@pytest.mark.parametrize("kind", ["host", "fee"])
@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "expired",
        "revoked",
        "source",
        "operation",
        "revision",
        "stage",
        "digest",
        "model",
        "endpoint",
        "group",
    ],
)
def test_sdk_independent_authorization_rejection(tmp_path, monkeypatch, kind, failure):
    root, state = tmp_path / "input", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    service, args = setup_context(root)
    shutil.copy2(service.store.path, state / "tasks.sqlite")
    snapshot = args[4]
    monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
    now = time.time()
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
        for current in ("host", "fee"):
            evidence = first_authorization.FirstAuthorization(
                current,
                current,
                "op",
                snapshot.task_id,
                snapshot.revision,
                snapshot.stage,
                snapshot.execution_digest,
                snapshot.model,
                snapshot.endpoint,
                snapshot.group,
                snapshot.operation_scope,
                now + 300,
                "fixture",
                current,
            )
            if current == kind:
                if failure == "missing":
                    continue
                changes = {
                    "expired": {"expires_at": now + 10},
                    "source": {"source_reference": "untrusted"},
                    "operation": {"operation_id": "other"},
                    "revision": {"revision": snapshot.revision + 1},
                    "stage": {"stage": "mock"},
                    "digest": {"execution_digest": "0" * 64},
                    "model": {"model": "other"},
                    "endpoint": {"endpoint": "other"},
                    "group": {"group": "other"},
                }
                evidence = replace(evidence, **changes.get(failure, {}))
            first_authorization.record(db, evidence)
        if failure == "revoked":
            db.execute("INSERT INTO first_authorization_revocation VALUES (?)", (kind,))
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
    trace, calls = state / "trace", state / "calls"
    command = (
        "import sys,time; from pathlib import Path; "
        "import ecommerce_product_image_creator.first_authorization as a; "
        "import ecommerce_product_image_creator.offline_submission as o; "
        "real=time.time; "
        + ("a.time.time=lambda:real()+20; " if failure == "expired" else "")
        + "a._verify_source=lambda db,e:e.source=='fixture' and e.source_reference!='untrusted'; "
        "orig=a.check; "
        "exec('def check(*args,**kwargs):\\n try:\\n  result=orig(*args,**kwargs)\\n except ValueError as e:\\n  Path(sys.argv[1]).write_text(str(e))\\n  raise\\n return result'); "
        "a.check=check; o._verify_authorization=lambda s:True; "
        "o._verify_business_choices=lambda s:True; "
        "o._offline_dispatch=lambda req:Path(sys.argv[2]).write_text('sent'); "
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
            command=sys.executable,
            args=["-c", command, str(trace), str(calls)],
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
    assert trace.read_text() in {
        "Independent authorization missing",
        "Authorization scope or source is unverified",
        "Authorization revoked",
    }
    assert not calls.exists()
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM operation").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_consumption"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (0,)


def test_sdk_rejects_v1_digest_against_v2(tmp_path, monkeypatch):
    root, state = tmp_path / "input", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    service, args = setup_context(root)
    shutil.copy2(service.store.path, state / "tasks.sqlite")
    snap = args[4]
    monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
        for kind in ("host", "fee"):
            first_authorization.record(
                db,
                first_authorization.FirstAuthorization(
                    kind,
                    kind,
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
                    kind,
                ),
            )
    legacy = ExecutionSnapshot.create(
        args[1], args[3], snap.stage, snap.model, snap.endpoint, snap.group
    )
    assert legacy.digest_version == 1
    request = {
        "task_id": snap.task_id,
        "revision": snap.revision,
        "input_digest": snap.input_digest,
        "stage": snap.stage,
        "prompt_revision": snap.prompt_revision,
        "prompt_digest": snap.prompt_digest,
        "execution_digest": legacy.execution_digest,
        "operation_id": "op",
    }
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }
    calls = state / "calls"
    command = (
        "import sys; from pathlib import Path; "
        "import ecommerce_product_image_creator.offline_submission as o; "
        "o._offline_dispatch=lambda req:Path(sys.argv[1]).write_text('sent'); "
        "from ecommerce_product_image_creator.server import main; main()"
    )

    async def invoke(req):
        params = StdioServerParameters(
            command=sys.executable, args=["-c", command, str(calls)], env=env
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await session.call_tool("submit_product_image", req)

    # Valid v1 digest against an existing v2 preparation is an identity mismatch.
    assert asyncio.run(invoke(request)).is_error
    assert not calls.exists()
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_consumption"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("kind", ["host", "fee"])
@pytest.mark.parametrize("point", ["after_intent", "after_entered"])
def test_sdk_revocation_after_intent_preserves_unknown(
    tmp_path, monkeypatch, kind, point
):
    root, state = tmp_path / "input", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    service, args = setup_context(root)
    shutil.copy2(service.store.path, state / "tasks.sqlite")
    snap = args[4]
    monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
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
    request = {
        "task_id": snap.task_id,
        "revision": snap.revision,
        "input_digest": snap.input_digest,
        "stage": snap.stage,
        "prompt_revision": snap.prompt_revision,
        "prompt_digest": snap.prompt_digest,
        "execution_digest": snap.execution_digest,
        "operation_id": "op",
    }
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }
    calls = state / "calls"
    revoke_after_entered = (
        "def record(*args,**kwargs):\n"
        " result=original(*args,**kwargs)\n"
        " if args[3]=='entered':\n"
        "  db=sqlite3.connect(sys.argv[2],isolation_level=None)\n"
        "  assert db.execute(\"SELECT count(*) FROM attempt_transport_evidence WHERE phase='entered'\").fetchone()==(1,)\n"
        "  db.execute('INSERT INTO first_authorization_revocation VALUES (?)',(sys.argv[3],))\n"
        "  db.close()\n"
        " return result"
    )
    revoke_hook = (
        "o._after_intent_recorded=lambda:sqlite3.connect(sys.argv[2],isolation_level=None).execute('INSERT INTO first_authorization_revocation VALUES (?)',(sys.argv[3],)); "
        if point == "after_intent"
        else "original=o._record_transport_boundary; exec("
        + repr(revoke_after_entered)
        + "); o._record_transport_boundary=record; "
    )
    command = (
        "import sys,sqlite3; from pathlib import Path; "
        "import ecommerce_product_image_creator.first_authorization as a; "
        "import ecommerce_product_image_creator.offline_submission as o; "
        "a._verify_source=lambda db,e:e.source=='fixture'; "
        "o._verify_authorization=lambda s:True; o._verify_business_choices=lambda s:True; "
        "o._offline_dispatch=lambda req:Path(sys.argv[1]).write_text('sent'); "
        + revoke_hook
        + "from ecommerce_product_image_creator.server import main; main()"
    )

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", command, str(calls), str(state / "tasks.sqlite"), kind],
            env=env,
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            first = await session.call_tool("submit_product_image", request)
            assert not first.is_error and first.structured_content["state"] == "unknown"
            again = await session.call_tool("submit_product_image", request)
            assert not again.is_error and again.structured_content["replayed"] is True
            assert (
                again.structured_content["attempt_id"]
                == first.structured_content["attempt_id"]
            )

    asyncio.run(run())
    assert not calls.exists()
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM operation").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_consumption"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT count(*) FROM first_authorization_revocation"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT phase FROM attempt_transport_evidence"
        ).fetchone() == ("entered",)


def test_sdk_rejects_readable_old_v0_preparation(tmp_path):
    # Reproduce the actual legacy tables and digest used by the existing
    # test_legacy_form_snapshot_remains_readable_without_migration fixture.
    root, state = tmp_path / "input", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    _, v2_args = setup_context(root)
    real_v2_digest = v2_args[4].execution_digest
    Image.new("RGB", (3, 3)).save(root / "legacy.png")
    image = (root / "legacy.png").read_bytes()
    image_digest = hashlib.sha256(image).hexdigest()
    prompt_digest = hashlib.sha256(b"prompt").hexdigest()
    values = [
        "old",
        1,
        "form",
        "input",
        prompt_digest,
        "model",
        "endpoint",
        "group",
        "six-view-composite",
        [("form", image_digest)],
    ]
    digest = hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    store = Store(state / "tasks.sqlite")
    store.create_task("old", "input", "instructions")
    store.create_revision("old", 1, "input")
    with store._connect() as db:
        db.execute(
            "CREATE TABLE prepared_execution (task_id TEXT, revision INTEGER, stage TEXT, prompt TEXT, prompt_digest TEXT, execution_digest TEXT, model TEXT, endpoint TEXT, grouping TEXT, PRIMARY KEY(task_id,revision))"
        )
        db.execute(
            "CREATE TABLE prepared_reference (task_id TEXT, revision INTEGER, position INTEGER, ref_id TEXT, category TEXT, mime TEXT, sha256 TEXT, data BLOB)"
        )
        db.execute(
            "INSERT INTO prepared_execution VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "old",
                1,
                "form",
                "prompt",
                prompt_digest,
                digest,
                "model",
                "endpoint",
                "group",
            ),
        )
        db.execute(
            "INSERT INTO prepared_reference VALUES (?,?,?,?,?,?,?,?)",
            ("old", 1, 0, "form", "form", "image/png", image_digest, image),
        )
    old = read_prepared(store, "old", 1)
    assert old["digest_version"] == 0 and old["reference_bytes"] == [image]
    request = {
        "task_id": "old",
        "revision": 1,
        "input_digest": "input",
        "stage": "form",
        "prompt_revision": 1,
        "prompt_digest": prompt_digest,
        "execution_digest": digest,
        "operation_id": "op",
    }
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(root),
        "PRODUCT_IMAGE_STATE_DIR": str(state),
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "100000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "100000",
    }
    trace = state / "trace"
    read_spy = (
        "def read(*a,**k):\n"
        " r=orig(*a,**k)\n"
        " Path(sys.argv[1]).write_text(str(r['digest_version']))\n"
        " return r"
    )
    command = (
        "import sys; from pathlib import Path; "
        "import ecommerce_product_image_creator.server as s; "
        "import ecommerce_product_image_creator.offline_submission as o; "
        "o._offline_dispatch=lambda req:Path(sys.argv[1]+'.calls').write_text('sent'); "
        "orig=s.read_prepared; "
        + "exec("
        + repr(read_spy)
        + "); s.read_prepared=read; s.main()"
    )

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-c", command, str(trace)], env=env
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            for supplied in (digest, real_v2_digest):
                result = await session.call_tool(
                    "submit_product_image", {**request, "execution_digest": supplied}
                )
                assert result.is_error

    asyncio.run(run())
    assert trace.read_text() == "0"
    assert not (state / "trace.calls").exists()
    assert read_prepared(store, "old", 1) == old
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM operation").fetchone() == (0,)
