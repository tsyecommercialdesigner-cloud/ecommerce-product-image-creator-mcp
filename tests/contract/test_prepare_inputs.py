import hashlib
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator.preparation import (
    PreparationError,
    prepare_form,
    read_prepared,
)
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.tasks import LocalEligibility


def setup_inputs(tmp_path: Path, art: int = 1, form: int = 1):
    entries = []
    for category, count in [("artwork", art), ("form", form)]:
        for n in range(count):
            path = tmp_path / f"{category}{n}.png"
            Image.new("RGB", (4, 4), "red").save(path)
            entries.append(
                {
                    "ref_id": f"{category}{n}",
                    "category": category,
                    "path": str(path),
                    "mime": "image/png",
                }
            )
    return entries


def prepare(tmp_path: Path, refs: list[dict[str, str]], **kwargs: str):
    return prepare_form(
        Store(tmp_path / "state.sqlite"),
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        kwargs.get("instructions", "request"),
        kwargs.get("prompt", "F L R B T Q"),
        "model",
        "endpoint",
        "group",
    )


@pytest.mark.parametrize("art,form", [(1, 1), (1, 6), (2, 1), (2, 6)])
def test_form_prepare_freezes_and_recovers(tmp_path: Path, art: int, form: int):
    refs = setup_inputs(tmp_path, art, form)
    before = Path(refs[-1]["path"]).read_bytes()
    result = prepare(tmp_path, refs)
    assert result["stage"] == "form"
    assert result["authorization"] == "unverified"
    assert result["cost"] == "unknown" and result["duration"] == "unknown"
    assert len(result["stage_references"]) == form
    assert result["stage_references"] == [
        entry["ref_id"] for entry in refs if entry["category"] == "form"
    ]
    Path(refs[-1]["path"]).write_bytes(b"changed")
    restored = read_prepared(Store(tmp_path / "state.sqlite"), result["task_id"], 1)
    assert restored["reference_bytes"][-1] == before
    assert hashlib.sha256(before).hexdigest() == restored["reference_digests"][-1]


@pytest.mark.parametrize(
    "art,form,category",
    [(0, 1, "artwork"), (3, 1, "artwork"), (1, 0, "form"), (1, 7, "form")],
)
def test_count_errors_are_localized(tmp_path: Path, art: int, form: int, category: str):
    with pytest.raises(PreparationError, match=category):
        prepare(tmp_path, setup_inputs(tmp_path, art, form))


def test_rejects_missing_category_and_invalid_images(tmp_path: Path):
    refs = setup_inputs(tmp_path)
    refs[0].pop("category")
    with pytest.raises(PreparationError):
        prepare(tmp_path, refs)
    refs = setup_inputs(tmp_path)
    refs[0]["mime"] = "image/gif"
    with pytest.raises(PreparationError):
        prepare(tmp_path, refs)
    refs = setup_inputs(tmp_path)
    with pytest.raises(PreparationError):
        prepare(tmp_path, refs, instructions=" ")


def test_tampered_snapshot_rejected(tmp_path: Path):
    refs = setup_inputs(tmp_path)
    result = prepare(tmp_path, refs)
    store = Store(tmp_path / "state.sqlite")
    with store._connect() as db:
        db.execute(
            "UPDATE prepared_reference SET data=? WHERE task_id=? AND position=0",
            (b"tampered", result["task_id"]),
        )
    with pytest.raises(PreparationError):
        read_prepared(store, result["task_id"], 1)


def test_new_prompt_revision_preserves_old_snapshot(tmp_path: Path):
    refs = setup_inputs(tmp_path)
    first = prepare(tmp_path, refs)
    store = Store(tmp_path / "state.sqlite")
    second = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "changed prompt",
        "model",
        "endpoint",
        "group",
        task_id=first["task_id"],
    )
    assert second["revision"] == 2
    assert second["execution_digest"] != first["execution_digest"]
    assert read_prepared(store, first["task_id"], 1)["prompt"] == "F L R B T Q"
    assert read_prepared(store, first["task_id"], 2)["prompt"] == "changed prompt"


def test_tampered_execution_metadata_rejected(tmp_path: Path):
    result = prepare(tmp_path, setup_inputs(tmp_path))
    store = Store(tmp_path / "state.sqlite")
    with store._connect() as db:
        db.execute(
            "UPDATE prepared_execution SET model='other' WHERE task_id=?",
            (result["task_id"],),
        )
    with pytest.raises(PreparationError):
        read_prepared(store, result["task_id"], 1)


def test_valid_jpeg_and_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    refs = setup_inputs(tmp_path)
    jpeg = tmp_path / "form.jpg"
    Image.new("RGB", (4, 4), "blue").save(jpeg, format="JPEG")
    refs[1]["path"] = str(jpeg)
    refs[1]["mime"] = "image/jpeg"

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert prepare(tmp_path, refs)["stage"] == "form"


def test_new_revision_invalidates_existing_local_eligibility(tmp_path: Path):
    refs = setup_inputs(tmp_path)
    first = prepare(tmp_path, refs)
    store = Store(tmp_path / "state.sqlite")
    LocalEligibility(store, tmp_path, ResourceBudget(100000, 10000, 100000))
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
    prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "new prompt",
        "model",
        "endpoint",
        "group",
        task_id=first["task_id"],
    )
    with store._connect() as db:
        assert db.execute(
            "SELECT invalidated FROM local_eligibility WHERE operation_id='old-op'"
        ).fetchone() == (1,)


