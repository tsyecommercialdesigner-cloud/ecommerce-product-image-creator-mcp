import asyncio
import hashlib
import shutil
import sys
import time

import pytest
from integration.test_multiview_completion import prepare_eligibility
from integration.test_offline_submission import setup_context
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image
from unit.revision_fixtures import confirm_fixture_revision

from ecommerce_product_image_creator import (
    business_choices,
    first_authorization,
    offline_submission,
    preparation,
)
from ecommerce_product_image_creator.form_workflow import register_form_artifact
from ecommerce_product_image_creator.preparation import prepare_mock, read_prepared
from ecommerce_product_image_creator.review import FormReview, save_review


@pytest.mark.parametrize(
    "scenario",
    [
        "accepted",
        "unknown",
        "review_invalid",
        "review_expired",
        "review_rejected",
        "source_untrusted",
        "artifact_conflict",
        "artifact_file_corrupt",
        "artifact_file_missing",
        "revision_mismatch",
        "digest_mismatch",
        "before_transaction",
        "after_intent",
        "host_missing",
        "fee_missing",
        "host_form_scope",
        "new_form_revision",
        "region_revision",
        "subset_revision",
        "subset_pending",
        "version_conflict",
        "structure_conflict",
        "brand_conflict",
        "business_invalid",
        "business_before_transaction",
        "business_after_intent",
    ],
)
def test_mock_first_submission_sdk(tmp_path, monkeypatch, scenario):
    root, state = tmp_path / "input", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    service, form_args = setup_context(root)
    store, budget = service.store, service.budget
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: offline_submission.ProviderResult(
            "succeeded", task_id="form-upstream"
        ),
    )
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    form_attempt = offline_submission.submit_offline(service, *form_args)
    artifact_dir = state / "artifacts"
    artifact_dir.mkdir()
    image = artifact_dir / "form-result.png"
    Image.new("RGB", (5, 5), "blue").save(image)
    form_bytes = image.read_bytes()
    digest = hashlib.sha256(form_bytes).hexdigest()
    task_id = form_args[0].task_id
    register_form_artifact(
        store,
        task_id,
        1,
        form_attempt.attempt_id,
        "form-result",
        artifact_dir,
        image,
        "image/png",
        digest,
        budget,
    )
    save_review(
        store,
        FormReview(
            "form-ok",
            task_id,
            1,
            "form-result",
            digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    artwork_input = [
        {
            "ref_id": "artwork",
            "category": "artwork",
            "path": str(root / "artwork.png"),
            "mime": "image/png",
        }
    ]
    revision_decision = None
    if scenario in {"subset_revision", "subset_pending", "version_conflict"}:
        extra = root / "extra.png"
        Image.new("RGB", (3, 3), "red").save(extra)
        if scenario == "version_conflict":
            artwork_input[0]["version"] = "v1"
        artwork_input.append(
            {
                "ref_id": "extra",
                "category": "artwork",
                "path": str(extra),
                "mime": "image/png",
                **({"version": "v2"} if scenario == "version_conflict" else {}),
            }
        )
        revision_decision = confirm_fixture_revision(
            store,
            root,
            budget,
            task_id,
            artwork_input,
            monkeypatch,
            artifact_id="form-result",
            artifact_root=artifact_dir,
        )
    mock = prepare_mock(
        store,
        root,
        budget,
        task_id,
        "form-result",
        "form-ok",
        artwork_input,
        "Use competitor brand ORBIT as the target brand."
        if scenario == "brand_conflict"
        else "mock F L R B T Q",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        artifact_root=artifact_dir,
        _revision_decision_id=revision_decision,
        structure_conflicts=["seal differs"]
        if scenario == "structure_conflict"
        else None,
    )
    (root / "form-result.png").write_bytes(form_bytes)
    _mock_service, mock_args = prepare_eligibility(
        store, root, budget, task_id, mock["revision"], "mock-op"
    )
    snapshot = mock_args[4]
    saved = read_prepared(store, task_id, mock["revision"])
    with store._connect() as db:
        form_review_before_revision = db.execute(
            "SELECT r.*,d.* FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id WHERE r.review_id='form-ok'"
        ).fetchone()
    assert saved["stage_references"] == (
        ["form-result", "artwork", "extra"]
        if scenario in {"subset_revision", "subset_pending", "version_conflict"}
        else ["form-result", "artwork"]
    )
    monkeypatch.setattr(first_authorization, "_verify_source", lambda *_: True)
    with store._connect() as db:
        for kind in ("host", "fee"):
            if (scenario == "host_missing" and kind == "host") or (
                scenario == "fee_missing" and kind == "fee"
            ):
                continue
            auth_snapshot = (
                form_args[4]
                if scenario == "host_form_scope" and kind == "host"
                else snapshot
            )
            first_authorization.record(
                db,
                first_authorization.FirstAuthorization(
                    kind,
                    kind,
                    "mock-op",
                    task_id,
                    auth_snapshot.revision,
                    auth_snapshot.stage,
                    auth_snapshot.execution_digest,
                    auth_snapshot.model,
                    auth_snapshot.endpoint,
                    auth_snapshot.group,
                    auth_snapshot.operation_scope,
                    time.time() + 120,
                    "fixture",
                    kind + "-reference",
                ),
            )
    if scenario.startswith("business_"):
        choice = business_choices.BusinessChoice(
            "preview-choice",
            task_id,
            snapshot.revision,
            snapshot.input_digest,
            snapshot.execution_digest,
            "uncertainty",
            {"conflicts": [], "assumptions": {"bottom": "W"}, "mode": "preview"},
            "fixture",
            time.time(),
        )
        business_choices.record_choice(store, choice)
        monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
        assert (
            business_choices.evaluate_choices(
                store, task_id, snapshot.revision, choice_ids=[choice.choice_id]
            )["status"]
            == "preview"
        )
        if scenario == "business_invalid":
            business_choices.invalidate_choice(
                store, choice.choice_id, "fixture revoked"
            )
            assert (
                business_choices.evaluate_choices(
                    store, task_id, snapshot.revision, choice_ids=[choice.choice_id]
                )["status"]
                == "pending"
            )
    if scenario == "new_form_revision":
        original = [
            {
                "ref_id": kind,
                "category": kind,
                "path": str(root / (kind + ".png")),
                "mime": "image/png",
            }
            for kind in ("artwork", "form")
        ]
        newer = preparation.prepare_form(
            store,
            root,
            budget,
            original,
            "request",
            "new form revision",
            snapshot.model,
            snapshot.endpoint,
            snapshot.group,
            task_id=task_id,
        )
        assert newer["revision"] > snapshot.revision
        assert (
            read_prepared(store, task_id, snapshot.revision)["execution_digest"]
            == snapshot.execution_digest
        )
    if scenario == "region_revision":
        artwork = [
            {
                "ref_id": "artwork",
                "category": "artwork",
                "path": str(root / "artwork.png"),
                "mime": "image/png",
                "region": "rear",
            }
        ]
        decision = confirm_fixture_revision(
            store,
            root,
            budget,
            task_id,
            artwork,
            monkeypatch,
            artifact_id="form-result",
            artifact_root=artifact_dir,
        )
        newer = prepare_mock(
            store,
            root,
            budget,
            task_id,
            "form-result",
            "form-ok",
            artwork,
            "updated packaging area",
            snapshot.model,
            snapshot.endpoint,
            snapshot.group,
            artifact_root=artifact_dir,
            _revision_decision_id=decision,
        )
        assert newer["revision"] > snapshot.revision
        assert (
            read_prepared(store, task_id, newer["revision"])["references"][1]["region"]
            == "rear"
        )
    if scenario in {"subset_revision", "subset_pending"}:
        business_choices.record_subset_proposal(
            store,
            "subset-proposal",
            task_id,
            snapshot.revision,
            ["form-result", "artwork"],
            "fixture",
            "reduce images",
        )
    if scenario == "subset_revision":
        business_choices.record_subset_decision(
            store,
            "subset-proposal",
            "subset-decision",
            "accepted",
            "fixture",
            time.time() + 120,
        )
        monkeypatch.setattr(
            business_choices, "_subset_source_is_trusted", lambda *_: True
        )
        newer = business_choices.apply_subset_decision(store, "subset-proposal")
        assert newer["revision"] > snapshot.revision
        assert read_prepared(store, task_id, newer["revision"])["stage_references"] == [
            "form-result",
            "artwork",
        ]
        assert (
            business_choices.read_subset_proposal(store, "subset-proposal")[
                "target_revision"
            ]
            == newer["revision"]
        )
    with store._connect() as db:
        if scenario == "review_invalid":
            db.execute("INSERT INTO form_review_invalidation VALUES ('form-ok')")
        elif scenario == "review_expired":
            db.execute(
                "UPDATE form_review_detail SET expires_at=? WHERE review_id='form-ok'",
                (time.time() - 1,),
            )
        elif scenario == "review_rejected":
            db.execute(
                "UPDATE form_review_detail SET decision='reject' WHERE review_id='form-ok'"
            )
        elif scenario == "artifact_conflict":
            db.execute(
                "UPDATE artifact SET sha256=? WHERE artifact_id='form-result'",
                ("0" * 64,),
            )
        before_review = db.execute(
            "SELECT * FROM form_review_detail WHERE review_id='form-ok'"
        ).fetchone()
    shutil.copy2(store.path, state / "tasks.sqlite")
    if scenario == "artifact_file_corrupt":
        image.write_bytes(b"corrupt source artifact")
    elif scenario == "artifact_file_missing":
        image.unlink()
    request = {
        "task_id": task_id,
        "revision": snapshot.revision,
        "input_digest": snapshot.input_digest,
        "stage": "mock",
        "prompt_revision": snapshot.prompt_revision,
        "prompt_digest": snapshot.prompt_digest,
        "execution_digest": snapshot.execution_digest,
        "operation_id": "mock-op",
        "form_revision": 2 if scenario == "revision_mismatch" else 1,
        "form_artifact_digest": digest if scenario != "digest_mismatch" else "0" * 64,
    }
    calls = state / "calls.txt"
    outcome = (
        "unknown"
        if scenario in {"unknown", "after_intent", "business_after_intent"}
        else "accepted"
    )
    command = (
        "import sys; import ecommerce_product_image_creator.preparation as p; "
        "import ecommerce_product_image_creator.offline_submission as o; "
        "import ecommerce_product_image_creator.first_authorization as a; "
        "import ecommerce_product_image_creator.business_choices as b; "
        "import ecommerce_product_image_creator.input_revision as ir; "
        f"p._review_is_trusted=lambda *args: {scenario != 'source_untrusted'}; a._verify_source=lambda db,e: e.source=='fixture'; "
        "o._verify_authorization=lambda s: True; o._verify_business_choices=lambda s: True; "
        "b._source_is_trusted=lambda c: c.source=='fixture'; "
        "ir._source_is_trusted=lambda r: r['source']=='test fixture' and r['target_revision'] is not None; "
        f"o._offline_dispatch=lambda req: (open(sys.argv[1],'a').write('1'),open(sys.argv[1]+'.body','wb').write(req.body),o.ProviderResult('{outcome}',task_id='mock-upstream'))[2]; "
    )
    if scenario == "subset_pending":
        command += "old_gate=o.subset_gate_locked; o.subset_gate_locked=lambda *args: (open(sys.argv[1]+'.gate','w').write('checked'),old_gate(*args))[1]; "
    if scenario == "before_transaction":
        command += (
            "old=o.submit_offline; o.submit_offline=lambda *args,**kwargs: "
            "(__import__('sqlite3').connect(sys.argv[2],isolation_level=None).execute(\"INSERT INTO form_review_invalidation VALUES ('form-ok')\"),old(*args,**kwargs))[1]; "
        )
    elif scenario == "after_intent":
        command += "o._after_intent_recorded=lambda: __import__('sqlite3').connect(sys.argv[2],isolation_level=None).execute(\"INSERT INTO form_review_invalidation VALUES ('form-ok')\"); "
    elif scenario in {"business_before_transaction", "business_after_intent"}:
        command += "from pathlib import Path; from ecommerce_product_image_creator.storage import Store; "
        mutation = "b.invalidate_choice(Store(Path(sys.argv[2])),'preview-choice','fixture revoked')"
        if scenario == "business_before_transaction":
            command += f"old=o.submit_offline; o.submit_offline=lambda *args,**kwargs: ({mutation},old(*args,**kwargs))[1]; "
        else:
            command += f"o._after_intent_recorded=lambda: {mutation}; "
    command += "from ecommerce_product_image_creator.server import main; main()"
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
            args=["-c", command, str(calls), str(state / "tasks.sqlite")],
            env=env,
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("submit_product_image", request)
            if scenario not in {
                "accepted",
                "unknown",
                "after_intent",
                "business_after_intent",
            }:
                assert result.is_error, result
                if scenario in {"revision_mismatch", "digest_mismatch"}:
                    assert "FORM_SOURCE_MISMATCH" in str(result.content)
                elif scenario in {"artifact_file_corrupt", "artifact_file_missing"}:
                    assert "FORM_ARTIFACT_INVALID" in str(result.content)
                elif scenario in {
                    "new_form_revision",
                    "region_revision",
                    "subset_revision",
                }:
                    assert "STALE_TASK_REVISION" in str(result.content)
                elif scenario in {
                    "business_invalid",
                    "business_before_transaction",
                    "version_conflict",
                    "structure_conflict",
                }:
                    assert "BUSINESS_CLARIFICATION_REQUIRED" in str(result.content)
                elif scenario == "brand_conflict":
                    assert "Prompt brand conflict requires clarification" in str(
                        result.content
                    )
                elif scenario == "subset_pending":
                    assert "Submission cannot be safely authorized or completed" in str(
                        result.content
                    )
                elif scenario not in {
                    "host_missing",
                    "fee_missing",
                    "host_form_scope",
                    "new_form_revision",
                    "region_revision",
                    "subset_revision",
                    "business_invalid",
                    "business_before_transaction",
                    "subset_pending",
                }:
                    assert "FORM_REVIEW_UNVERIFIED_OR_INVALID" in str(result.content)
                assert not calls.exists()
            else:
                assert not result.is_error, result
                assert result.structured_content is not None
                assert result.structured_content["state"] == outcome
                replay = await session.call_tool("submit_product_image", request)
                assert (
                    not replay.is_error
                    and replay.structured_content["replayed"] is True
                )

    asyncio.run(run())
    if scenario == "subset_pending":
        assert calls.with_name(calls.name + ".gate").read_text() == "checked"
    with first_authorization.sqlite3.connect(state / "tasks.sqlite") as db:
        assert db.execute(
            "SELECT execution_digest FROM prepared_execution WHERE task_id=? AND revision=?",
            (task_id, snapshot.revision),
        ).fetchone() == (snapshot.execution_digest,)
        if scenario == "new_form_revision":
            assert (
                db.execute(
                    "SELECT r.*,d.* FROM form_review r JOIN form_review_detail d ON d.review_id=r.review_id WHERE r.review_id='form-ok'"
                ).fetchone()
                == form_review_before_revision
            )
            assert db.execute(
                "SELECT MAX(revision) FROM prepared_execution WHERE task_id=? AND stage='form'",
                (task_id,),
            ).fetchone() == (snapshot.revision + 1,)
            assert db.execute(
                "SELECT count(*) FROM form_review WHERE task_id=? AND form_revision=?",
                (task_id, snapshot.revision + 1),
            ).fetchone() == (0,)
        if scenario.startswith("business_"):
            assert db.execute(
                "SELECT invalidated FROM business_choice WHERE choice_id='preview-choice'"
            ).fetchone() == (1,)
        assert (
            db.execute(
                "SELECT * FROM form_review_detail WHERE review_id='form-ok'"
            ).fetchone()
            == before_review
        )
        assert db.execute(
            "SELECT count(*) FROM attempt WHERE revision=?", (snapshot.revision,)
        ).fetchone() == (
            (0,)
            if scenario
            not in {"accepted", "unknown", "after_intent", "business_after_intent"}
            else (1,)
        )
        assert db.execute(
            "SELECT count(*) FROM first_authorization_consumption"
        ).fetchone() == (
            (0,)
            if scenario
            not in {"accepted", "unknown", "after_intent", "business_after_intent"}
            else (2,)
        )
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='mock-op'"
        ).fetchone() == (
            (0,)
            if scenario
            not in {"accepted", "unknown", "after_intent", "business_after_intent"}
            else (1,)
        )
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (1,)
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mock_review'"
        ).fetchone():
            assert db.execute("SELECT count(*) FROM mock_review").fetchone() == (0,)
    if scenario in {"accepted", "unknown"}:
        assert calls.read_text() == "1"
        body = (state / "calls.txt.body").read_bytes()
        assert form_bytes in body and body.count(b'name="image[]"') == 2
    elif scenario in {"after_intent", "business_after_intent"}:
        assert not calls.exists()
