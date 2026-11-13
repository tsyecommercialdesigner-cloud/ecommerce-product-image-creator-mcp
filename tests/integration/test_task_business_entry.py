"""T039 task-level business decisions; all authorities are isolated fixtures."""

import time

import pytest
from PIL import Image
from unit.revision_fixtures import confirm_fixture_revision
from unit.test_confirmation import choice, make_task

from ecommerce_product_image_creator import business_choices, tasks
from ecommerce_product_image_creator.preparation import (
    PreparationError,
    prepare_form,
    prepare_mock,
    read_prepared,
)
from integration.test_mock_workflow import mock_fixture


def test_task_business_proposal_pause_apply_and_reassess(tmp_path, monkeypatch):
    store, budget, refs, initial = make_task(tmp_path)
    extra = tmp_path / "extra.png"
    Image.new("RGB", (3, 3), "green").save(extra)
    refs.append(
        {"ref_id": "extra", "category": "form", "path": str(extra), "mime": "image/png"}
    )
    decision = confirm_fixture_revision(
        store, tmp_path, budget, initial["task_id"], refs, monkeypatch
    )
    source = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "prompt",
        "model",
        "endpoint",
        "group",
        task_id=initial["task_id"],
        _revision_decision_id=decision,
    )
    task_id, revision = source["task_id"], source["revision"]
    tasks.propose_task_subset(
        store, task_id, revision, "form", "proposal", ["extra"], "fixture", "cost"
    )
    state = tasks.evaluate_task_business(store, task_id, revision, "form")
    assert state["status"] == "pending_confirmation"
    assert (
        read_prepared(store, task_id, revision)["execution_digest"]
        == source["execution_digest"]
    )
    tasks.decide_task_subset(
        store,
        task_id,
        revision,
        "form",
        "proposal",
        "decision",
        "accepted",
        "fixture",
        time.time() + 120,
    )
    with pytest.raises(ValueError):
        tasks.apply_task_subset(store, task_id, revision, "form", "proposal")
    monkeypatch.setattr(
        business_choices,
        "_subset_source_is_trusted",
        lambda p: (
            p["decision_id"] == "decision"
            and p["task_id"] == task_id
            and p["revision"] == revision
            and p["selected"] == ["extra"]
        ),
    )
    applied = tasks.apply_task_subset(store, task_id, revision, "form", "proposal")
    assert applied["revision"] == revision + 1
    assert (
        tasks.evaluate_task_business(store, task_id, applied["revision"], "form")[
            "status"
        ]
        == "ready"
    )
    assert read_prepared(store, task_id, revision)["stage_references"] == ["f", "extra"]
    assert read_prepared(store, task_id, applied["revision"])["stage_references"] == [
        "extra"
    ]
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 0


def test_task_structure_choice_and_revocation(tmp_path, monkeypatch):
    store, _, _, saved = make_task(tmp_path, conflicts=("seal differs",))
    task_id = saved["task_id"]
    state = tasks.evaluate_task_business(store, task_id, 1, "form")
    assert state["status"] == "pending_confirmation"
    assert state["structure_verified"] is False and state["unknown_structure"]
    selected = choice(
        saved,
        "uncertainty",
        {
            "conflicts": ["seal differs"],
            "assumptions": {"bottom": "W"},
            "mode": "preview",
        },
    )
    tasks.record_task_business_choice(store, "form", selected)
    assert (
        tasks.evaluate_task_business(store, task_id, 1, "form")["status"]
        == "pending_confirmation"
    )
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda c: c == selected)
    state = tasks.evaluate_task_business(store, task_id, 1, "form")
    assert state["status"] == "preview_only" and state["assumptions"] == {"bottom": "W"}
    business_choices.invalidate_choice(store, selected.choice_id, "revoked")
    assert (
        tasks.evaluate_task_business(store, task_id, 1, "form")["status"]
        == "pending_confirmation"
    )


