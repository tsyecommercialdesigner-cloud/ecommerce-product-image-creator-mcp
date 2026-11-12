"""Mock first delivery through isolated TLS and the existing T048 service."""

import hashlib
import ssl
import time
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from ecommerce_product_image_creator import (
    artifact_locator,
    download_safety,
    first_artifact,
    offline_submission,
    pinned_https,
    preparation,
    provider,
)
from ecommerce_product_image_creator.execution_candidates import register_root_candidate
from ecommerce_product_image_creator.preparation import prepare_mock, read_prepared
from ecommerce_product_image_creator.review import (
    FormReview,
    invalidate_review,
    save_review,
)
from ecommerce_product_image_creator.task_view import read_task_view

from .test_first_artifact_acceptance import install_bytes, png_bytes, trusted_locator
from .test_multiview_completion import prepare_eligibility
from .test_offline_submission import setup_context
from .test_pinned_https import tls_server  # noqa: F401
from .test_task_revision_roots import grant_fixture


@pytest.fixture
def mock_delivery(tmp_path, monkeypatch, tls_server):  # noqa: F811
    service, args = setup_context(tmp_path)
    store, budget = service.store, service.budget
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: (
            calls.append(1)
            or offline_submission.ProviderResult(
                "succeeded", task_id=f"up-{len(calls)}"
            )
        ),
    )
    grant_fixture(service, args, monkeypatch)
    form_attempt = offline_submission.submit_offline(
        service, *args, require_first_authorizations=True
    )
    register_root_candidate(store, "op", "form-candidate")
    locator = trusted_locator(
        monkeypatch,
        service,
        args[4],
        form_attempt,
        "op",
        "form-candidate",
        "form-locator",
    )
    form_data = png_bytes()
    form_sha = install_bytes(monkeypatch, form_data)
    form_path = tmp_path / "form-delivered.png"
    store.register_first_artifact(
        "form-delivery",
        "form-artifact",
        locator.locator_id,
        form_sha,
        "image/png",
        tmp_path,
        form_path,
    )
    store.stage_first_artifact("form-delivery", tmp_path, budget)
    store.reconcile_first_artifact("form-delivery", tmp_path, budget)
    save_review(
        store,
        FormReview(
            "form-review-p1",
            args[0].task_id,
            1,
            "form-artifact",
            form_sha,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    mock = prepare_mock(
        store,
        tmp_path,
        budget,
        args[0].task_id,
        "form-artifact",
        "form-review-p1",
        [
            {
                "ref_id": "artwork",
                "category": "artwork",
                "path": str(tmp_path / "artwork.png"),
                "mime": "image/png",
            }
        ],
        "mock F L R B T Q",
        args[4].model,
        args[4].endpoint,
        args[4].group,
    )
    (tmp_path / "form-artifact.png").write_bytes(form_data)
    mock_service, mock_args = prepare_eligibility(
        store, tmp_path, budget, args[0].task_id, mock["revision"], "mock-op-p1"
    )
    grant_fixture(mock_service, mock_args, monkeypatch)
    attempt = offline_submission.submit_offline(
        mock_service, *mock_args, require_first_authorizations=True
    )
    register_root_candidate(store, "mock-op-p1", "mock-candidate")
    mock_locator = trusted_locator(
        monkeypatch,
        mock_service,
        mock_args[4],
        attempt,
        "mock-op-p1",
        "mock-candidate",
        "mock-locator",
    )
    output = BytesIO()
    Image.new("RGB", (5, 5), "red").save(output, format="PNG")
    content = output.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    install_bytes(monkeypatch, content)
    cert, port, state = tls_server
    state["body"] = content
    monkeypatch.setattr(
        download_safety,
        "_verify_download_scope",
        lambda e, u, d, m: (
            e == mock_locator
            and u == "https://artifact.test/image"
            and d == digest
            and m == "image/png"
        ),
    )
    monkeypatch.setattr(download_safety, "_addresses", lambda *_: ("127.0.0.1",))
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(cert)),
    )
    monkeypatch.setattr(
        download_safety,
        "_open_pinned",
        lambda url, host, addresses, limits: pinned_https.open_pinned(
            url, host, addresses, limits, port=port
        ),
    )
    limits = download_safety.DownloadLimits(
        100000, 1024, 2, 2, 5, max_header_bytes=16384
    )
    target = tmp_path / "mock-delivered.png"

    def execute():
        return provider.deliver_downloaded_mock(
            store,
            mock_locator.locator_id,
            "https://artifact.test/image",
            digest,
            "image/png",
            limits,
            budget,
            "mock-delivery",
            "mock-artifact",
            tmp_path,
            target,
        )

    return (
        execute,
        store,
        args,
        mock_args,
        attempt,
        mock_locator,
        form_data,
        content,
        digest,
        target,
        calls,
        state,
    )


