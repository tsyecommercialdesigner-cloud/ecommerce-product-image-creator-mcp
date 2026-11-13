"""T036 local business-choice contract; fixture verifiers are not real Host approval."""

import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator import business_choices
from ecommerce_product_image_creator.preparation import prepare_form, read_prepared
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from unit.revision_fixtures import confirm_fixture_revision


def make_task(root: Path, *, versions=("v1", "v1"), conflicts=()):
    refs = []
    for name, category, version, region in [
        ("a", "artwork", versions[0], "front"),
        ("b", "artwork", versions[1], "rear"),
        ("f", "form", None, None),
    ]:
        path = root / (name + ".png")
        Image.new("RGB", (3, 3), name == "f" and "blue" or "red").save(path)
        item = {
            "ref_id": name,
            "category": category,
            "path": str(path),
            "mime": "image/png",
        }
        if version:
            item.update(version=version, region=region)
        refs.append(item)
    store = Store(root / "state.sqlite")
    budget = ResourceBudget(100000, 10000, 100000)
    prepared = prepare_form(
        store,
        root,
        budget,
        refs,
        "request",
        "prompt",
        "model",
        "endpoint",
        "group",
        structure_conflicts=list(conflicts),
    )
    return store, budget, refs, prepared


def choice(prepared, kind, payload, name="choice"):
    return business_choices.BusinessChoice(
        name,
        prepared["task_id"],
        prepared["revision"],
        prepared["input_digest"],
        prepared["execution_digest"],
        kind,
        payload,
        "fixture",
        time.time(),
    )


def test_same_version_different_regions_does_not_pause(tmp_path: Path):
    store, _, _, p = make_task(tmp_path)
    result = business_choices.evaluate_choices(store, p["task_id"], p["revision"])
    assert result["status"] == "ready" and result["unknown_structure"]
    assert result["authorization"] == "unverified"


def test_version_conflict_requires_current_trusted_choice(tmp_path: Path, monkeypatch):
    store, _, _, p = make_task(tmp_path, versions=("v1", "v2"))
    assert (
        business_choices.evaluate_choices(store, p["task_id"], 1)["status"] == "pending"
    )
    c = choice(p, "version", {"version": "v1"})
    business_choices.record_choice(store, c)
    assert (
        business_choices.evaluate_choices(
            store, p["task_id"], 1, choice_ids=["choice"]
        )["status"]
        == "pending"
    )
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    result = business_choices.evaluate_choices(
        store, p["task_id"], 1, choice_ids=["choice"]
    )
    assert result["status"] == "ready" and result["authorization"] == "unverified"
    assert result["chosen_version"] == "v1"


def test_structure_preview_is_unverified_and_scope_bound(tmp_path: Path, monkeypatch):
    store, _, _, p = make_task(tmp_path, conflicts=("seal differs",))
    assert (
        business_choices.evaluate_choices(store, p["task_id"], 1)["status"] == "pending"
    )
    c = choice(
        p,
        "uncertainty",
        {
            "conflicts": ["seal differs"],
            "assumptions": {"bottom": "W"},
            "mode": "preview",
        },
    )
    business_choices.record_choice(store, c)
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    result = business_choices.evaluate_choices(
        store, p["task_id"], 1, choice_ids=["choice"]
    )
    assert result["status"] == "preview" and result["structure_verified"] is False
    assert (
        result["assumptions"] == {"bottom": "W"}
        and result["authorization"] == "unverified"
    )


def test_subset_proposal_never_silently_changes_snapshot(tmp_path: Path, monkeypatch):
    store, _, _, p = make_task(tmp_path)
    result = business_choices.evaluate_choices(store, p["task_id"], 1, suggested=["f"])
    assert result["status"] == "pending" and result["selected"] == p["stage_references"]
    with pytest.raises(ValueError):
        business_choices.record_choice(
            store, choice(p, "subset", {"suggested": ["f"], "selected": ["missing"]})
        )
    assert read_prepared(store, p["task_id"], 1)["stage_references"] == ["f"]