def test_task_version_conflict_and_source_scope(tmp_path, monkeypatch):
    store, _, _, saved = make_task(tmp_path, versions=("v1", "v2"))
    task_id = saved["task_id"]
    assert (
        "packaging_version"
        in tasks.evaluate_task_business(store, task_id, 1, "form")["pending"]
    )
    decision = choice(saved, "version", {"version": "v1"})
    tasks.record_task_business_choice(store, "form", decision)
    assert (
        tasks.evaluate_task_business(store, task_id, 1, "form")["status"]
        == "pending_confirmation"
    )
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda c: c == decision)
    assert tasks.evaluate_task_business(store, task_id, 1, "form")["status"] == "ready"
    with pytest.raises(ValueError, match="business_identity_mismatch"):
        tasks.record_task_business_choice(store, "mock", decision)


def test_mock_fixed_selection_and_extra_artwork_subset(tmp_path, monkeypatch):
    store, budget, form, mock, _, _ = mock_fixture(tmp_path, monkeypatch)
    task_id = form["task_id"]
    state = tasks.evaluate_task_business(store, task_id, mock["revision"], "mock")
    assert state["status"] == "ready"
    assert state["selected"] == ["form-artifact", "artwork"]
    with store._connect() as db:
        before_attempts = db.execute("SELECT count(*) FROM attempt").fetchone()[0]
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='subset_proposal'"
        ).fetchone()
    extra = tmp_path / "art2.png"
    Image.new("RGB", (4, 4), "red").save(extra)
    artwork = [
        {
            "ref_id": name,
            "category": "artwork",
            "path": str(tmp_path / (name + ".png")),
            "mime": "image/png",
            "version": "v1",
        }
        for name in ("artwork", "art2")
    ]
    decision = confirm_fixture_revision(
        store,
        tmp_path,
        budget,
        task_id,
        artwork,
        monkeypatch,
        artifact_id="form-artifact",
    )
    args = (store, tmp_path, budget, task_id, "form-artifact", "form-review")
    suffix = (
        "mock prompt",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
    )
    two = prepare_mock(*args, artwork, *suffix, _revision_decision_id=decision)
    rev = two["revision"]
    assert (
        tasks.evaluate_task_business(store, task_id, rev, "mock")["status"] == "ready"
    )
    with pytest.raises(PreparationError):
        prepare_mock(*args, artwork[:1], *suffix)
    tasks.propose_task_subset(
        store,
        task_id,
        rev,
        "mock",
        "p",
        ["form-artifact", "artwork"],
        "fixture",
        "cost",
    )
    assert (
        tasks.evaluate_task_business(store, task_id, rev, "mock")["status"]
        == "pending_confirmation"
    )
    tasks.decide_task_subset(
        store, task_id, rev, "mock", "p", "d", "accepted", "fixture", time.time() + 120
    )
    with pytest.raises(ValueError):
        tasks.apply_task_subset(store, task_id, rev, "mock", "p")
    with pytest.raises(ValueError):
        tasks.apply_task_subset(store, task_id, rev, "form", "p")
    monkeypatch.setattr(
        business_choices,
        "_subset_source_is_trusted",
        lambda p: (
            p["decision_id"] == "d"
            and p["task_id"] == task_id
            and p["revision"] == rev
            and p["selected"] == ["form-artifact", "artwork"]
        ),
    )
    applied = tasks.apply_task_subset(store, task_id, rev, "mock", "p")
    assert (
        tasks.evaluate_task_business(store, task_id, applied["revision"], "mock")[
            "status"
        ]
        == "ready"
    )
    assert (
        tasks.evaluate_task_business(store, task_id, rev, "mock")["status"]
        == "stale_revision"
    )
    assert read_prepared(store, task_id, rev)["stage_references"] == [
        "form-artifact",
        "artwork",
        "art2",
    ]
    with store._connect() as db:
        assert (
            db.execute("SELECT count(*) FROM attempt").fetchone()[0] == before_attempts
        )
