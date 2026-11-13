"""Candidate persistence only; no model switching or provider network."""

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ecommerce_product_image_creator import offline_submission
from ecommerce_product_image_creator.domain import (
    ExecutionSnapshot,
    PromptRevision,
    Revision,
)
from ecommerce_product_image_creator.execution_candidates import (
    CandidateError,
    create_candidate,
    read_candidate,
    register_root_candidate,
)
from ecommerce_product_image_creator.preparation import read_prepared
from ecommerce_product_image_creator.storage import Store
from ecommerce_product_image_creator.tasks import EligibilityError

from .test_offline_submission import setup_context


def setup_candidates(tmp_path, monkeypatch):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: (
            calls.append(1)
            or offline_submission.ProviderResult("failed", task_id="fixture")
        ),
    )
    offline_submission.submit_offline(service, *args)
    snapshot = args[4]
    alternate = ExecutionSnapshot.create(
        args[1],
        args[3],
        snapshot.stage,
        "gpt-image-2.5-flare",
        snapshot.endpoint,
        snapshot.group,
        references=snapshot.references,
        operation_scope=snapshot.operation_scope,
    )
    return service, args, alternate, calls


def test_candidate_chain_is_immutable_and_does_not_grant_eligibility(
    tmp_path: Path, monkeypatch
):
    service, args, alternate, calls = setup_candidates(tmp_path, monkeypatch)
    original = read_prepared(service.store, args[0].task_id, 1)
    a = register_root_candidate(service.store, "op", "a")
    b = create_candidate(service.store, "op", "b", "a", alternate)
    assert register_root_candidate(service.store, "op", "a") == a
    assert create_candidate(service.store, "op", "b", "a", alternate) == b
    assert (a.root_operation_id, b.root_operation_id, b.source_candidate_id) == (
        "op",
        "op",
        "a",
    )
    assert a.snapshot.revision == b.snapshot.revision == 1
    assert a.snapshot.execution_digest != b.snapshot.execution_digest
    assert a.prompt == b.prompt and a.references == b.references
    assert a.snapshot.references == b.snapshot.references
    assert read_prepared(service.store, args[0].task_id, 1) == original
    assert read_candidate(service.store, "b") == b
    assert b.capability_state == b.authorization_state == "unverified"
    assert b.eligibility_state == "none" and not b.can_send
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM local_eligibility WHERE execution_digest=?",
            (alternate.execution_digest,),
        ).fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable candidate"):
            db.execute(
                "UPDATE execution_candidate SET snapshot_payload='changed' WHERE candidate_id='a'"
            )
    with pytest.raises(EligibilityError):
        service.prepare(
            *args[:4], alternate, args[5], "candidate-b", args[7], expires_at=9999999999
        )
    assert calls == [1]
    code = "import sys; from pathlib import Path; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.execution_candidates import read_candidate; a=read_candidate(Store(Path(sys.argv[1])), 'a'); b=read_candidate(Store(Path(sys.argv[1])), 'b'); print(a.root_operation_id, b.source_candidate_id, a.snapshot.execution_digest != b.snapshot.execution_digest)"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "op a True"


def test_candidate_rejects_changed_context_and_identity_conflicts(
    tmp_path: Path, monkeypatch
):
    service, args, alternate, _ = setup_candidates(tmp_path, monkeypatch)
    register_root_candidate(service.store, "op", "a")
    create_candidate(service.store, "op", "b", "a", alternate)
    modified_prompt = PromptRevision.from_text(args[0].task_id, 1, 2, "form", "changed")
    changed = ExecutionSnapshot.create(
        args[1],
        modified_prompt,
        alternate.stage,
        alternate.model,
        alternate.endpoint,
        alternate.group,
        references=alternate.references,
        operation_scope=alternate.operation_scope,
    )
    changed_images = ExecutionSnapshot.create(
        args[1],
        args[3],
        alternate.stage,
        alternate.model,
        alternate.endpoint,
        alternate.group,
        references=((alternate.references[0][0], "0" * 64),),
        operation_scope=alternate.operation_scope,
    )
    for candidate in (changed, changed_images):
        with pytest.raises(CandidateError):
            create_candidate(service.store, "op", "c", "a", candidate)
    with pytest.raises(CandidateError):
        create_candidate(service.store, "other-root", "c", "a", alternate)
    with pytest.raises(CandidateError):
        create_candidate(service.store, "op", "b", "a", args[4])
    for task_id, revision in (("other-task", 1), (args[0].task_id, 2)):
        rev = Revision(task_id, revision, args[1].input_digest)
        prompt = PromptRevision.from_text(task_id, revision, 1, "form", args[3].text)
        foreign = ExecutionSnapshot.create(
            rev,
            prompt,
            "form",
            alternate.model,
            alternate.endpoint,
            alternate.group,
            references=alternate.references,
            operation_scope=alternate.operation_scope,
        )
        with pytest.raises(CandidateError):
            create_candidate(service.store, "op", "foreign", "a", foreign)

    with pytest.raises(CandidateError):
        create_candidate(
            service.store, "op", "different-id-same-content", "a", alternate
        )


def test_concurrent_candidate_creation_is_idempotent(tmp_path: Path, monkeypatch):
    service, _args, alternate, _ = setup_candidates(tmp_path, monkeypatch)
    register_root_candidate(service.store, "op", "a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(
            pool.map(
                lambda _: create_candidate(service.store, "op", "b", "a", alternate),
                range(2),
            )
        )
    assert values[0] == values[1]


def test_old_database_upgrades_without_backfilling_or_granting_authority(
    tmp_path: Path, monkeypatch
):
    service, args, alternate, _ = setup_candidates(tmp_path, monkeypatch)
    with service.store._connect() as db:
        db.execute("DROP TRIGGER execution_candidate_no_delete")
        db.execute("DROP TRIGGER execution_candidate_no_update")
        db.execute("DROP TABLE execution_candidate")
    reopened = Store(service.store.path)
    assert (
        read_prepared(reopened, args[0].task_id, 1)["execution_digest"]
        == args[4].execution_digest
    )
    with reopened._connect() as db:
        assert db.execute("SELECT count(*) FROM execution_candidate").fetchone() == (0,)
    register_root_candidate(reopened, "op", "a")
    create_candidate(reopened, "op", "b", "a", alternate)
    legacy = ExecutionSnapshot.create(
        args[1], args[3], "form", alternate.model, alternate.endpoint, alternate.group
    )
    assert legacy.digest_version == 1
    with pytest.raises(CandidateError):
        create_candidate(reopened, "op", "legacy", "a", legacy)


def test_candidate_read_rejects_modified_root_digest(tmp_path: Path, monkeypatch):
    service, _args, alternate, _ = setup_candidates(tmp_path, monkeypatch)
    register_root_candidate(service.store, "op", "a")
    create_candidate(service.store, "op", "b", "a", alternate)
    with service.store._connect() as db:
        db.execute("UPDATE operation SET digest='conflicting' WHERE operation_id='op'")
    with pytest.raises(CandidateError):
        read_candidate(service.store, "b")