def test_new_revision_invalidates_old_choice_and_preserves_history(
    tmp_path: Path, monkeypatch
):
    store, budget, refs, p = make_task(tmp_path, conflicts=("seal differs",))
    business_choices.record_choice(
        store,
        choice(
            p,
            "uncertainty",
            {"conflicts": ["seal differs"], "assumptions": {}, "mode": "preview"},
        ),
    )
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    newer = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "new prompt",
        "model",
        "endpoint",
        "group",
        task_id=p["task_id"],
        structure_conflicts=["seal differs"],
    )
    assert (
        business_choices.evaluate_choices(
            store, p["task_id"], newer["revision"], choice_ids=["choice"]
        )["status"]
        == "pending"
    )
    assert (
        business_choices.read_choice(store, "choice").execution_digest
        == p["execution_digest"]
    )
    assert read_prepared(store, p["task_id"], 1)["prompt"] == "prompt"


def test_choice_survives_process_restart(tmp_path: Path):
    store, _, _, p = make_task(tmp_path, conflicts=("seal differs",))
    business_choices.record_choice(
        store,
        choice(
            p,
            "uncertainty",
            {"conflicts": ["seal differs"], "assumptions": {}, "mode": "preview"},
        ),
    )
    code = 'from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.business_choices import read_choice; c=read_choice(Store(Path(sys.argv[1])),"choice"); print(c.revision,c.execution_digest,c.source)'
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"1 {p['execution_digest']} fixture"


def test_invalidation_survives_and_pause_does_not_create_attempt(
    tmp_path: Path, monkeypatch
):
    store, _, _, p = make_task(tmp_path, conflicts=("seal differs",))
    c = choice(
        p,
        "uncertainty",
        {"conflicts": ["seal differs"], "assumptions": {}, "mode": "preview"},
    )
    business_choices.record_choice(store, c)
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    assert (
        business_choices.evaluate_choices(
            store, p["task_id"], 1, choice_ids=["choice"]
        )["status"]
        == "preview"
    )
    business_choices.invalidate_choice(store, "choice")
    assert business_choices.read_choice(Store(store.path), "choice").invalidated
    assert (
        business_choices.evaluate_choices(
            store, p["task_id"], 1, choice_ids=["choice"]
        )["status"]
        == "pending"
    )
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 0


def test_changed_reference_metadata_invalidates_digest():
    from dataclasses import replace

    from ecommerce_product_image_creator.domain import (
        ReferenceCategory,
        digest_references,
    )
    from unit.test_reference_context import ref

    artwork = ref("a", ReferenceCategory.ARTWORK, version="v1")
    form = ref("f", ReferenceCategory.FORM, view="front")
    assert digest_references([artwork, form], "request") != digest_references(
        [replace(artwork, version="v2"), form], "request"
    )


def test_changed_input_does_not_reuse_choice(tmp_path: Path, monkeypatch):
    store, budget, refs, p = make_task(tmp_path, conflicts=("seal differs",))
    business_choices.record_choice(
        store,
        choice(
            p,
            "uncertainty",
            {"conflicts": ["seal differs"], "assumptions": {}, "mode": "preview"},
        ),
    )
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    refs[2]["view"] = "left"
    decision = confirm_fixture_revision(
        store, tmp_path, budget, p["task_id"], refs, monkeypatch
    )
    newer = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "prompt",
        "model",
        "endpoint",
        "group",
        task_id=p["task_id"],
        structure_conflicts=["seal differs"],
        _revision_decision_id=decision,
    )
    assert (
        business_choices.evaluate_choices(
            store, p["task_id"], newer["revision"], choice_ids=["choice"]
        )["status"]
        == "pending"
    )


def test_rejecting_subset_keeps_frozen_selection(tmp_path: Path, monkeypatch):
    store, _, _, prepared = make_task(tmp_path)
    business_choices.record_choice(
        store,
        choice(prepared, "subset", {"suggested": [], "selected": ["f"]}),
    )
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    result = business_choices.evaluate_choices(
        store, prepared["task_id"], 1, choice_ids=["choice"], suggested=[]
    )
    assert result["status"] == "ready"
    assert result["selected"] == ["f"]
    assert result["authorization"] == "unverified"


def test_subset_proposal_is_persisted_without_changing_snapshot(tmp_path: Path):
    store, _, _, p = make_task(tmp_path)
    proposal = business_choices.record_subset_proposal(
        store, "proposal", p["task_id"], 1, ["f"], "fixture-agent", "cost"
    )
    assert proposal["candidates"] == ["f"]
    assert proposal["selected"] == ["f"] and proposal["excluded"] == []
    assert proposal["status"] == "pending"
    assert (
        business_choices.read_subset_proposal(Store(store.path), "proposal") == proposal
    )
    assert (
        read_prepared(store, p["task_id"], 1)["execution_digest"]
        == p["execution_digest"]
    )


