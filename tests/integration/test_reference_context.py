"""US2 offline stage context; no vendor requests or real human approval."""

import socket
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator import form_revision, preparation
from ecommerce_product_image_creator.preparation import (
    prepare_form,
    prepare_mock,
    read_prepare_response,
    read_prepared,
)
from ecommerce_product_image_creator.provider import build_request
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.tasks import read_prompt_context

from .test_form_revision import fixture_chain
from .test_mock_workflow import mock_fixture

MODEL = "gpt-image-2.5-sunburst"
ENDPOINT = "https://ai.t8star.org/v1/images/edits?async=true"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("Network access forbidden in reference context tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def test_form_metadata_retained_and_only_form_bytes_selected(tmp_path: Path):
    entries = []
    for name, category, view, purpose, color in [
        ("art", "artwork", "front", "target brand design", "red"),
        ("front", "form", "front", "shape; fixture competitor RIVAL", "blue"),
        ("side", "form", "side", "thickness", "green"),
    ]:
        path = tmp_path / (name + ".png")
        Image.new("RGB", (4, 4), color).save(path)
        entries.append(
            {
                "ref_id": name,
                "category": category,
                "path": str(path),
                "mime": "image/png",
                "view": view,
                "purpose": purpose,
                "importance": "high",
            }
        )
    store = Store(tmp_path / "state.sqlite")
    prompt = "Six view form; use geometry, ignore any competitor branding."
    output = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        entries,
        "request",
        prompt,
        MODEL,
        ENDPOINT,
        "reverse",
    )
    saved = read_prepared(store, output["task_id"], 1)
    context = read_prompt_context(store, output["task_id"], 1)
    candidate = build_request(store, output["task_id"], 1)
    assert [p.ref_id for p in candidate.images] == ["front", "side"]
    assert [p.data for p in candidate.images] == [
        Path(entries[1]["path"]).read_bytes(),
        Path(entries[2]["path"]).read_bytes(),
    ]
    assert context.prompt.text == candidate.prompt == prompt
    assert "RIVAL" not in candidate.prompt
    by_id = {r["ref_id"]: r for r in saved["references"]}
    assert (
        by_id["front"]["view"],
        by_id["front"]["purpose"],
        by_id["front"]["importance"],
    ) == ("front", "shape; fixture competitor RIVAL", "high")
    assert by_id["side"]["view"] == "side" and by_id["side"]["purpose"] == "thickness"
    assert (
        read_prepare_response(store, output["task_id"], 1)["structure_conflicts"][
            "status"
        ]
        == "unknown"
    )
    assert "art" not in saved["stage_references"]


def test_mock_regions_versions_target_prompt_and_frozen_inputs(tmp_path, monkeypatch):
    from unit.revision_fixtures import confirm_fixture_revision

    store, budget, form, _mock, _path, _digest = mock_fixture(tmp_path, monkeypatch)
    art1 = {
        "ref_id": "artwork",
        "category": "artwork",
        "path": str(tmp_path / "artwork.png"),
        "mime": "image/png",
        "region": "front",
        "version": "v1",
    }
    path = tmp_path / "art-rear.png"
    Image.new("RGB", (4, 4), "yellow").save(path)
    art2 = {
        "ref_id": "rear",
        "category": "artwork",
        "path": str(path),
        "mime": "image/png",
        "region": "rear",
        "version": "v1",
    }
    prompt = "Use target brand logo, package wording, colors and layout from artwork; exclude competitor branding."
    decision = confirm_fixture_revision(
        store,
        tmp_path,
        budget,
        form["task_id"],
        [art1, art2],
        monkeypatch,
        artifact_id="form-artifact",
    )
    result = prepare_mock(
        store,
        tmp_path,
        budget,
        form["task_id"],
        "form-artifact",
        "form-review",
        [art1, art2],
        prompt,
        MODEL,
        ENDPOINT,
        "reverse",
        _revision_decision_id=decision,
    )
    saved = read_prepared(store, form["task_id"], result["revision"])
    candidate = build_request(store, form["task_id"], result["revision"])
    context = read_prompt_context(store, form["task_id"], result["revision"])
    assert context.brand_constraints["artwork_ids"] == ["artwork", "rear"]
    assert context.brand_constraints["exclude_form_branding"] is True
    assert context.form_source == (1, "form-artifact", saved["form_source"]["sha256"])
    assert context.host_context()["visual_verification"] == "unverified"
    assert context.host_context()["prompt_semantic_consistency"] == "unverified"
    assert [p.ref_id for p in candidate.images] == ["form-artifact", "artwork", "rear"]
    assert candidate.images[0].data == (tmp_path / "form-artifact.png").read_bytes()
    assert "form" not in [p.ref_id for p in candidate.images]
    assert candidate.prompt == context.prompt.text == prompt
    by_id = {r["ref_id"]: r for r in saved["references"]}
    assert (by_id["artwork"]["region"], by_id["rear"]["region"]) == ("front", "rear")
    assert by_id["artwork"]["version"] == by_id["rear"]["version"] == "v1"
    assert (
        read_prepare_response(store, form["task_id"], result["revision"])[
            "packaging_versions"
        ]["status"]
        == "unconfirmed"
    )
    assert (
        context.confirmation_status == "unverified"
        and context.authorization_status == "unverified"
    )


