import hashlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

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
from ecommerce_product_image_creator.preparation import (
    prepare_form,
    prepare_mock,
    read_prepared,
)
from ecommerce_product_image_creator.review import FormReview, save_review
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.tasks import (
    EligibilityError,
    LocalEligibility,
    OperationScope,
)


def setup(tmp_path: Path):
    refs = []
    for name, category in [
        ("a", ReferenceCategory.ARTWORK),
        ("f", ReferenceCategory.FORM),
    ]:
        path = tmp_path / (name + ".png")
        Image.new("RGB", (2, 2), "red").save(path)
        import hashlib

        raw = path.read_bytes()
        refs.append(
            Reference(
                name,
                category,
                path,
                "image/png",
                len(raw),
                hashlib.sha256(raw).hexdigest(),
                task_id="task",
            )
        )
    store = Store(tmp_path / "old.sqlite")
    inputs = [
        {
            "ref_id": ref.ref_id,
            "category": ref.category.value,
            "path": str(ref.path),
            "mime": ref.mime,
        }
        for ref in refs
    ]
    with patch(
        "ecommerce_product_image_creator.preparation.uuid.uuid4",
        return_value=SimpleNamespace(hex="task"),
    ):
        result = prepare_form(
            store,
            tmp_path,
            ResourceBudget(10000, 1000, 10000),
            inputs,
            "request",
            "private-prompt",
            "model",
            "endpoint",
            "group",
        )
    digest = result["input_digest"]
    task = Task("task", digest, "request", TaskStatus.READY)
    revision = Revision("task", 1, digest)
    prompt = PromptRevision.from_text("task", 1, 1, "form", "private-prompt")
    snapshot = ExecutionSnapshot.from_payload(
        read_prepared(store, "task", 1)["snapshot_payload"]
    )
    confirmation = Confirmation(
        "task",
        1,
        digest,
        prompt.prompt_digest,
        1,
        snapshot.execution_digest,
        confirmation_id="confirmation-1",
    )
    scope = OperationScope("generate", None, "six-view-composite", 3)
    store.transition_task("task", TaskStatus.AWAITING_CONFIRMATION)
    store.transition_task("task", TaskStatus.READY)
    service = LocalEligibility(store, tmp_path, ResourceBudget(10000, 1000, 10000))
    args = (task, revision, refs, prompt, snapshot, confirmation, "op", scope)
    return service, args


def test_matching_unverified_and_single_consumption(tmp_path: Path):
    service, args = setup(tmp_path)
    service.prepare(*args, expires_at=time.time() + 120)
    result = service.consume(*args)
    assert result.local_eligible and result.evidence_status == "unverified"
    assert not result.host_confirmation_verified and not result.business_authorized
    with pytest.raises(EligibilityError):
        service.consume(*args)