def test_subset_proposal_rejects_foreign_or_empty_refs(tmp_path: Path):
    store, _, _, p = make_task(tmp_path)
    for selection in ([], ["foreign"], ["a"], ["f", "f"]):
        with pytest.raises(ValueError):
            business_choices.record_subset_proposal(
                store, "bad", p["task_id"], 1, selection, "fixture", "cost"
            )


def test_subset_decision_is_unverified_and_rejection_keeps_snapshot(tmp_path: Path):
    store, _, _, p = make_task(tmp_path)
    business_choices.record_subset_proposal(
        store, "proposal", p["task_id"], 1, ["f"], "fixture", "cost"
    )
    business_choices.record_subset_decision(
        store, "proposal", "decision", "rejected", "fixture", time.time() + 120
    )
    proposal = business_choices.read_subset_proposal(store, "proposal")
    assert proposal["status"] == "rejected"
    assert proposal["trust_state"] == "unverified"
    assert read_prepared(store, p["task_id"], 1)["stage_references"] == ["f"]


def test_apply_trusted_subset_creates_new_snapshot_and_preserves_history(
    tmp_path: Path, monkeypatch
):
    store, budget, refs, initial = make_task(tmp_path)
    extra = tmp_path / "f2.png"
    Image.new("RGB", (3, 3), "green").save(extra)
    refs.append(
        {"ref_id": "f2", "category": "form", "path": str(extra), "mime": "image/png"}
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
    business_choices.record_subset_proposal(
        store,
        "subset",
        source["task_id"],
        source["revision"],
        ["f2"],
        "fixture",
        "cost",
    )
    business_choices.record_subset_decision(
        store, "subset", "decision", "accepted", "fixture", time.time() + 120
    )
    with pytest.raises(ValueError):
        business_choices.apply_subset_decision(store, "subset")
    monkeypatch.setattr(business_choices, "_subset_source_is_trusted", lambda *_: True)
    result = business_choices.apply_subset_decision(store, "subset")
    assert result["revision"] == source["revision"] + 1
    assert result["authorization"] == "unverified"
    assert read_prepared(store, source["task_id"], result["revision"])[
        "stage_references"
    ] == ["f2"]
    assert read_prepared(store, source["task_id"], source["revision"])[
        "stage_references"
    ] == ["f", "f2"]
    with pytest.raises(ValueError):
        business_choices.apply_subset_decision(store, "subset")


def test_concurrent_subset_apply_consumes_decision_once(tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    store, budget, refs, p = make_task(tmp_path)
    image = tmp_path / "f2.png"
    Image.new("RGB", (3, 3), "green").save(image)
    refs.append(
        {"ref_id": "f2", "category": "form", "path": str(image), "mime": "image/png"}
    )
    decision = confirm_fixture_revision(
        store, tmp_path, budget, p["task_id"], refs, monkeypatch
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
        task_id=p["task_id"],
        _revision_decision_id=decision,
    )
    business_choices.record_subset_proposal(
        store, "proposal", p["task_id"], source["revision"], ["f"], "fixture", "cost"
    )
    business_choices.record_subset_decision(
        store, "proposal", "decision", "accepted", "fixture", time.time() + 120
    )
    monkeypatch.setattr(business_choices, "_subset_source_is_trusted", lambda *_: True)

    def apply(_):
        try:
            return business_choices.apply_subset_decision(store, "proposal")["revision"]
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(apply, range(2)))
    assert outcomes.count(source["revision"] + 1) == 1
    assert outcomes.count(None) == 1
    assert (
        business_choices.read_subset_proposal(store, "proposal")["target_revision"]
        == source["revision"] + 1
    )


def test_proposal_and_decision_recover_in_new_python_process(tmp_path: Path):
    store, _, _, p = make_task(tmp_path)
    business_choices.record_subset_proposal(
        store, "proposal", p["task_id"], 1, ["f"], "fixture", "cost"
    )
    business_choices.record_subset_decision(
        store, "proposal", "decision", "rejected", "fixture", time.time() + 120
    )
    code = 'from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.business_choices import read_subset_proposal; p=read_subset_proposal(Store(Path(sys.argv[1])),"proposal"); print(p["status"],p["input_digest"],p["trust_state"])'
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"rejected {p['input_digest']} unverified"


def test_expired_subset_decision_cannot_create_snapshot(tmp_path: Path, monkeypatch):
    store, _, _, p = make_task(tmp_path)
    business_choices.record_subset_proposal(
        store, "proposal", p["task_id"], 1, ["f"], "fixture", "cost"
    )
    business_choices.record_subset_decision(
        store, "proposal", "decision", "accepted", "fixture", time.time() + 120
    )
    with store._connect() as db:
        db.execute(
            "UPDATE subset_decision SET expires_at=0 WHERE proposal_id='proposal'"
        )
    monkeypatch.setattr(business_choices, "_subset_source_is_trusted", lambda *_: True)
    with pytest.raises(ValueError):
        business_choices.apply_subset_decision(store, "proposal")
    assert (
        read_prepared(store, p["task_id"], 1)["execution_digest"]
        == p["execution_digest"]
    )


def test_pending_proposal_cannot_be_bypassed_by_plain_prepare(
    tmp_path: Path, monkeypatch
):
    store, budget, refs, first = make_task(tmp_path)
    extra = tmp_path / "f2.png"
    Image.new("RGB", (3, 3), "green").save(extra)
    refs.append(
        {"ref_id": "f2", "category": "form", "path": str(extra), "mime": "image/png"}
    )
    decision = confirm_fixture_revision(
        store, tmp_path, budget, first["task_id"], refs, monkeypatch
    )
    prepared = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "prompt",
        "model",
        "endpoint",
        "group",
        task_id=first["task_id"],
        _revision_decision_id=decision,
    )
    business_choices.record_subset_proposal(
        store,
        "proposal",
        prepared["task_id"],
        prepared["revision"],
        ["f"],
        "fixture",
        "cost",
    )
    with pytest.raises(ValueError, match="(?i)subset"):
        prepare_form(
            store,
            tmp_path,
            budget,
            refs[:-1],
            "request",
            "prompt",
            "model",
            "endpoint",
            "group",
            task_id=first["task_id"],
        )
    assert read_prepared(store, prepared["task_id"], prepared["revision"])[
        "stage_references"
    ] == ["f", "f2"]
    prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "changed prompt",
        "model",
        "endpoint",
        "group",
        task_id=first["task_id"],
    )
    with pytest.raises(ValueError, match="(?i)subset"):
        prepare_form(
            store,
            tmp_path,
            budget,
            refs[:-1],
            "request",
            "changed prompt",
            "model",
            "endpoint",
            "group",
            task_id=first["task_id"],
        )


