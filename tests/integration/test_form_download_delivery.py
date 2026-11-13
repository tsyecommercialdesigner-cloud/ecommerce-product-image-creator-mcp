"""Form download and first delivery using an isolated real TLS connection."""

import ssl
from dataclasses import replace

import pytest

from ecommerce_product_image_creator import (
    artifact_locator,
    download_safety,
    first_artifact,
    pinned_https,
    provider,
)
from ecommerce_product_image_creator.task_view import read_task_view

from .test_artifact_reconciliation import first_context  # noqa: F401
from .test_pinned_https import tls_server  # noqa: F401


@pytest.fixture
def delivery(first_context, tls_server, monkeypatch, tmp_path):  # noqa: F811
    service, args, attempt, sends, locator, content, digest, target = first_context
    cert, port, state = tls_server
    state["body"] = content
    allowed = {"value": True}
    monkeypatch.setattr(
        download_safety,
        "_verify_download_scope",
        lambda e, u, d, m: (
            allowed["value"]
            and e == locator
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
        lambda url, hostname, addresses, limits: pinned_https.open_pinned(
            url, hostname, addresses, limits, port=port
        ),
    )
    limits = download_safety.DownloadLimits(
        100000, 1024, 2, 2, 5, max_header_bytes=16384
    )

    def execute():
        return provider.deliver_downloaded_form(
            service.store,
            locator.locator_id,
            "https://artifact.test/image",
            digest,
            "image/png",
            limits,
            service.budget,
            "first",
            "first-artifact",
            tmp_path,
            target,
        )

    return (
        execute,
        service,
        args,
        attempt,
        sends,
        locator,
        content,
        digest,
        target,
        state,
        allowed,
    )


def test_real_tls_form_delivery_and_replay(delivery, tmp_path):
    (
        execute,
        service,
        args,
        attempt,
        sends,
        locator,
        content,
        digest,
        target,
        state,
        _,
    ) = delivery
    assert execute() == "delivered"
    assert execute() == "delivered"
    assert target.read_bytes() == content
    with service.store._connect() as db:
        assert db.execute(
            "SELECT artifact_id,attempt_id,sha256 FROM artifact"
        ).fetchall() == [("first-artifact", attempt.attempt_id, digest)]
        assert db.execute(
            "SELECT upstream_status,delivery_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("accepted", "delivered")
        assert db.execute(
            "SELECT count(*) FROM recovery_observation WHERE observation_id=?",
            (locator.observation_id,),
        ).fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
    view = read_task_view(
        service.store,
        args[0].task_id,
        tmp_path,
        service.budget,
        attempt_id=attempt.attempt_id,
    )
    artifact = next(
        a for a in view["attempts"] if a["attempt_id"] == attempt.attempt_id
    )["artifacts"][0]
    assert artifact["artifact_id"] == "first-artifact"
    assert artifact["delivery_status"] == "delivered"
    assert artifact["file"]["state"] == "verified"
    assert artifact["review"]["state"] != "accept"
    assert sends == [1] and len(state["requests"]) == 2


def test_revoked_scope_after_stage_prevents_publication(delivery, monkeypatch):
    execute, service, _, attempt, sends, _, _, _, target, _, allowed = delivery
    monkeypatch.setattr(
        first_artifact,
        "_hook",
        lambda phase: allowed.update(value=False) if phase == "verified" else None,
    )
    with pytest.raises(ValueError):
        execute()
    assert not target.exists() and sends == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status,delivery_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("accepted", "not_available")
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)


def test_save_failure_preserves_remote_success(delivery, monkeypatch):
    execute, service, _, attempt, sends, _, _, _, target, _, _ = delivery
    monkeypatch.setattr(
        first_artifact.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("fixture write failure")
        ),
    )
    with pytest.raises(OSError, match="fixture write failure"):
        execute()
    assert not target.exists() and sends == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status,delivery_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("accepted", "save_failed")
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)


def test_existing_target_is_never_overwritten(delivery):
    execute, service, _, attempt, sends, _, _, _, target, _, _ = delivery
    target.write_bytes(b"existing")
    with pytest.raises(ValueError, match="target_conflict"):
        execute()
    assert target.read_bytes() == b"existing" and sends == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status,delivery_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("accepted", "save_failed")
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)


def test_tampered_download_candidate_cannot_be_delivered(delivery, monkeypatch):
    execute, service, _, attempt, sends, _, _, _, target, _, _ = delivery
    real_fetch = provider.download_artifact_candidate

    def tampered(*args, **kwargs):
        candidate = real_fetch(*args, **kwargs)
        return replace(candidate, data=b"forged")

    monkeypatch.setattr(provider, "download_artifact_candidate", tampered)
    with pytest.raises(ValueError, match="download_delivery_source_unverified"):
        execute()
    assert not target.exists() and sends == [1]
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("accepted",)


def test_default_download_authorization_rejects_before_transport(delivery, monkeypatch):
    execute, service, _, _, sends, _, _, _, target, state, _ = delivery
    monkeypatch.setattr(download_safety, "_verify_download_scope", lambda *_: False)
    with pytest.raises(ValueError, match="download_source_unverified"):
        execute()
    assert not target.exists() and sends == [1] and state["requests"] == []
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM first_artifact_anchor").fetchone() == (
            0,
        )


def test_locator_identity_invalidated_after_download_stops_publication(
    delivery, monkeypatch
):
    execute, service, _, attempt, sends, _, _, _, target, state, _ = delivery

    def invalidate(phase):
        if phase == "verified":
            monkeypatch.setattr(
                artifact_locator, "_verify_locator_source", lambda *_: False
            )

    monkeypatch.setattr(first_artifact, "_hook", invalidate)
    with pytest.raises(ValueError, match="first_source_unverified"):
        execute()
    assert not target.exists() and sends == [1] and len(state["requests"]) == 1
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("accepted",)