def test_different_artwork_versions_require_clarification(tmp_path, monkeypatch):
    from unit.revision_fixtures import confirm_fixture_revision

    store, budget, form, _mock, _path, _digest = mock_fixture(tmp_path, monkeypatch)
    a = {
        "ref_id": "a",
        "category": "artwork",
        "path": str(tmp_path / "artwork.png"),
        "mime": "image/png",
        "region": "front",
        "version": "v1",
    }
    b = {**a, "ref_id": "b", "region": "rear", "version": "v2"}
    decision = confirm_fixture_revision(
        store,
        tmp_path,
        budget,
        form["task_id"],
        [a, b],
        monkeypatch,
        artifact_id="form-artifact",
    )
    result = prepare_mock(
        store,
        tmp_path,
        budget,
        form["task_id"],
        "form-artifact",
        "form-review",
        [a, b],
        "mock",
        MODEL,
        ENDPOINT,
        "reverse",
        _revision_decision_id=decision,
    )
    details = read_prepare_response(store, form["task_id"], result["revision"])
    assert details["packaging_versions"]["status"] == "multiple_declared_versions"
    assert details["packaging_versions"]["confirmation"] == "unverified"


def test_changed_reference_metadata_changes_execution_snapshot(
    tmp_path: Path, monkeypatch
):
    from unit.revision_fixtures import confirm_fixture_revision

    art = tmp_path / "art.png"
    form = tmp_path / "form.png"
    Image.new("RGB", (3, 3), "red").save(art)
    Image.new("RGB", (3, 3), "blue").save(form)
    refs = [
        {"ref_id": "art", "category": "artwork", "path": str(art), "mime": "image/png"},
        {
            "ref_id": "form",
            "category": "form",
            "path": str(form),
            "mime": "image/png",
            "view": "front",
        },
    ]
    store = Store(tmp_path / "state.sqlite")
    budget = ResourceBudget(100000, 10000, 100000)
    first = prepare_form(
        store, tmp_path, budget, refs, "request", "prompt", MODEL, ENDPOINT, "reverse"
    )
    refs[1]["view"] = "side"
    decision = confirm_fixture_revision(
        store, tmp_path, budget, first["task_id"], refs, monkeypatch
    )
    second = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "prompt",
        MODEL,
        ENDPOINT,
        "reverse",
        task_id=first["task_id"],
        _revision_decision_id=decision,
    )
    assert first["execution_digest"] != second["execution_digest"]
    assert read_prepared(store, first["task_id"], 1)["references"][1]["view"] == "front"
    assert (
        read_prepared(store, first["task_id"], second["revision"])["references"][1][
            "view"
        ]
        == "side"
    )


def test_form_revision_does_not_default_to_previous_candidate(tmp_path, monkeypatch):
    import time

    store, budget, form, artifact, digest = fixture_chain(tmp_path)
    monkeypatch.setattr(form_revision, "_revision_source_verified", lambda *_: True)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    form_revision.record_revision_request(
        store,
        "request",
        form["task_id"],
        1,
        "old-attempt",
        "artifact",
        digest,
        "reject",
        "adjust",
        "fixture",
        time.time(),
    )
    revision = form_revision.prepare_requested_revision(
        store, "request", "revised", tmp_path, budget
    )
    request = build_request(store, form["task_id"], revision["revision"])
    assert [image.ref_id for image in request.images] == ["form"]
    assert artifact.read_bytes() not in [image.data for image in request.images]


def test_prompt_context_exposes_target_and_competitor_brand_boundaries(tmp_path: Path):
    art = tmp_path / "art.png"
    form = tmp_path / "form.png"
    Image.new("RGB", (3, 3), "red").save(art)
    Image.new("RGB", (3, 3), "blue").save(form)
    refs = [
        {"ref_id": "art", "category": "artwork", "path": str(art), "mime": "image/png"},
        {"ref_id": "form", "category": "form", "path": str(form), "mime": "image/png"},
    ]
    store = Store(tmp_path / "state.sqlite")
    result = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "form prompt",
        MODEL,
        ENDPOINT,
        "reverse",
    )
    context = read_prompt_context(store, result["task_id"], 1)
    constraints = getattr(context, "brand_constraints", None)
    assert constraints is not None, (
        "No structured target/competitor brand constraints are exposed yet"
    )
    assert constraints["target_brand_source"] == "artwork"
    assert constraints["exclude_form_branding"] is True