def test_version_alias_clarification_keeps_original_labels(tmp_path: Path, monkeypatch):
    store, _, _, p = make_task(tmp_path, versions=("v1", "v2"))
    c = choice(p, "version", {"same_version": ["v1", "v2"]})
    business_choices.record_choice(store, c)
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    result = business_choices.evaluate_choices(
        store, p["task_id"], 1, choice_ids=["choice"]
    )
    assert result["status"] == "ready"
    assert result["same_version"] == ["v1", "v2"]
    assert [
        r["version"]
        for r in read_prepared(store, p["task_id"], 1)["references"]
        if r["category"] == "artwork"
    ] == ["v1", "v2"]


def test_expired_and_revoked_version_choices_remain_pending(
    tmp_path: Path, monkeypatch
):
    from dataclasses import replace

    store, _, _, p = make_task(tmp_path, versions=("v1", "v2"))
    original = choice(p, "version", {"same_version": ["v1", "v2"]})
    expired = replace(original, created_at=time.time() - 30, expires_at=time.time() - 1)
    business_choices.record_choice(store, expired)
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    assert (
        business_choices.evaluate_choices(
            store, p["task_id"], 1, choice_ids=["choice"]
        )["status"]
        == "pending"
    )
    business_choices.invalidate_choice(store, "choice", "withdrawn")
    assert business_choices.read_choice(Store(store.path), "choice").invalidated


