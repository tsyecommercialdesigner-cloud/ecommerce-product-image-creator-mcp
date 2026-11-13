"""Direct HTTPS delivery with a persisted nonempty retry cost reservation."""

import errno
import sqlite3
import ssl
import subprocess
import sys

import pytest

from ecommerce_product_image_creator import (
    download_safety,
    first_artifact,
    offline_submission,
    pinned_https,
    provider,
    retry_scope,
)

from .test_first_artifact_acceptance import install_bytes, png_bytes, trusted_locator
from .test_pinned_https import tls_server  # noqa: F401
from .test_retry_scopes import make_scopes


@pytest.fixture
def risk_delivery(tmp_path, monkeypatch, tls_server):  # noqa: F811
    service, args, _, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    retry_scope.reserve_eligibility(
        service, *args, root="op", candidate_id="source", host_id="host", cost_id="cost"
    )
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: (
            calls.append(1)
            or offline_submission.ProviderResult("succeeded", task_id="second-upstream")
        ),
    )
    attempt = offline_submission.submit_offline(
        service, *args, retry_of="op", require_retry_scope=True
    )
    locator = trusted_locator(
        monkeypatch, service, args[4], attempt, "op", "source", "risk-locator"
    )
    content = png_bytes()
    digest = install_bytes(monkeypatch, content)
    cert, port, state = tls_server
    state["body"] = content
    monkeypatch.setattr(
        download_safety,
        "_verify_download_scope",
        lambda e, u, d, m: (
            e == locator
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
    target = tmp_path / "risk-output.png"

    def execute():
        return provider.deliver_downloaded_form(
            service.store,
            locator.locator_id,
            "https://artifact.test/image",
            digest,
            "image/png",
            limits,
            service.budget,
            "risk-delivery",
            "risk-artifact",
            tmp_path,
            target,
        )

    def snapshot():
        with service.store._connect() as db:
            tables = (
                "retry_scope_reservation",
                "retry_scope_evidence",
                "attempt_counter",
                "operation_attempt",
                "local_eligibility",
                "query_invocation",
                "query_billing_event",
            )
            result = {
                name: db.execute(f"SELECT * FROM {name}").fetchall() for name in tables
            }
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_cost_evidence'"
            ).fetchone():
                result["generation_cost_evidence"] = db.execute(
                    "SELECT * FROM generation_cost_evidence"
                ).fetchall()
            return result

    before = snapshot()
    reservation = retry_scope.read_reservation(service.store, args[6])
    assert (
        reservation["reserved_minor"] == 20
        and reservation["actual_cost"] is None
        and reservation["attempt_id"] == attempt.attempt_id
    )
    return (
        execute,
        service,
        args,
        attempt,
        locator,
        content,
        digest,
        target,
        calls,
        state,
        snapshot,
        before,
        cert,
        port,
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "conflict",
        "enospc",
        "enospc_create",
        "memory_stage",
        "database_commit",
        "interrupted",
        "digest",
    ],
)
def test_nonempty_risk_preserved_through_https_delivery(
    risk_delivery, monkeypatch, scenario
):
    (
        execute,
        service,
        args,
        attempt,
        locator,
        content,
        _,
        target,
        calls,
        state,
        snapshot,
        before,
        _,
        _,
    ) = risk_delivery
    if scenario == "conflict":
        target.write_bytes(b"existing")
    if scenario == "enospc":

        def hook(phase):
            if phase == "partial_write":
                raise OSError(errno.ENOSPC, "isolated disk full")

        monkeypatch.setattr(first_artifact, "_hook", hook)
    if scenario == "memory_stage":

        def memory_hook(phase):
            if phase == "partial_write":
                raise MemoryError("isolated allocation failure")

        monkeypatch.setattr(first_artifact, "_hook", memory_hook)
    if scenario == "database_commit":

        def database_hook(phase):
            if phase == "before_commit":
                raise sqlite3.OperationalError("isolated sqlite failure")

        monkeypatch.setattr(first_artifact, "_hook", database_hook)
    if scenario == "enospc_create":

        def fail_create(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "isolated disk full")

        monkeypatch.setattr(first_artifact.os, "open", fail_create)
    if scenario == "interrupted":
        state["extra_headers"] = f"Content-Length: {len(content) + 10}\r\n".encode()
    if scenario == "digest":
        state["body"] = b"incorrect result"
    if scenario == "success":
        assert execute() == "delivered"
        assert target.read_bytes() == content
    else:
        with pytest.raises((ValueError, OSError, MemoryError, sqlite3.Error)):
            execute()
        assert not target.exists() or target.read_bytes() == (
            content if scenario == "database_commit" else b"existing"
        )
    assert snapshot() == before and len(calls) == 2
    assert retry_scope.read_reservation(service.store, args[6])["actual_cost"] is None
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status FROM attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == ("succeeded",)
        assert db.execute(
            "SELECT count(*) FROM recovery_observation WHERE observation_id=?",
            (locator.observation_id,),
        ).fetchone() == (1,)
        if scenario in {
            "conflict",
            "enospc",
            "enospc_create",
            "memory_stage",
            "database_commit",
        }:
            assert db.execute(
                "SELECT delivery_status FROM attempt WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone() == ("save_failed",)
        if scenario == "database_commit":
            assert db.execute(
                "SELECT count(*) FROM artifact WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone() == (0,)
        if scenario == "enospc":
            from pathlib import Path

            stage = Path(
                db.execute(
                    "SELECT stage_path FROM first_artifact_anchor WHERE attempt_id=?",
                    (attempt.attempt_id,),
                ).fetchone()[0]
            )
            assert 0 < stage.stat().st_size < len(content)
            assert db.execute(
                "SELECT count(*) FROM first_artifact_stage WHERE delivery_id='risk-delivery'"
            ).fetchone() == (0,)
            assert db.execute(
                "SELECT count(*) FROM artifact WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone() == (0,)
        if scenario in {"interrupted", "digest"}:
            assert db.execute(
                "SELECT count(*) FROM first_artifact_anchor WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone() == (0,)
            assert db.execute(
                "SELECT delivery_status FROM attempt WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone() == ("download_failed",)
    if scenario == "database_commit":
        monkeypatch.setattr(first_artifact, "_hook", lambda _: None)
        assert (
            service.store.reconcile_first_artifact(
                "risk-delivery", target.parent, service.budget
            )
            == "delivered"
        )
        assert snapshot() == before


def test_real_https_then_process_crash_after_link_and_reconcile(
    risk_delivery, tmp_path
):
    (
        _,
        service,
        args,
        attempt,
        locator,
        content,
        digest,
        target,
        calls,
        state,
        snapshot,
        before,
        cert,
        port,
    ) = risk_delivery
    code = r"""
import os,ssl,sys
from pathlib import Path
from ecommerce_product_image_creator import artifact_locator,download_safety,first_artifact,original_query,pinned_https,provider,recovery
from ecommerce_product_image_creator.storage import ResourceBudget,Store
root=Path(sys.argv[2]); cert=Path(sys.argv[3]); port=int(sys.argv[4]); digest=sys.argv[5]
original_query._verify_original_binding=lambda *_:True
recovery._verify_observation_source=lambda *_:True
artifact_locator._verify_locator_source=lambda *_:True
artifact_locator._verify_fixture_access=lambda *_:True
artifact_locator._verify_fixture_content=lambda _,data:__import__('hashlib').sha256(data).hexdigest()==digest
first_artifact._verify_digest_source=lambda _,d,m:d==digest and m=='image/png'
download_safety._verify_download_scope=lambda e,u,d,m:e.locator_id=='risk-locator' and u=='https://artifact.test/image' and d==digest and m=='image/png'
download_safety._addresses=lambda *_:('127.0.0.1',)
pinned_https._allow_test_loopback=lambda address,_port:str(address)=='127.0.0.1'
pinned_https._ssl_context=lambda:ssl.create_default_context(cafile=str(cert))
download_safety._open_pinned=lambda u,h,a,l:pinned_https.open_pinned(u,h,a,l,port=port)
first_artifact._hook=lambda phase:os._exit(83) if phase=='after_link' else None
provider.deliver_downloaded_form(Store(Path(sys.argv[1])),'risk-locator','https://artifact.test/image',digest,'image/png',download_safety.DownloadLimits(100000,1024,2,2,5,max_header_bytes=16384),ResourceBudget(100000,10000,100000),'risk-delivery','risk-artifact',root,root/'risk-output.png')
raise AssertionError('crash seam missed')
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.store.path),
            str(tmp_path),
            str(cert),
            str(port),
            digest,
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert child.returncode == 83, child.stderr
    assert target.read_bytes() == content and len(state["requests"]) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM artifact WHERE artifact_id='risk-artifact'"
        ).fetchone() == (0,)
    assert snapshot() == before and len(calls) == 2
    recovery_code = r"""
import sys
from pathlib import Path
from ecommerce_product_image_creator import artifact_locator,first_artifact,original_query,recovery
from ecommerce_product_image_creator.storage import ResourceBudget,Store
original_query._verify_original_binding=lambda *_:True
recovery._verify_observation_source=lambda *_:True
artifact_locator._verify_locator_source=lambda *_:True
artifact_locator._verify_fixture_access=lambda *_:True
first_artifact._verify_digest_source=lambda _,d,m:d==sys.argv[3] and m=='image/png'
result=Store(Path(sys.argv[1])).reconcile_first_artifact('risk-delivery',Path(sys.argv[2]),ResourceBudget(100000,10000,100000))
assert result=='delivered'
"""
    resumed = subprocess.run(
        [
            sys.executable,
            "-c",
            recovery_code,
            str(service.store.path),
            str(tmp_path),
            digest,
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert (
        snapshot() == before
        and retry_scope.read_reservation(service.store, args[6])["actual_cost"] is None
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT attempt_id,sha256 FROM artifact WHERE artifact_id='risk-artifact'"
        ).fetchone() == (attempt.attempt_id, digest)
        assert db.execute(
            "SELECT count(*) FROM recovery_observation WHERE observation_id=?",
            (locator.observation_id,),
        ).fetchone() == (1,)
