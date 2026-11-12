"""Stage-boundary contract tests, entirely local."""

from pathlib import Path

import pytest
from integration.test_mock_workflow import mock_fixture
from PIL import Image

from ecommerce_product_image_creator.preparation import (
    PreparationError,
    prepare_mock,
    read_prepared,
)
from ecommerce_product_image_creator.provider import build_request

from .test_prepare_inputs import prepare, setup_inputs


@pytest.mark.parametrize("count", [1, 2, 6])
def test_form_excludes_artwork_and_prior_candidate(tmp_path: Path, count: int):
    refs = setup_inputs(tmp_path, 1, count)
    from ecommerce_product_image_creator.preparation import prepare_form
    from ecommerce_product_image_creator.storage import ResourceBudget, Store

    store = Store(tmp_path / "state.sqlite")
    result = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "form",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
    )
    request = build_request(store, result["task_id"], 1)
    saved = read_prepared(store, result["task_id"], 1)
    assert saved["stage_references"] == [
        r["ref_id"] for r in refs if r["category"] == "form"
    ]
    assert len(request.images) == count
    assert all(
        r["category"] == "form"
        for r in refs
        if r["ref_id"] in saved["stage_references"]
    )
    assert "F/L/R/B/T/Q" in result["form_output_contract"][0]
    assert "top left" in result["form_output_contract"][1]
    assert "orthographic" in result["form_output_contract"][2]
    assert "previous candidate" in result["form_output_contract"][3]


@pytest.mark.parametrize("count", [0, 7])
def test_form_rejects_count_outside_contract(tmp_path: Path, count: int):
    with pytest.raises(PreparationError):
        prepare(tmp_path, setup_inputs(tmp_path, 1, count))


def test_mock_only_form_composite_and_two_artwork(tmp_path, monkeypatch):
    from unit.revision_fixtures import confirm_fixture_revision

    store, budget, form, mock, _, _ = mock_fixture(tmp_path, monkeypatch)
    extra = tmp_path / "art2.png"
    Image.new("RGB", (4, 4), "red").save(extra)
    artwork = [
        {
            "ref_id": "artwork",
            "category": "artwork",
            "path": str(tmp_path / "artwork.png"),
            "mime": "image/png",
            "version": "v1",
        },
        {
            "ref_id": "art2",
            "category": "artwork",
            "path": str(extra),
            "mime": "image/png",
            "version": "v1",
        },
    ]
    decision = confirm_fixture_revision(
        store,
        tmp_path,
        budget,
        form["task_id"],
        artwork,
        monkeypatch,
        artifact_id="form-artifact",
    )
    second = prepare_mock(
        store,
        tmp_path,
        budget,
        form["task_id"],
        "form-artifact",
        "form-review",
        artwork,
        "mock",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        _revision_decision_id=decision,
    )
    first = read_prepared(store, form["task_id"], mock["revision"])
    saved = read_prepared(store, form["task_id"], second["revision"])
    assert first["stage_references"] == ["form-artifact", "artwork"]
    assert saved["stage_references"] == ["form-artifact", "artwork", "art2"]
    assert len(build_request(store, form["task_id"], second["revision"]).images) == 3
    assert "form" not in saved["stage_references"]
    assert first["execution_digest"] != saved["execution_digest"]
