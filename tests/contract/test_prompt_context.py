from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator.domain import PromptRevision
from ecommerce_product_image_creator.preparation import prepare_form
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.tasks import (
    read_prompt_context,
    validate_prompt_context,
)


def setup_task(tmp_path: Path):
    entries = []
    for category in ("artwork", "form"):
        path = tmp_path / f"{category}.png"
        Image.new("RGB", (4, 4), "red").save(path)
        entries.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(path),
                "mime": "image/png",
            }
        )
    store = Store(tmp_path / "state.sqlite")
    budget = ResourceBudget(100000, 10000, 100000)
    output = prepare_form(
        store,
        tmp_path,
        budget,
        entries,
        "request",
        "F L R B T Q",
        "model",
        "endpoint",
        "group",
    )
    return store, budget, entries, output


def test_form_prompt_context_is_bound_and_unverified(tmp_path: Path):
    store, _, _, output = setup_task(tmp_path)
    context = read_prompt_context(store, output["task_id"], 1)
    assert context.prompt.text == "F L R B T Q"
    assert context.snapshot.execution_digest == output["execution_digest"]
    assert context.snapshot.references[0][0] == "form"
    assert context.form_source is None
    assert context.confirmation_status == "unverified"
    assert context.authorization_status == "unverified"
    validate_prompt_context(store, context.prompt)


def test_same_version_different_text_and_cross_stage_or_task_rejected(tmp_path: Path):
    store, _, _, output = setup_task(tmp_path)
    context = read_prompt_context(store, output["task_id"], 1)
    for candidate in (
        PromptRevision.from_text(output["task_id"], 1, 1, "form", "changed"),
        replace(context.prompt, stage="mock"),
        replace(context.prompt, task_id="other"),
    ):
        with pytest.raises(ValueError):
            validate_prompt_context(store, candidate)


def test_prompt_revision_preserves_history_and_invalidates_confirmation(tmp_path: Path):
    store, budget, refs, first = setup_task(tmp_path)
    old = read_prompt_context(store, first["task_id"], 1)
    second = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "updated",
        "model",
        "endpoint",
        "group",
        task_id=first["task_id"],
    )
    new = read_prompt_context(
        Store(tmp_path / "state.sqlite"), first["task_id"], second["revision"]
    )
    assert (
        old.prompt.text == read_prompt_context(store, first["task_id"], 1).prompt.text
    )
    assert new.prompt.prompt_revision == second["revision"]
    assert new.snapshot.execution_digest != old.snapshot.execution_digest
    assert new.confirmation_status == "unverified"


@pytest.mark.parametrize("text,stage", [("   ", "form"), ("valid", "other")])
def test_invalid_prompt_rejected(tmp_path: Path, text: str, stage: str):
    store, budget, refs, first = setup_task(tmp_path)
    with pytest.raises(ValueError):
        PromptRevision.from_text(first["task_id"], 2, 2, stage, text)
    if stage == "form":
        with pytest.raises(ValueError):
            prepare_form(
                store,
                tmp_path,
                budget,
                refs,
                "request",
                text,
                "model",
                "endpoint",
                "group",
                task_id=first["task_id"],
            )


@pytest.mark.parametrize("changed", ["input", "model", "endpoint", "group"])
def test_execution_changes_create_new_version(
    tmp_path: Path, changed: str, monkeypatch
):
    from unit.revision_fixtures import confirm_fixture_revision

    store, budget, refs, first = setup_task(tmp_path)
    model, endpoint, group = "model", "endpoint", "group"
    if changed == "input":
        Image.new("RGB", (5, 5), "blue").save(refs[1]["path"])
    elif changed == "model":
        model = "other-model"
    elif changed == "endpoint":
        endpoint = "other-endpoint"
    else:
        group = "other-group"
    decision = (
        confirm_fixture_revision(
            store, tmp_path, budget, first["task_id"], refs, monkeypatch
        )
        if changed == "input"
        else None
    )
    second = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "F L R B T Q",
        model,
        endpoint,
        group,
        task_id=first["task_id"],
        _revision_decision_id=decision,
    )
    assert second["revision"] != first["revision"]
    assert second["execution_digest"] != first["execution_digest"]
    assert (
        read_prompt_context(store, first["task_id"], 1).snapshot.execution_digest
        == first["execution_digest"]
    )
    assert (
        read_prompt_context(store, first["task_id"], 2).authorization_status
        == "unverified"
    )


def test_prompt_context_recovers_in_new_process(tmp_path: Path):
    import subprocess
    import sys

    _, _, _, output = setup_task(tmp_path)
    code = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.tasks import read_prompt_context; c=read_prompt_context(Store(Path(sys.argv[1])),sys.argv[2],1); print(c.prompt.prompt_digest, c.snapshot.execution_digest, c.prompt.stage)"
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "state.sqlite"), output["task_id"]],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (
        child.stdout.strip()
        == f"{output['prompt_digest']} {output['execution_digest']} form"
    )