def test_legacy_choice_record_survives_detail_table_upgrade(tmp_path: Path):
    import json

    store, _, _, p = make_task(tmp_path, versions=("v1", "v2"))
    with store._connect() as db:
        db.execute("""CREATE TABLE business_choice (
            choice_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
            input_digest TEXT NOT NULL, execution_digest TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
            invalidated INTEGER NOT NULL DEFAULT 0)""")
        db.execute(
            "INSERT INTO business_choice VALUES(?,?,?,?,?,?,?,?,?,0)",
            (
                "old",
                p["task_id"],
                1,
                p["input_digest"],
                p["execution_digest"],
                "version",
                json.dumps({"version": "v1"}),
                "fixture",
                time.time(),
            ),
        )
    old = business_choices.read_choice(Store(store.path), "old")
    assert old.choice_id == "old" and old.payload == {"version": "v1"}
    assert old.expires_at is None and old.evidence_reference is None


def test_plain_revision_cannot_remove_reference_without_proposal(tmp_path: Path):
    store, budget, refs, first = make_task(tmp_path)
    with pytest.raises(ValueError, match="(?i)(revision|confirm)"):
        prepare_form(
            store,
            tmp_path,
            budget,
            refs[1:],
            "request",
            "prompt",
            "model",
            "endpoint",
            "group",
            task_id=first["task_id"],
        )
    assert (
        read_prepared(store, first["task_id"], 1)["execution_digest"]
        == first["execution_digest"]
    )


def test_team_input_revision_is_bound_and_strict_denies(tmp_path: Path, monkeypatch):
    from ecommerce_product_image_creator import input_revision

    store, budget, refs, first = make_task(tmp_path)
    target = refs[1:]
    descriptors = read_prepared(store, first["task_id"], 1)["references"][1:]
    input_revision.record_input_revision(
        store,
        "team-change",
        first["task_id"],
        1,
        descriptors,
        "team requested input change",
        "team-local-input-change",
        time.time() + 120,
        "team-explicit-preparation",
    )
    monkeypatch.setenv("TEAM_MODE", "false")
    with pytest.raises(ValueError):
        prepare_form(
            store,
            tmp_path,
            budget,
            target,
            "request",
            "new prompt",
            "model",
            "endpoint",
            "group",
            task_id=first["task_id"],
            _revision_decision_id="team-change",
        )
    monkeypatch.setenv("TEAM_MODE", "true")
    second = prepare_form(
        store,
        tmp_path,
        budget,
        target,
        "request",
        "new prompt",
        "model",
        "endpoint",
        "group",
        task_id=first["task_id"],
        _revision_decision_id="team-change",
    )
    record = input_revision.read_input_revision(store, "team-change")
    assert record["target_revision"] == second["revision"]
    assert record["target_execution_digest"] == second["execution_digest"]
    assert second["authorization"] == "unverified"


def test_trusted_operator_revision_is_bound_and_frozen(tmp_path: Path, monkeypatch):
    from ecommerce_product_image_creator import input_revision

    store, budget, refs, first = make_task(tmp_path)
    target = refs[1:]
    descriptors = read_prepared(store, first["task_id"], 1)["references"][1:]
    input_revision.record_input_revision(
        store,
        "change",
        first["task_id"],
        1,
        descriptors,
        "remove duplicate",
        "fixture",
        time.time() + 120,
    )
    with pytest.raises(ValueError):
        prepare_form(
            store,
            tmp_path,
            budget,
            target,
            "request",
            "new prompt",
            "model",
            "endpoint",
            "group",
            task_id=first["task_id"],
            _revision_decision_id="change",
        )
    monkeypatch.setattr(input_revision, "_source_is_trusted", lambda *_: True)
    second = prepare_form(
        store,
        tmp_path,
        budget,
        target,
        "request",
        "new prompt",
        "model",
        "endpoint",
        "group",
        task_id=first["task_id"],
        _revision_decision_id="change",
    )
    record = input_revision.read_input_revision(Store(store.path), "change")
    assert record["target_revision"] == second["revision"]
    assert record["target_execution_digest"] == second["execution_digest"]
    assert record["trust_state"] == "unverified"
    assert second["authorization"] == "unverified"
    assert len(read_prepared(store, first["task_id"], 1)["references"]) == 3
    with pytest.raises(ValueError):
        prepare_form(
            store,
            tmp_path,
            budget,
            target,
            "request",
            "new prompt",
            "model",
            "endpoint",
            "group",
            task_id=first["task_id"],
            _revision_decision_id="change",
        )