def test_mock_https_first_delivery_and_business_identity(mock_delivery, tmp_path):
    (
        execute,
        store,
        args,
        mock_args,
        attempt,
        locator,
        form_data,
        content,
        digest,
        target,
        calls,
        state,
    ) = mock_delivery
    assert execute() == "delivered"
    assert execute() == "delivered"
    assert target.read_bytes() == content != form_data
    saved = read_prepared(store, args[0].task_id, mock_args[4].revision)
    assert (
        saved["stage"] == "mock"
        and saved["form_source"]["artifact_id"] == "form-artifact"
    )
    assert saved["stage_references"] == ["form-artifact", "artwork"]
    assert (
        locator.attempt_id == attempt.attempt_id
        and locator.digest == mock_args[4].execution_digest
    )
    with store._connect() as db:
        refs = db.execute(
            "SELECT ref_id,sha256,data FROM prepared_reference WHERE task_id=? AND revision=? ORDER BY position",
            (args[0].task_id, mock_args[4].revision),
        ).fetchall()
        assert [r[0] for r in refs] == ["form-artifact", "artwork"]
        assert (
            refs[0][2] == form_data
            and refs[0][1] == hashlib.sha256(form_data).hexdigest()
        )
        assert refs[1][2] == (tmp_path / "artwork.png").read_bytes()
        assert refs[1][1] == hashlib.sha256(refs[1][2]).hexdigest()
        assert db.execute(
            "SELECT attempt_id,sha256 FROM artifact WHERE artifact_id='mock-artifact'"
        ).fetchone() == (attempt.attempt_id, digest)
        assert db.execute(
            "SELECT count(*) FROM artifact WHERE artifact_id='mock-artifact'"
        ).fetchone() == (1,)
    view = read_task_view(
        store,
        args[0].task_id,
        tmp_path,
        store_budget(store, tmp_path),
        attempt_id=attempt.attempt_id,
    )
    assert view["attempts"][0]["artifacts"][0]["review"]["state"] != "accept"
    assert len(calls) == 2 and len(state["requests"]) == 2


def store_budget(store, tmp_path):
    del store, tmp_path
    from ecommerce_product_image_creator.storage import ResourceBudget

    return ResourceBudget(100000, 10000, 100000)


def test_mock_form_review_revoked_after_download(mock_delivery, monkeypatch):
    execute, store, _, _, attempt, _, _, _, _, target, calls, state = mock_delivery

    def hook(phase):
        if phase == "written":
            invalidate_review(store, "form-review-p1", "fixture revoked")

    monkeypatch.setattr(first_artifact, "_hook", hook)
    with pytest.raises(ValueError):
        execute()
    assert not target.exists() and len(calls) == 2 and len(state["requests"]) == 1
    with store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM artifact WHERE attempt_id=?", (attempt.attempt_id,)
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("succeeded",)


