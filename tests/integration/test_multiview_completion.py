"""Offline US1 integration: fakes exercise bookkeeping, not image quality or Host approval."""

import asyncio
import hashlib
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from ecommerce_product_image_creator import (
    mock_workflow,
    offline_submission,
    preparation,
)
from ecommerce_product_image_creator.domain import (
    Confirmation,
    ExecutionSnapshot,
    PromptRevision,
    Reference,
    ReferenceCategory,
    Revision,
    Task,
    TaskStatus,
)
from ecommerce_product_image_creator.form_workflow import register_form_artifact
from ecommerce_product_image_creator.preparation import prepare_mock, read_prepared
from ecommerce_product_image_creator.review import FormReview, save_review
from ecommerce_product_image_creator.storage import Store
from ecommerce_product_image_creator.tasks import LocalEligibility, OperationScope

from .test_offline_submission import setup_context


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def guarded(sock, address):
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeError("network disabled")
        return original(sock, address)

    def guarded_ex(sock, address):
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeError("network disabled")
        return original_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_ex)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network disabled")),
    )


def prepare_eligibility(store, root, budget, task_id, rev, op):
    saved = read_prepared(store, task_id, rev)
    snapshot = ExecutionSnapshot.from_payload(saved["snapshot_payload"])
    refs = []
    for meta in saved["references"]:
        path = root / (
            "form-result.png"
            if meta["ref_id"] == "form-result"
            else meta["ref_id"] + ".png"
        )
        data = path.read_bytes()
        refs.append(
            Reference(
                meta["ref_id"],
                ReferenceCategory(meta["category"]),
                path,
                meta["mime"],
                len(data),
                meta["sha256"],
                task_id=task_id,
                view=meta.get("view"),
                region=meta.get("region"),
                version=meta.get("version"),
                importance=meta.get("importance"),
                purpose=meta.get("purpose"),
            )
        )
    task = Task(task_id, saved["input_digest"], "request", TaskStatus.READY)
    revision = Revision(task_id, rev, saved["input_digest"])
    prompt = PromptRevision.from_text(
        task_id, rev, rev, saved["stage"], saved["prompt"]
    )
    confirmation = Confirmation(
        task_id,
        rev,
        saved["input_digest"],
        prompt.prompt_digest,
        rev,
        snapshot.execution_digest,
        confirmation_id="fixture-" + op,
    )
    scope = OperationScope("generate", None, "six-view-composite", 3)
    service = LocalEligibility(store, root, budget)
    args = (task, revision, refs, prompt, snapshot, confirmation, op, scope)
    store.transition_task(task_id, TaskStatus.READY)
    service.prepare(*args, expires_at=time.time() + 120)
    return service, args