@pytest.mark.parametrize(
    "invalid", ["unverified", "revoked", "expired", "target_mismatch"]
)
def test_input_revision_rejects_invalid_decision(
    tmp_path: Path, monkeypatch, invalid: str
):
    from ecommerce_product_image_creator import input_revision

    store, budget, refs, first = make_task(tmp_path)
    target = refs[1:]
    target_details = read_prepared(store, first["task_id"], 1)["references"][1:]
    input_revision.record_input_revision(
        store,
        "change",
        first["task_id"],
        1,
        target_details,
        "revision",
        "fixture",
        time.time() + 120,
    )
    if invalid != "unverified":
        monkeypatch.setattr(input_revision, "_source_is_trusted", lambda *_: True)
    if invalid == "revoked":
        input_revision.invalidate_input_revision(store, "change", "revoked")
    if invalid == "expired":
        with store._connect() as db:
            db.execute(
                "UPDATE input_revision_decision SET expires_at=0 WHERE decision_id='change'"
            )
    if invalid == "target_mismatch":
        target[0] = {**target[0], "region": "changed"}
    with pytest.raises(ValueError):
        prepare_form(
            store,
            tmp_path,
            budget,
            target,
            "request",
            "new prompt",
            "model",
            "endpoint",
            "group",
            task_id=first["task_id"],
            _revision_decision_id="change",
        )
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM revision WHERE task_id=?", (first["task_id"],)
        ).fetchone() == (1,)


def test_plain_revision_cannot_change_reference_metadata(tmp_path: Path):
    store, budget, refs, first = make_task(tmp_path)
    changed = [dict(r) for r in refs]
    changed[-1]["view"] = "left"
    with pytest.raises(ValueError, match="(?i)confirm"):
        prepare_form(
            store,
            tmp_path,
            budget,
            changed,
            "request",
            "prompt",
            "model",
            "endpoint",
            "group",
            task_id=first["task_id"],
        )


def test_input_revision_concurrent_apply_and_process_recovery(
    tmp_path: Path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from ecommerce_product_image_creator import input_revision

    store, budget, refs, first = make_task(tmp_path)
    target = refs[1:]
    target_details = read_prepared(store, first["task_id"], 1)["references"][1:]
    input_revision.record_input_revision(
        store,
        "change",
        first["task_id"],
        1,
        target_details,
        "revision",
        "fixture",
        time.time() + 120,
    )
    monkeypatch.setattr(input_revision, "_source_is_trusted", lambda *_: True)

    def apply(_):
        try:
            return prepare_form(
                store,
                tmp_path,
                budget,
                target,
                "request",
                "prompt",
                "model",
                "endpoint",
                "group",
                task_id=first["task_id"],
                _revision_decision_id="change",
            )["revision"]
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, range(2)))
    assert results.count(2) == 1 and results.count(None) == 1
    code = 'import sys; from pathlib import Path; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.input_revision import read_input_revision; r=read_input_revision(Store(Path(sys.argv[1])),"change"); print(r["target_revision"],r["trust_state"])'
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "2 unverified"


def test_input_revision_cannot_cross_tasks_or_versions(tmp_path: Path, monkeypatch):
    from ecommerce_product_image_creator import input_revision

    store, budget, refs, first = make_task(tmp_path)
    target = refs[1:]
    details = read_prepared(store, first["task_id"], 1)["references"][1:]
    input_revision.record_input_revision(
        store,
        "change",
        first["task_id"],
        1,
        details,
        "revision",
        "fixture",
        time.time() + 120,
    )
    monkeypatch.setattr(input_revision, "_source_is_trusted", lambda *_: True)
    other = prepare_form(
        store, tmp_path, budget, refs, "request", "prompt", "model", "endpoint", "group"
    )
    with pytest.raises(ValueError):
        prepare_form(
            store,
            tmp_path,
            budget,
            target,
            "request",
            "prompt",
            "model",
            "endpoint",
            "group",
            task_id=other["task_id"],
            _revision_decision_id="change",
        )
    prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "new prompt",
        "model",
        "endpoint",
        "group",
        task_id=first["task_id"],
    )
    with pytest.raises(ValueError):
        prepare_form(
            store,
            tmp_path,
            budget,
            target,
            "request",
            "prompt",
            "model",
            "endpoint",
            "group",
            task_id=first["task_id"],
            _revision_decision_id="change",
        )