def test_mock_revoked_review_is_rejected_before_download(mock_delivery):
    execute, store, _, _, attempt, locator, _, _, _, target, calls, state = (
        mock_delivery
    )
    invalidate_review(store, "form-review-p1", "fixture revoked")
    with pytest.raises(ValueError):
        execute()
    assert not target.exists() and len(calls) == 2 and state["requests"] == []
    with store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM artifact WHERE attempt_id=?", (attempt.attempt_id,)
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM recovery_observation WHERE observation_id=?",
            (locator.observation_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize("mismatch", ["form_source", "revision", "locator"])
def test_mock_https_rechecks_authoritative_identity_after_download(
    mock_delivery, monkeypatch, mismatch
):
    execute, store, args, mock_args, attempt, locator, _, _, _, target, calls, state = (
        mock_delivery
    )
    real_fetch = provider.download_artifact_candidate
    with store._connect() as db:
        attempt_before = db.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (attempt.attempt_id,)
        ).fetchone()
        obs_before = db.execute(
            "SELECT * FROM recovery_observation WHERE observation_id=?",
            (locator.observation_id,),
        ).fetchone()
        review_before = db.execute(
            "SELECT * FROM form_review WHERE review_id='form-review-p1'"
        ).fetchone()
        query_before = db.execute("SELECT * FROM query_invocation").fetchall()
        reservations = (
            db.execute("SELECT * FROM retry_scope_reservation").fetchall()
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retry_scope_reservation'"
            ).fetchone()
            else None
        )

    def after_download(*pos, **kw):
        candidate = real_fetch(*pos, **kw)
        if mismatch == "locator":
            # The other locator is a real, separately persisted Form identity.
            return replace(candidate, locator_id="form-locator")
        new_revision = None
        if mismatch == "revision":
            next_mock = prepare_mock(
                store,
                target.parent,
                store_budget(store, target.parent),
                args[0].task_id,
                "form-artifact",
                "form-review-p1",
                [
                    {
                        "ref_id": "artwork",
                        "category": "artwork",
                        "path": str(target.parent / "artwork.png"),
                        "mime": "image/png",
                    }
                ],
                "second mock F L R B T Q",
                args[4].model,
                args[4].endpoint,
                args[4].group,
            )
            assert next_mock["revision"] != mock_args[4].revision
            new_revision = next_mock["revision"]
        with store._connect() as db:
            if mismatch == "form_source":
                db.execute(
                    "UPDATE prepared_mock_source SET artifact_id='wrong-form-artifact' WHERE task_id=? AND revision=?",
                    (args[0].task_id, mock_args[4].revision),
                )
            else:
                # Fault injection binds the Root to the distinct prepared Mock
                # revision; it does not alter the original Attempt.
                assert new_revision is not None
                db.execute(
                    "UPDATE operation SET revision=? WHERE operation_id=?",
                    (new_revision, locator.root),
                )
        return candidate

    monkeypatch.setattr(provider, "download_artifact_candidate", after_download)
    expected = "download_delivery_source_unverified" if mismatch == "locator" else None
    with pytest.raises(ValueError, match=expected):
        execute()
    assert not target.exists() and len(calls) == 2 and len(state["requests"]) == 1
    with store._connect() as db:
        assert (
            db.execute(
                "SELECT * FROM attempt WHERE attempt_id=?", (attempt.attempt_id,)
            ).fetchone()
            == attempt_before
        )
        assert (
            db.execute(
                "SELECT * FROM recovery_observation WHERE observation_id=?",
                (locator.observation_id,),
            ).fetchone()
            == obs_before
        )
        assert (
            db.execute(
                "SELECT * FROM form_review WHERE review_id='form-review-p1'"
            ).fetchone()
            == review_before
        )
        assert db.execute("SELECT * FROM query_invocation").fetchall() == query_before
        assert db.execute(
            "SELECT count(*) FROM artifact WHERE attempt_id=?", (attempt.attempt_id,)
        ).fetchone() == (0,)
        if reservations is not None:
            assert (
                db.execute("SELECT * FROM retry_scope_reservation").fetchall()
                == reservations
            )


def test_mock_wrong_attempt_locator_rejected_before_network(mock_delivery):
    _, store, _, _, attempt, locator, _, _, digest, target, calls, state = mock_delivery
    with store._connect() as db:
        other_attempt = db.execute(
            "SELECT attempt_id FROM artifact WHERE artifact_id='form-artifact'"
        ).fetchone()[0]
        observation = db.execute(
            "SELECT * FROM recovery_observation WHERE observation_id=?",
            (locator.observation_id,),
        ).fetchone()
    assert other_attempt != attempt.attempt_id
    mismatched = replace(
        locator, locator_id="wrong-attempt-locator", attempt_id=other_attempt
    )
    with pytest.raises(ValueError, match="Locator identity or source unverified"):
        artifact_locator.register(store, mismatched)
    from ecommerce_product_image_creator.storage import ResourceBudget

    with pytest.raises(ValueError, match="download_source_unverified"):
        provider.deliver_downloaded_mock(
            store,
            mismatched.locator_id,
            "https://artifact.test/image",
            digest,
            "image/png",
            download_safety.DownloadLimits(
                100000, 1024, 2, 2, 5, max_header_bytes=16384
            ),
            ResourceBudget(100000, 10000, 100000),
            "wrong-delivery",
            "wrong-artifact",
            target.parent,
            target,
        )
    assert not target.exists() and len(calls) == 2 and state["requests"] == []
    with store._connect() as db:
        assert (
            db.execute(
                "SELECT * FROM recovery_observation WHERE observation_id=?",
                (locator.observation_id,),
            ).fetchone()
            == observation
        )
        assert db.execute(
            "SELECT count(*) FROM artifact WHERE attempt_id=?", (attempt.attempt_id,)
        ).fetchone() == (0,)