def test_two_stage_fake_generation_review_revision_and_sdk(tmp_path: Path, monkeypatch):
    service, form_args = setup_context(tmp_path)
    store, budget = service.store, service.budget
    task_id = form_args[0].task_id
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    calls = []
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_revision_source_verified", lambda *_: True)

    def dispatch(request):
        calls.append(request)
        return offline_submission.ProviderResult(
            "succeeded", task_id="fixture-upstream"
        )

    monkeypatch.setattr(offline_submission, "_offline_dispatch", dispatch)
    form = offline_submission.submit_offline(service, *form_args)
    assert form.state == "succeeded" and form.local_delivery == "not_available"
    assert calls[0].body.count(b'name="image[]"') == 1
    form_path = artifact_root / "form-result.png"
    Image.new("RGB", (5, 5), "blue").save(form_path)
    form_digest = hashlib.sha256(form_path.read_bytes()).hexdigest()
    register_form_artifact(
        store,
        task_id,
        1,
        form.attempt_id,
        "form-result",
        artifact_root,
        form_path,
        "image/png",
        form_digest,
        budget,
    )
    save_review(
        store,
        FormReview(
            "form-ok",
            task_id,
            1,
            "form-result",
            form_digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    artwork = [
        {
            "ref_id": "artwork",
            "category": "artwork",
            "path": str(tmp_path / "artwork.png"),
            "mime": "image/png",
        }
    ]
    mock = prepare_mock(
        store,
        tmp_path,
        budget,
        task_id,
        "form-result",
        "form-ok",
        artwork,
        "mock F L R B T Q",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        artifact_root=artifact_root,
    )
    saved = read_prepared(store, task_id, mock["revision"])
    assert saved["stage_references"] == ["form-result", "artwork"]
    assert saved["reference_bytes"][0] == form_path.read_bytes()
    # Reference objects describe frozen bytes; their paths are never re-read by v2 eligibility.
    (tmp_path / "form-result.png").write_bytes(form_path.read_bytes())
    mock_service, mock_args = prepare_eligibility(
        store, tmp_path, budget, task_id, mock["revision"], "mock-op"
    )
    mock_attempt = offline_submission.submit_offline(mock_service, *mock_args)
    assert (
        mock_attempt.state == "succeeded"
        and calls[1].body.count(b'name="image[]"') == 2
    )
    assert form_path.read_bytes() in calls[1].body
    mock_path = artifact_root / "mock-result.png"
    Image.new("RGB", (5, 5), "green").save(mock_path)
    mock_digest = hashlib.sha256(mock_path.read_bytes()).hexdigest()
    mock_workflow.register_mock_artifact(
        store,
        task_id,
        mock["revision"],
        mock_attempt.attempt_id,
        "mock-result",
        artifact_root,
        mock_path,
        "image/png",
        mock_digest,
        budget,
    )
    reject = mock_workflow.MockReview(
        "mock-no",
        task_id,
        mock["revision"],
        "mock-result",
        mock_digest,
        "reject",
        "fixture",
        time.time(),
        None,
        view="F",
        feedback="fix",
    )
    mock_workflow.save_mock_review(store, reject)
    mock_workflow.record_mock_revision_request(
        store, "rev-request", reject, "fix F", "fixture", time.time()
    )
    assert store.increment_attempt_counter("mock-op", "model", "endpoint") == 1
    new = mock_workflow.prepare_mock_revision(
        store, "rev-request", "host revised prompt", artifact_root, budget
    )
    new_saved = read_prepared(store, task_id, new["revision"])
    assert (
        new_saved["prompt"] == "host revised prompt"
        and new_saved["digest_version"] == 2
    )
    assert new_saved["execution_digest"] != saved["execution_digest"]
    assert new_saved["form_source"] == saved["form_source"]
    assert store.increment_attempt_counter("mock-op", "model", "endpoint") == 2
    new_service, new_args = prepare_eligibility(
        store, tmp_path, budget, task_id, new["revision"], "new-op"
    )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(
            new_service, *new_args[:5], mock_args[5], *new_args[6:]
        )
    assert len(calls) == 2
    revised_attempt = offline_submission.submit_offline(new_service, *new_args)
    assert revised_attempt.state == "succeeded" and len(calls) == 3
    assert store.increment_attempt_counter("new-op", "model", "endpoint") == 1
    revised_path = artifact_root / "mock-revised.png"
    Image.new("RGB", (5, 5), "red").save(revised_path)
    revised_digest = hashlib.sha256(revised_path.read_bytes()).hexdigest()
    mock_workflow.register_mock_artifact(
        store,
        task_id,
        new["revision"],
        revised_attempt.attempt_id,
        "mock-revised",
        artifact_root,
        revised_path,
        "image/png",
        revised_digest,
        budget,
    )
    accepted = mock_workflow.MockReview(
        "mock-yes",
        task_id,
        new["revision"],
        "mock-revised",
        revised_digest,
        "accept",
        "fixture",
        time.time(),
        None,
    )
    mock_workflow.save_mock_review(store, accepted)
    with pytest.raises(mock_workflow.MockWorkflowError):
        mock_workflow.check_mock_approval(
            store,
            "mock-no",
            task_id,
            new["revision"],
            "mock-revised",
            revised_digest,
            artifact_root,
            budget,
        )
    chain = mock_workflow.read_mock_revision_chain(store, "rev-request")
    assert (
        chain["source_attempt"] == mock_attempt.attempt_id
        and chain["attempts"][0]["attempt_id"] == revised_attempt.attempt_id
    )
    assert (
        read_prepared(store, task_id, mock["revision"])["prompt"] == "mock F L R B T Q"
    )
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM artifact WHERE attempt_id=?",
            (mock_attempt.attempt_id,),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM artifact WHERE attempt_id=?", (form.attempt_id,)
        ).fetchone() == (1,)
    state = tmp_path / "state"
    state.mkdir()
    # Registered paths must remain under configured output root for the SDK query.
    for name in ("form-result.png", "mock-result.png", "mock-revised.png"):
        (state / name).write_bytes((artifact_root / name).read_bytes())
    with sqlite3.connect(state / "tasks.sqlite") as target, store._connect() as source:
        source.backup(target)
    with sqlite3.connect(state / "tasks.sqlite") as db:
        for aid, name in [
            ("form-result", "form-result.png"),
            ("mock-result", "mock-result.png"),
            ("mock-revised", "mock-revised.png"),
        ]:
            db.execute(
                "UPDATE artifact SET path=? WHERE artifact_id=?",
                (str(state / name), aid),
            )
    input_root = tmp_path / "empty-input"
    input_root.mkdir()
    env = {
        "PRODUCT_IMAGE_INPUT_ROOT": str(input_root),
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

    async def query():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            for stage, rev, attempt, artifact, decision in [
                ("form", 1, form.attempt_id, "form-result", "accept"),
                (
                    "mock",
                    mock["revision"],
                    mock_attempt.attempt_id,
                    "mock-result",
                    "reject",
                ),
                (
                    "mock",
                    new["revision"],
                    revised_attempt.attempt_id,
                    "mock-revised",
                    "accept",
                ),
            ]:
                result = await session.call_tool(
                    "get_product_image_task",
                    {
                        "task_id": task_id,
                        "stage": stage,
                        "revision": rev,
                        "attempt_id": attempt,
                        "artifact_id": artifact,
                    },
                )
                assert not result.is_error
                assert result.structured_content["file"]["state"] == "verified"
                assert result.structured_content["review"]["decision"] == decision
                assert (
                    result.structured_content["review"]["trust_state"] == "unverified"
                )

    asyncio.run(query())
    reopened = Store(state / "tasks.sqlite")
    assert (
        mock_workflow.read_mock_revision_chain(reopened, "rev-request")["attempts"][0][
            "attempt_id"
        ]
        == revised_attempt.attempt_id
    )
    code = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.mock_workflow import read_mock_revision_chain; c=read_mock_revision_chain(Store(Path(sys.argv[1])),'rev-request'); print(c['source_attempt'],c['target_revision'],c['attempts'][0]['attempt_id'])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(state / "tasks.sqlite")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (
        child.stdout.strip()
        == f"{mock_attempt.attempt_id} {new['revision']} {revised_attempt.attempt_id}"
    )


def test_mock_multiple_manual_revisions_preserve_history(tmp_path, monkeypatch):
    from .test_mock_workflow import _registered_fixture

    store, budget, form, mock, _path, digest = _registered_fixture(
        tmp_path, monkeypatch
    )
    task_id = form["task_id"]
    monkeypatch.setattr(mock_workflow, "_mock_review_source_verified", lambda *_: True)
    monkeypatch.setattr(mock_workflow, "_revision_source_verified", lambda *_: True)
    first = mock_workflow.MockReview(
        "review-one",
        task_id,
        mock["revision"],
        "mock-artifact",
        digest,
        "reject",
        "fixture",
        time.time(),
        None,
    )
    mock_workflow.save_mock_review(store, first)
    mock_workflow.record_mock_revision_request(
        store, "request-one", first, "revise", "fixture", time.time()
    )
    second = mock_workflow.prepare_mock_revision(
        store, "request-one", "revised once", tmp_path, budget
    )
    store.claim_operation(
        "second-op",
        task_id,
        second["revision"],
        second["execution_digest"],
        "second-attempt",
    )
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded' WHERE attempt_id='second-attempt'"
        )
    path = tmp_path / "second.png"
    Image.new("RGB", (5, 5), "purple").save(path)
    digest2 = hashlib.sha256(path.read_bytes()).hexdigest()
    mock_workflow.register_mock_artifact(
        store,
        task_id,
        second["revision"],
        "second-attempt",
        "second-artifact",
        tmp_path,
        path,
        "image/png",
        digest2,
        budget,
    )
    second_review = mock_workflow.MockReview(
        "review-two",
        task_id,
        second["revision"],
        "second-artifact",
        digest2,
        "reject",
        "fixture",
        time.time(),
        None,
    )
    mock_workflow.save_mock_review(store, second_review)
    mock_workflow.record_mock_revision_request(
        store, "request-two", second_review, "revise again", "fixture", time.time()
    )
    third = mock_workflow.prepare_mock_revision(
        store, "request-two", "revised twice", tmp_path, budget
    )
    assert third["revision"] == second["revision"] + 1
    assert read_prepared(store, task_id, second["revision"])["prompt"] == "revised once"
    assert read_prepared(store, task_id, third["revision"])["prompt"] == "revised twice"
    assert (
        mock_workflow.read_mock_revision_chain(store, "request-two")["source_attempt"]
        == "second-attempt"
    )
    assert (
        mock_workflow.read_mock_revision_chain(store, "request-one")["source_digest"]
        == digest
    )