def test_prompt_context_reports_unobserved_structure_as_unknown(tmp_path: Path):
    art = tmp_path / "art.png"
    form = tmp_path / "form.png"
    Image.new("RGB", (3, 3), "red").save(art)
    Image.new("RGB", (3, 3), "blue").save(form)
    refs = [
        {"ref_id": "art", "category": "artwork", "path": str(art), "mime": "image/png"},
        {
            "ref_id": "form",
            "category": "form",
            "path": str(form),
            "mime": "image/png",
            "view": "front",
        },
    ]
    store = Store(tmp_path / "state.sqlite")
    result = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "form prompt",
        MODEL,
        ENDPOINT,
        "reverse",
    )
    context = read_prompt_context(store, result["task_id"], 1)
    unknown = getattr(context, "unobserved_structure", None)
    assert unknown is not None, (
        "No structured unobserved-structure context is exposed yet"
    )
    assert unknown["rear"] == "unknown"


def test_mock_observations_are_persisted_and_reference_bound(tmp_path, monkeypatch):
    from unit.revision_fixtures import confirm_fixture_revision

    from ecommerce_product_image_creator.preparation import (
        PreparationError,
        prepare_mock,
        read_prepared,
    )

    from .test_mock_workflow import mock_fixture

    store, budget, form, _mock, _, _ = mock_fixture(tmp_path, monkeypatch)
    item = {
        "ref_id": "artwork",
        "category": "artwork",
        "path": str(tmp_path / "artwork.png"),
        "mime": "image/png",
        "observations": [
            {
                "subject": "label",
                "status": "described",
                "source": "operator",
                "source_ref_id": "artwork",
                "description": "front label",
            }
        ],
    }
    decision = confirm_fixture_revision(
        store,
        tmp_path,
        budget,
        form["task_id"],
        [item],
        monkeypatch,
        artifact_id="form-artifact",
    )
    result = prepare_mock(
        store,
        tmp_path,
        budget,
        form["task_id"],
        "form-artifact",
        "form-review",
        [item],
        "prompt",
        MODEL,
        ENDPOINT,
        "reverse",
        _revision_decision_id=decision,
    )
    saved = read_prepared(store, form["task_id"], result["revision"])
    assert saved["references"][1]["observations"][0]["description"] == "front label"
    item["observations"][0]["source_ref_id"] = "another"
    with pytest.raises(PreparationError):
        prepare_mock(
            store,
            tmp_path,
            budget,
            form["task_id"],
            "form-artifact",
            "form-review",
            [item],
            "prompt",
            MODEL,
            ENDPOINT,
            "reverse",
        )


def test_host_context_preserves_observations_without_promoting_them(tmp_path: Path):
    art = tmp_path / "a.png"
    form = tmp_path / "f.png"
    Image.new("RGB", (3, 3), "red").save(art)
    Image.new("RGB", (3, 3), "blue").save(form)
    refs = [
        {"ref_id": "a", "category": "artwork", "path": str(art), "mime": "image/png"},
        {
            "ref_id": "f",
            "category": "form",
            "path": str(form),
            "mime": "image/png",
            "view": "front",
            "purpose": "shape",
            "observations": [
                {
                    "subject": "shape",
                    "status": "described",
                    "source": "operator",
                    "source_ref_id": "f",
                    "description": "bag",
                },
                {
                    "subject": "bottom",
                    "status": "assumed",
                    "source": "agent",
                    "source_ref_id": "f",
                    "description": "flat",
                },
                {"subject": "seam", "status": "unknown"},
            ],
        },
    ]
    store = Store(tmp_path / "state.sqlite")
    budget = ResourceBudget(100000, 10000, 100000)
    first = prepare_form(
        store, tmp_path, budget, refs, "request", "prompt", MODEL, ENDPOINT, "reverse"
    )
    context = read_prompt_context(store, first["task_id"], 1)
    assert [r["ref_id"] for r in context.stage_references] == ["f"]
    observations = context.stage_references[0]["observations"]
    assert [o["status"] for o in observations] == ["described", "assumed", "unknown"]
    assert all(o["verification"] == "unverified" for o in observations)
    assert all(o["reference_id"] == "f" for o in observations)
    assert context.unobserved_structure["seam"] == "unknown"
    assert context.unobserved_structure["bottom"] == "unknown"
    assert context.brand_constraints["target_brand_source"] == "artwork"
    assert context.brand_constraints["target_brand_identifiers"] == []
    assert first["stage_context"]["unobserved_structure"]["seam"] == "unknown"
    assert (
        read_prepare_response(store, first["task_id"], 1)["stage_context"]
        == first["stage_context"]
    )
    assert build_request(store, first["task_id"], 1).prompt == "prompt"