def test_mock_scope_is_one_composite_and_rejects_old_single_view(tmp_path: Path):
    service, args = setup(tmp_path)
    task, revision, refs, _, _, _, _, _ = args
    service.store.claim_operation("form-operation", "task", 1, "digest", "form-attempt")
    artifact = tmp_path / "artifact.png"
    Image.new("RGB", (2, 2), "blue").save(artifact)
    raw = artifact.read_bytes()
    artifact_digest = hashlib.sha256(raw).hexdigest()
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded',delivery_status='delivered' WHERE attempt_id='form-attempt'"
        )
        db.execute(
            "INSERT INTO artifact VALUES(?,?,?,?)",
            ("artifact", "form-attempt", str(artifact), artifact_digest),
        )
    save_review(
        service.store,
        FormReview(
            "review",
            "task",
            1,
            "artifact",
            artifact_digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    artwork = [
        {
            "ref_id": "a",
            "category": "artwork",
            "path": str(refs[0].path),
            "mime": "image/png",
        }
    ]
    with patch(
        "ecommerce_product_image_creator.preparation._review_is_trusted",
        return_value=True,
    ):
        result = prepare_mock(
            service.store,
            tmp_path,
            service.budget,
            "task",
            "artifact",
            "review",
            artwork,
            "six views",
            "model",
            "endpoint",
            "group",
        )
    task = Task("task", result["input_digest"], "request", TaskStatus.READY)
    revision = Revision("task", result["revision"], result["input_digest"])
    refs = [
        Reference(
            "artifact",
            ReferenceCategory.FORM,
            artifact,
            "image/png",
            len(raw),
            artifact_digest,
            task_id="task",
        ),
        refs[0],
    ]
    prompt = PromptRevision.from_text(
        "task", revision.revision, revision.revision, "mock", "six views"
    )
    snapshot = ExecutionSnapshot.from_payload(
        read_prepared(service.store, "task", revision.revision)["snapshot_payload"]
    )
    confirmation = Confirmation(
        "task",
        revision.revision,
        task.input_digest,
        prompt.prompt_digest,
        revision.revision,
        snapshot.execution_digest,
        confirmation_id="mock-confirm",
    )
    scope = OperationScope("generate", None, "six-view-composite", 3)
    service.store.transition_task("task", TaskStatus.READY)
    with patch(
        "ecommerce_product_image_creator.preparation._review_is_trusted",
        return_value=True,
    ):
        service.prepare(
            task,
            revision,
            refs,
            prompt,
            snapshot,
            confirmation,
            "mock-op",
            scope,
            expires_at=time.time() + 120,
        )
        assert (
            service.consume(
                task, revision, refs, prompt, snapshot, confirmation, "mock-op", scope
            ).evidence_status
            == "unverified"
        )
    with pytest.raises(EligibilityError):
        service.prepare(
            task,
            revision,
            refs,
            prompt,
            snapshot,
            replace(confirmation, confirmation_id="old"),
            "old-op",
            replace(scope, target_view="F"),
            expires_at=time.time() + 120,
        )
    with service.store._connect() as db:
        db.execute(
            "INSERT INTO local_eligibility(operation_id,confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-op",
                "legacy-confirm",
                "task",
                revision.revision,
                task.input_digest,
                revision.revision,
                prompt.prompt_digest,
                snapshot.execution_digest,
                replace(scope, target_view="F").digest(),
                time.time() + 120,
            ),
        )
    with pytest.raises(EligibilityError):
        service.consume(
            task,
            revision,
            refs,
            prompt,
            snapshot,
            replace(confirmation, confirmation_id="legacy-confirm"),
            "legacy-op",
            replace(scope, target_view="F"),
        )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT invalidated FROM local_eligibility WHERE operation_id='legacy-op'"
        ).fetchone() == (1,)


def test_missing_and_cross_task_rejected(tmp_path: Path):
    service, args = setup(tmp_path)
    task, revision, refs, prompt, snapshot, confirmation, op, scope = args
    with pytest.raises(EligibilityError):
        service.prepare(
            replace(task, task_id="missing"),
            revision,
            refs,
            prompt,
            snapshot,
            confirmation,
            op,
            scope,
            expires_at=time.time() + 120,
        )
    with pytest.raises(EligibilityError):
        service.prepare(
            task,
            replace(revision, task_id="other"),
            refs,
            prompt,
            snapshot,
            confirmation,
            op,
            scope,
            expires_at=time.time() + 120,
        )


def test_content_prompt_scope_and_snapshot_changes_rejected(tmp_path: Path):
    service, args = setup(tmp_path)
    task, revision, refs, prompt, snapshot, confirmation, op, scope = args
    service.prepare(*args, expires_at=time.time() + 120)
    for changed in [
        replace(scope, output="other"),
        replace(scope, max_attempts=2),
        replace(scope, operation="other"),
    ]:
        with pytest.raises(EligibilityError):
            service.consume(
                task, revision, refs, prompt, snapshot, confirmation, op, changed
            )
    for key in ("model", "endpoint", "group"):
        changed = ExecutionSnapshot.create(
            revision,
            prompt,
            "form",
            "changed" if key == "model" else snapshot.model,
            "changed" if key == "endpoint" else snapshot.endpoint,
            "changed" if key == "group" else snapshot.group,
        )
        with pytest.raises(EligibilityError):
            service.consume(
                task, revision, refs, prompt, changed, confirmation, op, scope
            )
    new_prompt = PromptRevision.from_text("task", 1, 2, "form", "different")
    with pytest.raises(EligibilityError):
        service.consume(
            task, revision, refs, new_prompt, snapshot, confirmation, op, scope
        )
    refs[0].path.write_bytes(b"changed")
    with pytest.raises(EligibilityError) as caught:
        service.consume(*args)
    assert "private-prompt" not in str(caught.value)


def test_expiry_new_revision_and_id_conflict(tmp_path: Path):
    service, args = setup(tmp_path)
    service.prepare(*args, expires_at=time.time() + 120)
    with pytest.raises(EligibilityError):
        service.prepare(
            *args[:-1], replace(args[-1], output="other"), expires_at=time.time() + 120
        )
    service.store.create_revision("task", 2, args[0].input_digest)
    with pytest.raises(EligibilityError):
        service.consume(*args)


