import hashlib
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator.domain import UpstreamStatus
from ecommerce_product_image_creator.form_workflow import (
    FormWorkflowError,
    link_form_revision,
    read_form_state,
    register_form_artifact,
)
from ecommerce_product_image_creator.preparation import prepare_form
from ecommerce_product_image_creator.storage import ResourceBudget, Store


def fixture_task(tmp_path: Path):
    refs = []
    for category in ("artwork", "form"):
        path = tmp_path / f"{category}.png"
        Image.new("RGB", (4, 4)).save(path)
        refs.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(path),
                "mime": "image/png",
            }
        )
    store = Store(tmp_path / "state.sqlite")
    budget = ResourceBudget(100000, 10000, 100000)
    task = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "F L R B T Q",
        "model",
        "endpoint",
        "group",
    )
    store.claim_operation(
        "operation", task["task_id"], 1, task["execution_digest"], "attempt"
    )
    return store, budget, refs, task


def test_unresolved_or_failed_upstream_cannot_register(tmp_path: Path):
    store, budget, _, task = fixture_task(tmp_path)
    image = tmp_path / "artifact.png"
    Image.new("RGB", (4, 4)).save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    with pytest.raises(FormWorkflowError):
        register_form_artifact(
            store,
            task["task_id"],
            1,
            "attempt",
            "artifact",
            tmp_path,
            image,
            "image/png",
            digest,
            budget,
        )
    assert (
        read_form_state(store, task["task_id"])["attempts"][0]["upstream"]
        == "submission_uncertain"
    )


@pytest.mark.parametrize(
    "status",
    [
        UpstreamStatus.ACCEPTED,
        UpstreamStatus.RUNNING,
        UpstreamStatus.FAILED,
        UpstreamStatus.UNKNOWN,
    ],
)
def test_only_upstream_success_can_register(tmp_path: Path, status: UpstreamStatus):
    store, budget, _, task = fixture_task(tmp_path)
    image = tmp_path / "artifact.png"
    Image.new("RGB", (4, 4)).save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    store.record_upstream("attempt", status, "upstream-id")
    assert (
        read_form_state(store, task["task_id"])["attempts"][0]["upstream"]
        == status.value
    )
    with pytest.raises(FormWorkflowError):
        register_form_artifact(
            store,
            task["task_id"],
            1,
            "attempt",
            "artifact",
            tmp_path,
            image,
            "image/png",
            digest,
            budget,
        )


def test_one_attempt_one_artifact_and_review_pending(tmp_path: Path):
    store, budget, _, task = fixture_task(tmp_path)
    image = tmp_path / "artifact.png"
    Image.new("RGB", (4, 4)).save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    store.record_upstream("attempt", UpstreamStatus.SUCCEEDED, "upstream-id")
    register_form_artifact(
        store,
        task["task_id"],
        1,
        "attempt",
        "artifact",
        tmp_path,
        image,
        "image/png",
        digest,
        budget,
    )
    state = read_form_state(Store(tmp_path / "state.sqlite"), task["task_id"])
    assert state["attempts"][0]["upstream"] == "succeeded"
    assert state["attempts"][0]["delivery"] == "delivered"
    assert state["attempts"][0]["review"] == "pending"
    with pytest.raises(FormWorkflowError):
        register_form_artifact(
            store,
            task["task_id"],
            1,
            "attempt",
            "second",
            tmp_path,
            image,
            "image/png",
            digest,
            budget,
        )


def test_revision_reason_and_previous_source_are_preserved(tmp_path: Path):
    import subprocess
    import sys

    store, budget, refs, task = fixture_task(tmp_path)
    revised = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "revised",
        "model",
        "endpoint",
        "group",
        task_id=task["task_id"],
    )
    link_form_revision(
        store, task["task_id"], revised["revision"], 1, "attempt", "adjust seal"
    )
    state = read_form_state(Store(tmp_path / "state.sqlite"), task["task_id"])
    assert state["revision"] == 2
    assert state["revision_origin"] == {
        "revision": 1,
        "attempt_id": "attempt",
        "reason": "adjust seal",
    }
    code = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.form_workflow import read_form_state; s=read_form_state(Store(Path(sys.argv[1])),sys.argv[2]); print(s['revision'], s['revision_origin']['attempt_id'], s['revision_origin']['reason'])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "state.sqlite"), task["task_id"]],
        check=True,
        text=True,
        capture_output=True,
    )
    assert child.stdout.strip() == "2 attempt adjust seal"
    with pytest.raises(FormWorkflowError):
        link_form_revision(store, task["task_id"], 2, 1, "attempt", "different")