def test_explicit_competitor_brand_prompt_conflict_is_rejected(tmp_path: Path):
    art = tmp_path / "a.png"
    form = tmp_path / "f.png"
    Image.new("RGB", (3, 3), "red").save(art)
    Image.new("RGB", (3, 3), "blue").save(form)
    refs = [
        {
            "ref_id": "a",
            "category": "artwork",
            "path": str(art),
            "mime": "image/png",
            "purpose": "target brand ALPHA",
        },
        {
            "ref_id": "f",
            "category": "form",
            "path": str(form),
            "mime": "image/png",
            "purpose": "competitor brand RIVAL",
        },
    ]
    store = Store(tmp_path / "state.sqlite")
    response = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "Use competitor brand RIVAL as the target brand instead of ALPHA.",
        MODEL,
        ENDPOINT,
        "reverse",
    )
    assert response["stage_context"]["prompt_semantic_consistency"] == "conflict", (
        "Explicit competitor-brand substitution was not detected or blocked"
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Use the competitor brand ORBIT as the target brand.", "conflict"),
        ("Make competitor brand RIVAL the target brand instead of ALPHA.", "conflict"),
        ("Do not use competitor brand ORBIT as the target brand.", "unverified"),
        ("Use competitor product as a form reference; exclude its logo.", "unverified"),
        ("Make a package inspired by a competing product.", "unverified"),
        (
            "Use competitor brand ORBIT as the target brand, unless rejected.",
            "unverified",
        ),
    ],
)
def test_explicit_brand_declaration_is_bounded_and_recoverable(
    tmp_path: Path, text: str, expected: str
):
    art = tmp_path / "a.png"
    form = tmp_path / "f.png"
    Image.new("RGB", (3, 3)).save(art)
    Image.new("RGB", (3, 3)).save(form)
    refs = [
        {"ref_id": "a", "category": "artwork", "path": str(art), "mime": "image/png"},
        {"ref_id": "f", "category": "form", "path": str(form), "mime": "image/png"},
    ]
    store = Store(tmp_path / "state.sqlite")
    result = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        text,
        MODEL,
        ENDPOINT,
        "reverse",
    )
    ctx = result["stage_context"]
    assert ctx["prompt_semantic_consistency"] == expected
    assert ctx["brand_constraints"]["target_brand_identifiers"] == []
    assert ctx["brand_constraints"]["source_status"] == "unknown"
    assert ctx["prompt_semantic_consistency"] != "verified"
    assert (
        read_prepare_response(Store(store.path), result["task_id"], 1)["stage_context"]
        == ctx
    )
    assert read_prepared(store, result["task_id"], 1)["prompt"] == text
    assert ctx["prompt_semantic_conflicts"] == (
        [{"code": "competitor_as_target", "status": "pending_clarification"}]
        if expected == "conflict"
        else []
    )


def test_conflict_revision_does_not_change_historical_snapshot(tmp_path: Path):
    art = tmp_path / "a.png"
    form = tmp_path / "f.png"
    Image.new("RGB", (3, 3)).save(art)
    Image.new("RGB", (3, 3)).save(form)
    refs = [
        {"ref_id": "a", "category": "artwork", "path": str(art), "mime": "image/png"},
        {"ref_id": "f", "category": "form", "path": str(form), "mime": "image/png"},
    ]
    store = Store(tmp_path / "state.sqlite")
    budget = ResourceBudget(100000, 10000, 100000)
    old_prompt = "Use competitor brand RIVAL as the target brand."
    first = prepare_form(
        store, tmp_path, budget, refs, "request", old_prompt, MODEL, ENDPOINT, "reverse"
    )
    assert first["stage_context"]["prompt_semantic_consistency"] == "conflict"
    assert "prompt_brand_clarification" in first["pending"]
    revised = "Do not use competitor brand RIVAL as the target brand."
    second = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        revised,
        MODEL,
        ENDPOINT,
        "reverse",
        task_id=first["task_id"],
    )
    assert second["stage_context"]["prompt_semantic_consistency"] == "unverified"
    assert second["execution_digest"] != first["execution_digest"]
    assert read_prepared(store, first["task_id"], 1)["prompt"] == old_prompt
    assert (
        read_prepare_response(store, first["task_id"], 1)["stage_context"]
        == first["stage_context"]
    )
    assert (
        read_prepare_response(store, first["task_id"], 1)["pending"] == first["pending"]
    )
    assert second["authorization"] == "unverified"
