"""US2 reference semantics; metadata is supplied, not model-verified vision."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ecommerce_product_image_creator.domain import (
    Reference,
    ReferenceCategory,
    digest_references,
)


def ref(identifier: str, category: ReferenceCategory, **metadata: Any) -> Reference:
    return Reference(
        identifier,
        category,
        Path("fixture.png"),
        "image/png",
        12,
        "a" * 64,
        task_id="task",
        **metadata,
    )


def test_categories_and_optional_views():
    artwork = ref("a", ReferenceCategory.ARTWORK, region="front", version="v1")
    form = ref("f", ReferenceCategory.FORM)
    assert artwork.category.value == "artwork" and form.category.value == "form"
    assert artwork.region == "front" and artwork.version == "v1" and form.view is None


def test_regions_do_not_imply_different_versions():
    front = ref("a", ReferenceCategory.ARTWORK, region="front", version="v1")
    rear = ref("b", ReferenceCategory.ARTWORK, region="rear", version="v1")
    assert front.region != rear.region and front.version == rear.version
    assert front.ref_id != rear.ref_id


def test_distinct_versions_and_reference_identity_are_retained():
    one = ref("a", ReferenceCategory.ARTWORK, region="front", version="v1")
    two = ref("b", ReferenceCategory.ARTWORK, region="front", version="v2")
    assert [(r.ref_id, r.region, r.version) for r in (one, two)] == [
        ("a", "front", "v1"),
        ("b", "front", "v2"),
    ]


def test_complementary_views_purpose_and_importance_preserved():
    front = ref(
        "f", ReferenceCategory.FORM, view="front", purpose="shape", importance="high"
    )
    side = ref(
        "s",
        ReferenceCategory.FORM,
        view="side",
        purpose="thickness",
        importance="medium",
    )
    assert (front.ref_id, front.view, front.purpose, front.importance) == (
        "f",
        "front",
        "shape",
        "high",
    )
    assert (side.ref_id, side.view, side.purpose, side.importance) == (
        "s",
        "side",
        "thickness",
        "medium",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("view", "rear"),
        ("region", "back"),
        ("version", "v2"),
        ("purpose", "detail"),
        ("importance", "low"),
        ("sha256", "b" * 64),
    ],
)
def test_changed_reference_metadata_changes_task_digest(field: str, value: str):
    art = ref("a", ReferenceCategory.ARTWORK, region="front", version="v1")
    form = ref("f", ReferenceCategory.FORM, view="front")
    changed = replace(form, **{field: value})
    assert digest_references([art, form], "request") != digest_references(
        [art, changed], "request"
    )


def test_unobserved_structure_has_explicit_unknown_provenance():
    from ecommerce_product_image_creator.domain import Observation, ObservationStatus

    unknown = Observation("bottom", ObservationStatus.UNKNOWN)
    form = replace(
        ref("f", ReferenceCategory.FORM, view="front"), observations=(unknown,)
    )
    assert form.observations[0].status is ObservationStatus.UNKNOWN
    assert form.observations[0].source_ref_id is None
    assert form.observations[0].description is None


def test_observations_keep_source_and_never_promote_claims():
    from ecommerce_product_image_creator.domain import (
        Observation,
        ObservationSource,
        ObservationStatus,
    )

    manual = Observation(
        "shape",
        ObservationStatus.DESCRIBED,
        ObservationSource.OPERATOR,
        "f",
        "bag shape",
    )
    guess = Observation(
        "bottom", ObservationStatus.ASSUMED, ObservationSource.AGENT, "f", "flat base"
    )
    form = replace(ref("f", ReferenceCategory.FORM), observations=(manual, guess))
    assert form.observations == (manual, guess)
    assert all(item.status != ObservationStatus.VERIFIED for item in form.observations)
    with pytest.raises(ValueError):
        Observation(
            "bottom",
            ObservationStatus.VERIFIED,
            ObservationSource.AGENT,
            "f",
            "flat base",
        )
    with pytest.raises(ValueError):
        Observation(
            "bottom",
            ObservationStatus.VERIFIED,
            ObservationSource.INDEPENDENT,
            "f",
            "flat base",
        )


def test_cross_reference_observation_rejected():
    from ecommerce_product_image_creator.domain import (
        Observation,
        ObservationSource,
        ObservationStatus,
    )

    claim = Observation(
        "shape",
        ObservationStatus.DESCRIBED,
        ObservationSource.OPERATOR,
        "different",
        "shape",
    )
    with pytest.raises(ValueError):
        replace(ref("f", ReferenceCategory.FORM), observations=(claim,))


def test_observation_change_binds_existing_v2_snapshot():
    from ecommerce_product_image_creator.domain import (
        ExecutionSnapshot,
        Observation,
        ObservationSource,
        ObservationStatus,
        PromptRevision,
        Revision,
    )

    art = ref("a", ReferenceCategory.ARTWORK)
    form = ref("f", ReferenceCategory.FORM)
    claim = Observation(
        "bottom", ObservationStatus.ASSUMED, ObservationSource.AGENT, "f", "flat"
    )
    changed = replace(form, observations=(claim,))
    old = digest_references([art, form], "request")
    new = digest_references([art, changed], "request")
    assert old != new
    prompt = PromptRevision.from_text("task", 1, 1, "form", "text")
    one = ExecutionSnapshot.create(
        Revision("task", 1, old),
        prompt,
        "form",
        "model",
        "endpoint",
        "group",
        references=(("f", form.sha256),),
        operation_scope="scope",
    )
    two = ExecutionSnapshot.create(
        Revision("task", 1, new),
        prompt,
        "form",
        "model",
        "endpoint",
        "group",
        references=(("f", form.sha256),),
        operation_scope="scope",
    )
    assert (
        one.digest_version == two.digest_version == 2
        and one.execution_digest != two.execution_digest
    )


def test_unpersisted_observation_payload_is_not_silently_accepted(tmp_path):
    from PIL import Image

    from ecommerce_product_image_creator.preparation import (
        PreparationError,
        prepare_form,
    )
    from ecommerce_product_image_creator.storage import ResourceBudget, Store

    art = tmp_path / "art.png"
    form = tmp_path / "form.png"
    Image.new("RGB", (3, 3)).save(art)
    Image.new("RGB", (3, 3)).save(form)
    refs = [
        {"ref_id": "art", "category": "artwork", "path": str(art), "mime": "image/png"},
        {
            "ref_id": "form",
            "category": "form",
            "path": str(form),
            "mime": "image/png",
            "observations": "operator claims unknown bottom",
        },
    ]
    with pytest.raises(PreparationError):
        prepare_form(
            Store(tmp_path / "state.sqlite"),
            tmp_path,
            ResourceBudget(10000, 1000, 10000),
            refs,
            "request",
            "prompt",
            "model",
            "endpoint",
            "group",
        )


def test_legacy_reference_digest_is_unchanged_without_observations():
    import hashlib
    import json

    art = ref("a", ReferenceCategory.ARTWORK, region="front", version="v1")
    form = ref("f", ReferenceCategory.FORM, view="front")
    old = [
        (
            r.task_id,
            r.ref_id,
            r.category.value,
            r.mime,
            r.byte_count,
            r.sha256,
            r.view,
            r.region,
            r.version,
            r.importance,
        )
        for r in (art, form)
    ]
    raw = json.dumps(
        ("request", old), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert digest_references([art, form], "request") == hashlib.sha256(raw).hexdigest()


def test_unknown_and_assumption_cannot_claim_provenance():
    from ecommerce_product_image_creator.domain import (
        Observation,
        ObservationSource,
        ObservationStatus,
    )

    with pytest.raises(ValueError):
        Observation(
            "bottom", ObservationStatus.UNKNOWN, ObservationSource.OPERATOR, "f", "flat"
        )
    with pytest.raises(ValueError):
        Observation(
            "bottom", ObservationStatus.ASSUMED, ObservationSource.AGENT, "f", None
        )


def test_observation_persistence_and_snapshot_invalidation(tmp_path, monkeypatch):
    from PIL import Image

    from ecommerce_product_image_creator.preparation import prepare_form, read_prepared
    from ecommerce_product_image_creator.storage import ResourceBudget, Store
    from ecommerce_product_image_creator.tasks import LocalEligibility
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
            "observations": [
                {"subject": "bottom", "status": "unknown"},
                {
                    "subject": "shape",
                    "status": "described",
                    "source": "operator",
                    "source_ref_id": "form",
                    "description": "visible bag",
                },
            ],
        },
    ]
    store = Store(tmp_path / "state.sqlite")
    budget = ResourceBudget(10000, 1000, 10000)
    first = prepare_form(
        store, tmp_path, budget, refs, "request", "prompt", "model", "endpoint", "group"
    )
    LocalEligibility(store, tmp_path, budget)
    with store._connect() as db:
        db.execute(
            "INSERT INTO local_eligibility(operation_id,confirmation_id,task_id,revision,input_digest,prompt_revision,prompt_digest,execution_digest,scope_digest,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "old-op",
                "old-confirm",
                first["task_id"],
                1,
                first["input_digest"],
                1,
                first["prompt_digest"],
                first["execution_digest"],
                "scope",
                9999999999,
            ),
        )
    saved = read_prepared(Store(store.path), first["task_id"], 1)
    assert saved["references"][1]["observations"][0] == {
        "subject": "bottom",
        "status": "unknown",
        "source": None,
        "source_ref_id": None,
        "description": None,
    }
    assert saved["references"][1]["observations"][1]["source_ref_id"] == "form"
    refs[1]["observations"][1]["description"] = "changed bag"
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
        "model",
        "endpoint",
        "group",
        task_id=first["task_id"],
        _revision_decision_id=decision,
    )
    assert first["execution_digest"] != second["execution_digest"]
    with store._connect() as db:
        assert db.execute(
            "SELECT invalidated FROM local_eligibility WHERE operation_id='old-op'"
        ).fetchone() == (1,)
    assert (
        read_prepared(store, first["task_id"], 1)["references"][1]["observations"][1][
            "description"
        ]
        == "visible bag"
    )
    with store._connect() as db:
        db.execute(
            "UPDATE prepared_input_v2 SET context=replace(context,'changed bag','tampered') WHERE task_id=? AND revision=?",
            (first["task_id"], second["revision"]),
        )
    with pytest.raises(ValueError):
        read_prepared(store, first["task_id"], second["revision"])
