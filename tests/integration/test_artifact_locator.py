"""Offline locator evidence; these fixtures never access a provider."""

import hashlib
import socket
import subprocess
import sys
import time
from dataclasses import replace

import pytest
from PIL import Image

from ecommerce_product_image_creator import original_query, recovery
from ecommerce_product_image_creator.artifact_locator import (
    LocatorEvidence,
    assess,
    assess_attempt,
    register,
)

from .test_original_query import setup_query


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    def blocked(*_a, **_k):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def test_locator_bound_to_success_and_no_new_attempt(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(
        recovery, "_verify_observation_source", lambda i, row: row[6] == i.attempt_id
    )
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}},
    )
    import ecommerce_product_image_creator.artifact_locator as locator

    monkeypatch.setattr(
        locator,
        "_verify_locator_source",
        lambda i, e: (
            i.attempt_id == e.attempt_id
            and e.scope == "fixture-account"
            and e.source == "isolated-source"
            and e.handle in {"handle-1", "handle-2"}
        ),
    )
    snap = args[4]
    resolution = recovery.recover_once(
        service.store,
        "op",
        "a",
        snap.task_id,
        snap.revision,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
    )
    assert resolution.state == "succeeded" and resolution.observation_id is not None
    identity = (
        "op",
        "a",
        snap.task_id,
        snap.revision,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
        resolution.observation_id,
    )
    assert assess_attempt(service.store, *identity).reason == "locator_missing"
    evidence = LocatorEvidence(
        "fixture-locator",
        "op",
        "a",
        snap.task_id,
        snap.revision,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "fixture-account",
        "fixture-upstream",
        resolution.observation_id,
        "image-1",
        "fixture_handle",
        "isolated-source",
        "handle-1",
        time.time(),
        time.time() + 100,
    )
    assert register(service.store, evidence) == evidence.locator_id
    assert register(service.store, evidence) == evidence.locator_id
    with monkeypatch.context() as production:
        production.setattr(locator, "_verify_locator_source", lambda *_: False)
        with pytest.raises(ValueError):
            register(service.store, replace(evidence, locator_id="unverified"))
    result = assess(service.store, evidence)
    assert (
        result.state == "remote_retrieval_candidate"
        and result.locator_id == evidence.locator_id
    )
    assert (
        assess_attempt(service.store, *identity).state == "remote_retrieval_candidate"
    )
    from ecommerce_product_image_creator.domain import UpstreamStatus
    from ecommerce_product_image_creator.form_workflow import register_form_artifact
    from ecommerce_product_image_creator.storage import ResourceBudget

    path = tmp_path / "fixture-artifact.png"
    Image.new("RGB", (4, 4)).save(path)
    image_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    service.store.record_upstream(
        attempt.attempt_id, UpstreamStatus.SUCCEEDED, "fixture-upstream"
    )
    budget = ResourceBudget(100000, 10000, 100000)
    register_form_artifact(
        service.store,
        snap.task_id,
        snap.revision,
        attempt.attempt_id,
        "artifact",
        tmp_path,
        path,
        "image/png",
        image_digest,
        budget,
    )
    local = assess_attempt(
        service.store,
        *identity,
        artifact_id="artifact",
        artifact_root=tmp_path,
        budget=budget,
    )
    assert local.state == "already_available" and len(sends) == 1
    assert len(sends) == 1
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
    with pytest.raises(ValueError):
        register(service.store, replace(evidence, locator_id="bad", attempt_id="other"))
    for field, value in [
        ("task_id", "other"),
        ("candidate_id", "other"),
        ("digest", "bad"),
        ("revision", 99),
        ("stage", "mock"),
        ("upstream_id", "other"),
        ("scope", "unknown"),
        ("observation_id", 9999),
        ("source", "unverified"),
        ("handle", "https://example.invalid/secret"),
    ]:
        with pytest.raises(ValueError):
            register(
                service.store, replace(evidence, locator_id="bad", **{field: value})
            )
    with pytest.raises(ValueError):
        register(service.store, replace(evidence, handle="handle-2"))
    assert (
        assess(service.store, replace(evidence, locator_id="missing")).state
        == "locator_unverified"
    )
    from ecommerce_product_image_creator.storage import Store

    assert (
        assess(Store(service.store.path), evidence).state
        == "remote_retrieval_candidate"
    )
    code = "import sys; from pathlib import Path; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.artifact_locator import read_locator,assess; s=Store(Path(sys.argv[1])); e=read_locator(s,'fixture-locator'); print(e.locator_id,assess(s,e).state)"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "fixture-locator locator_unverified"
    with monkeypatch.context() as earlier:
        earlier.setattr(locator.time, "time", lambda: evidence.created - 1)
        assert assess(service.store, evidence).reason == "locator_time_unverified"
    with monkeypatch.context() as later:
        later.setattr(locator.time, "time", lambda: evidence.expires + 1)
        assert assess(service.store, evidence).reason == "locator_expired"
        with pytest.raises(ValueError):
            register(service.store, replace(evidence, locator_id="expired"))
    second = replace(evidence, locator_id="different", handle="handle-2")
    assert register(service.store, second) == "different"
    assert assess(service.store, evidence).state == "artifact_conflict"
    assert (
        assess_attempt(
            service.store,
            *identity,
            artifact_id="artifact",
            artifact_root=tmp_path,
            budget=budget,
        ).state
        == "artifact_conflict"
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM attempt_counter WHERE count<>1"
        ).fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM recovery_observation").fetchone() == (
            1,
        )
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": "FAILURE"}},
    )
    assert recovery.recover_once(service.store, *identity[:-1]).state == "conflict"
    assert assess(service.store, evidence).state == "locator_unverified"
    assert assess_attempt(service.store, *identity).state == "artifact_conflict"
    assert len(sends) == 1


@pytest.mark.parametrize("status", ["IN_PROGRESS", "unrecognized"])
def test_incomplete_upstream_cannot_produce_locator_candidate(
    tmp_path, monkeypatch, status
):
    service, args, attempt, sends = setup_query(tmp_path, monkeypatch)
    monkeypatch.setattr(
        original_query,
        "_verify_original_binding",
        lambda _db, i: i.attempt_id == attempt.attempt_id,
    )
    monkeypatch.setattr(
        recovery, "_verify_observation_source", lambda i, row: row[6] == i.attempt_id
    )
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: {"data": {"task_id": i.upstream_id, "status": status}},
    )
    snap = args[4]
    result = recovery.recover_once(
        service.store,
        "op",
        "a",
        snap.task_id,
        snap.revision,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
    )
    assert result.observation_id is not None
    assessment = assess_attempt(
        service.store,
        "op",
        "a",
        snap.task_id,
        snap.revision,
        snap.stage,
        attempt.attempt_id,
        snap.execution_digest,
        result.observation_id,
    )
    assert assessment.state == "upstream_not_confirmed" and len(sends) == 1