def test_snapshot_recovered_from_new_process(tmp_path: Path):
    result = prepare(tmp_path, setup_inputs(tmp_path))
    code = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.preparation import read_prepared; print(read_prepared(Store(Path(sys.argv[1])),sys.argv[2],1)['prompt_digest'])"
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "state.sqlite"), result["task_id"]],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == result["prompt_digest"]


def test_reference_metadata_and_instructions_are_recoverable(tmp_path: Path):
    refs = setup_inputs(tmp_path)
    refs[1]["view"] = "front"
    result = prepare(tmp_path, refs)
    recovered = read_prepared(Store(tmp_path / "state.sqlite"), result["task_id"], 1)
    assert recovered["instructions"] == "request"
    assert recovered["references"][1]["view"] == "front"


def test_purpose_metadata_is_bound_and_recovered(tmp_path: Path, monkeypatch):
    from unit.revision_fixtures import confirm_fixture_revision

    refs = setup_inputs(tmp_path)
    refs[1]["purpose"] = "side detail"
    result = prepare(tmp_path, refs)
    saved = read_prepared(Store(tmp_path / "state.sqlite"), result["task_id"], 1)
    assert saved["references"][1]["purpose"] == "side detail"
    refs[1]["purpose"] = "front detail"
    decision = confirm_fixture_revision(
        Store(tmp_path / "state.sqlite"),
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        result["task_id"],
        refs,
        monkeypatch,
    )
    second = prepare_form(
        Store(tmp_path / "state.sqlite"),
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "F L R B T Q",
        "model",
        "endpoint",
        "group",
        task_id=result["task_id"],
        _revision_decision_id=decision,
    )
    assert second["input_digest"] != result["input_digest"]


def test_form_response_has_persisted_classification_and_unverified_conflicts(
    tmp_path: Path,
):
    from ecommerce_product_image_creator.preparation import read_prepare_response

    refs = setup_inputs(tmp_path, 2, 1)
    refs[0]["version"] = "design-A"
    store = Store(tmp_path / "state.sqlite")
    result = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "prompt",
        "model",
        "endpoint",
        "group",
        structure_conflicts=["seam differs"],
    )
    assert result["references_summary"]["registered"]["artwork"] == {
        "count": 2,
        "ids": ["artwork0", "artwork1"],
    }
    assert result["references_summary"]["selected"]["form"]["ids"] == ["form0"]
    assert result["references_summary"]["not_selected"]["artwork"]["ids"] == [
        "artwork0",
        "artwork1",
    ]
    assert result["packaging_versions"]["references"][0] == {
        "ref_id": "artwork0",
        "version": "design-A",
    }
    assert result["packaging_versions"]["confirmation"] == "unverified"
    assert result["structure_conflicts"] == {
        "status": "pending_clarification",
        "reported": ["seam differs"],
        "confirmation": "unverified",
    }
    assert (
        read_prepare_response(
            Store(tmp_path / "state.sqlite"), result["task_id"], result["revision"]
        )
        == result
    )


def test_missing_conflict_information_is_unknown(tmp_path: Path):
    result = prepare(tmp_path, setup_inputs(tmp_path))
    assert result["structure_conflicts"]["status"] == "unknown"
    assert result["structure_conflicts"]["confirmation"] == "unverified"
    assert result["packaging_versions"]["references"][0]["version"] is None


def test_distinct_declared_packaging_versions_require_confirmation(tmp_path: Path):
    refs = setup_inputs(tmp_path, 2, 1)
    refs[0]["version"] = "v1"
    refs[1]["version"] = "v2"
    result = prepare(tmp_path, refs)
    assert result["packaging_versions"]["status"] == "multiple_declared_versions"
    assert result["packaging_versions"]["confirmation"] == "unverified"
    assert "packaging_version_confirmation" in result["pending"]


def test_form_revision_does_not_silently_clear_known_conflicts(tmp_path: Path):
    refs = setup_inputs(tmp_path)
    store = Store(tmp_path / "state.sqlite")
    budget = ResourceBudget(100000, 10000, 100000)
    first = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "prompt",
        "model",
        "endpoint",
        "group",
        structure_conflicts=["seal differs"],
    )
    second = prepare_form(
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
        structure_conflicts=[],
    )
    assert second["structure_conflicts"]["reported"] == ["seal differs"]
    assert second["structure_conflicts"]["status"] == "pending_clarification"


def test_old_database_without_conflicts_table_is_readable(tmp_path: Path):
    from ecommerce_product_image_creator.preparation import read_prepare_response

    refs = setup_inputs(tmp_path)
    first = prepare(tmp_path, refs)
    store = Store(tmp_path / "state.sqlite")
    with store._connect() as db:
        db.execute("DROP TABLE prepared_conflicts")
    assert (
        read_prepare_response(store, first["task_id"], 1)["structure_conflicts"][
            "status"
        ]
        == "unknown"
    )
    second = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "new",
        "model",
        "endpoint",
        "group",
        task_id=first["task_id"],
    )
    assert second["revision"] == 2
    assert read_prepared(store, first["task_id"], 1)["prompt"] == "F L R B T Q"


def test_response_recovered_from_new_process(tmp_path: Path):
    import json

    refs = setup_inputs(tmp_path)
    store = Store(tmp_path / "state.sqlite")
    result = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "prompt",
        "model",
        "endpoint",
        "group",
        structure_conflicts=["seal differs"],
    )
    code = "import json,sys; from pathlib import Path; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.preparation import read_prepare_response; print(json.dumps(read_prepare_response(Store(Path(sys.argv[1])),sys.argv[2],1)))"
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "state.sqlite"), result["task_id"]],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(child.stdout) == result