def test_upstream_success_and_missing_file_are_separate(tmp_path: Path):
    store, budget, _, task = fixture_task(tmp_path)
    image = tmp_path / "artifact.png"
    Image.new("RGB", (4, 4)).save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    store.record_upstream("attempt", UpstreamStatus.SUCCEEDED, "upstream-id")
    state = read_form_state(store, task["task_id"])
    assert state["attempts"][0]["upstream"] == "succeeded"
    assert state["attempts"][0]["delivery"] == "not_available"
    register_form_artifact(
        store,
        task["task_id"],
        1,
        "attempt",
        "artifact",
        tmp_path,
        image,
        "image/png",
        digest,
        budget,
    )
    assert (
        read_form_state(store, task["task_id"], artifact_root=tmp_path, budget=budget)[
            "attempts"
        ][0]["file_state"]
        == "readable"
    )
    image.unlink()
    state = read_form_state(
        Store(tmp_path / "state.sqlite"),
        task["task_id"],
        artifact_root=tmp_path,
        budget=budget,
    )
    assert state["attempts"][0]["upstream"] == "succeeded"
    assert state["attempts"][0]["delivery"] == "delivered"
    assert state["attempts"][0]["file_state"] == "missing_or_corrupt"
    assert state["attempts"][0]["review_applicability"] == "invalid_artifact"


def test_unknown_and_counter_persist_across_revision(tmp_path: Path):
    store, budget, refs, task = fixture_task(tmp_path)
    store.record_upstream("attempt", UpstreamStatus.UNKNOWN, "upstream-id")
    assert store.increment_attempt_counter("operation", "model", "endpoint") == 1
    revised = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "revision",
        "model",
        "endpoint",
        "group",
        task_id=task["task_id"],
    )
    link_form_revision(
        store, task["task_id"], revised["revision"], 1, "attempt", "human request"
    )
    operation = store.get_operation("operation")
    assert operation is not None
    assert operation.upstream_status == "unknown"
    assert store.increment_attempt_counter("operation", "model", "endpoint") == 2
    assert read_form_state(store, task["task_id"])["attempts"] == []
    assert (
        read_form_state(store, task["task_id"], revision=1)["attempts"][0]["upstream"]
        == "unknown"
    )


def test_wrong_digest_and_cross_task_rejected(tmp_path: Path):
    store, budget, _, task = fixture_task(tmp_path)
    image = tmp_path / "artifact.png"
    Image.new("RGB", (4, 4)).save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    store.record_upstream("attempt", UpstreamStatus.SUCCEEDED, "upstream-id")
    for identity, supplied in [(task["task_id"], "a" * 64), ("other-task", digest)]:
        with pytest.raises(FormWorkflowError):
            register_form_artifact(
                store,
                identity,
                1,
                "attempt",
                "artifact",
                tmp_path,
                image,
                "image/png",
                supplied,
                budget,
            )


def test_concurrent_registration_has_single_winner(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor

    store, budget, _, task = fixture_task(tmp_path)
    image = tmp_path / "artifact.png"
    Image.new("RGB", (4, 4)).save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    store.record_upstream("attempt", UpstreamStatus.SUCCEEDED, "upstream-id")

    def register(artifact_id: str) -> bool:
        try:
            register_form_artifact(
                Store(tmp_path / "state.sqlite"),
                task["task_id"],
                1,
                "attempt",
                artifact_id,
                tmp_path,
                image,
                "image/png",
                digest,
                budget,
            )
            return True
        except FormWorkflowError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(register, ["one", "two"]))
    assert sorted(outcomes) == [False, True]