def test_expired_confirmation_rejected(tmp_path: Path):
    service, args = setup(tmp_path)
    service.prepare(*args, expires_at=time.time() - 1)
    with pytest.raises(EligibilityError):
        service.consume(*args)


def test_concurrent_and_restart(tmp_path: Path):
    service, args = setup(tmp_path)
    service.prepare(*args, expires_at=time.time() + 120)

    def consume(_):
        try:
            return service.consume(*args).local_eligible
        except EligibilityError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(consume, range(8)))
    assert outcomes.count(True) == 1
    code = "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(db.execute('SELECT consumed FROM local_eligibility WHERE operation_id=?',('op',)).fetchone()[0])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "1"


def test_invalidation_and_existing_operation_conflict(tmp_path: Path):
    service, args = setup(tmp_path)
    service.prepare(*args, expires_at=time.time() + 120)
    service.invalidate("op")
    with pytest.raises(EligibilityError):
        service.consume(*args)
    service.store.claim_operation("existing", "task", 1, "different", "attempt")
    with pytest.raises(EligibilityError):
        service.prepare(*args[:-2], "existing", args[-1], expires_at=time.time() + 120)


def test_existing_database_records_survive_schema_extension(tmp_path: Path):
    store = Store(tmp_path / "old.sqlite")
    store.create_task("old", "legacy-digest", "legacy data")
    store.create_revision("old", 1, "legacy-digest")
    service, args = setup(tmp_path)
    reloaded = LocalEligibility(service.store, tmp_path, service.budget)
    with reloaded.store._connect() as db:
        assert db.execute(
            "SELECT input_digest,instructions FROM task WHERE task_id='old'"
        ).fetchone() == ("legacy-digest", "legacy data")
    reloaded.prepare(*args, expires_at=time.time() + 120)
    assert reloaded.consume(*args).evidence_status == "unverified"


def test_non_ready_task_rejected_without_exposing_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    service, args = setup(tmp_path)
    service.store.transition_task("task", TaskStatus.CLOSED)
    with pytest.raises(EligibilityError) as caught:
        service.prepare(*args, expires_at=time.time() + 120)
    assert "private-prompt" not in str(caught.value)
    assert "private-prompt" not in caplog.text


def test_observed_input_change_permanently_invalidates_confirmation(tmp_path: Path):
    service, args = setup(tmp_path)
    service.prepare(*args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        original = db.execute(
            "SELECT data FROM prepared_reference WHERE task_id='task' AND revision=1 AND position=0"
        ).fetchone()[0]
        db.execute(
            "UPDATE prepared_reference SET data=? WHERE task_id='task' AND revision=1 AND position=0",
            (b"different",),
        )
    with pytest.raises(EligibilityError):
        service.consume(*args)
    with service.store._connect() as db:
        db.execute(
            "UPDATE prepared_reference SET data=? WHERE task_id='task' AND revision=1 AND position=0",
            (original,),
        )
    with pytest.raises(EligibilityError):
        service.consume(*args)


def test_same_confirmation_cannot_be_reused_for_another_operation(tmp_path: Path):
    service, args = setup(tmp_path)
    service.prepare(*args, expires_at=time.time() + 120)
    service.consume(*args)
    with pytest.raises(EligibilityError):
        service.prepare(
            *args[:-2], "other-operation", args[-1], expires_at=time.time() + 120
        )
    renewed = replace(args[5], confirmation_id="confirmation-2")
    service.prepare(
        *args[:5], renewed, "other-operation", args[-1], expires_at=time.time() + 120
    )
    assert (
        service.consume(*args[:5], renewed, "other-operation", args[-1]).evidence_status
        == "unverified"
    )


def test_v2_cannot_inherit_existing_v1_confirmation(tmp_path: Path):
    service, args = setup(tmp_path)
    task, revision, _, prompt, _, confirmation, _, scope = args
    legacy = ExecutionSnapshot.create(
        revision, prompt, "form", "model", "endpoint", "group"
    )
    with service.store._connect() as db:
        db.execute(
            "INSERT INTO local_eligibility(operation_id,confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy",
                confirmation.confirmation_id,
                task.task_id,
                1,
                task.input_digest,
                1,
                prompt.prompt_digest,
                legacy.execution_digest,
                scope.digest(),
                time.time() + 120,
            ),
        )
    with pytest.raises(EligibilityError):
        service.prepare(*args, expires_at=time.time() + 120)
    with service.store._connect() as db:
        assert db.execute(
            "SELECT execution_digest FROM local_eligibility WHERE operation_id='legacy'"
        ).fetchone() == (legacy.execution_digest,)
