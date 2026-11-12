import hashlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator.preparation import prepare_form
from ecommerce_product_image_creator.review import (
    FormReview,
    ReviewError,
    get_review,
    invalidate_review,
    save_review,
)
from ecommerce_product_image_creator.storage import ResourceBudget, Store


def review_fixture(tmp_path: Path):
    refs = []
    for category in ("artwork", "form"):
        image = tmp_path / f"{category}.png"
        Image.new("RGB", (3, 3)).save(image)
        refs.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(image),
                "mime": "image/png",
            }
        )
    store = Store(tmp_path / "db.sqlite")
    result = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 1000, 100000),
        refs,
        "request",
        "form",
        "model",
        "endpoint",
        "group",
    )
    store.claim_operation("op", result["task_id"], 1, "d", "attempt")
    artifact = tmp_path / "artifact.png"
    Image.new("RGB", (4, 4)).save(artifact)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded',delivery_status='delivered' WHERE attempt_id='attempt'"
        )
        db.execute(
            "INSERT INTO artifact VALUES(?,?,?,?)",
            ("artifact", "attempt", str(artifact), digest),
        )
    review = FormReview(
        "review",
        result["task_id"],
        1,
        "artifact",
        digest,
        "accept",
        "host-unknown",
        time.time(),
        None,
    )
    return store, review


def test_review_record_is_durable_and_unverified(tmp_path: Path):
    store, review = review_fixture(tmp_path)
    save_review(store, review)
    recovered = get_review(Store(store.path), review.review_id)
    assert recovered == review
    assert recovered.trust_state == "unverified"
    with pytest.raises(ReviewError):
        save_review(store, replace(review, decision="reject"))
    invalidate_review(store, review.review_id, "superseded")
    assert get_review(Store(store.path), review.review_id).invalidated is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "other"),
        ("form_revision", 2),
        ("artifact_id", "other"),
        ("artifact_digest", "a" * 64),
    ],
)
def test_review_rejects_wrong_associations(tmp_path: Path, field: str, value: object):
    store, review = review_fixture(tmp_path)
    with pytest.raises(ReviewError):
        save_review(store, replace(review, **{field: value}))


def test_concurrent_duplicate_review_records_have_one_winner(tmp_path: Path):
    store, review = review_fixture(tmp_path)

    def write(_: int) -> bool:
        try:
            save_review(Store(store.path), review)
            return True
        except ReviewError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(write, range(8)))
    assert outcomes.count(True) == 1
    assert get_review(Store(store.path), review.review_id) == review


def test_review_recovered_in_new_process(tmp_path: Path):
    store, review = review_fixture(tmp_path)
    save_review(store, review)
    script = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; from ecommerce_product_image_creator.review import get_review; r=get_review(Store(Path(sys.argv[1])),sys.argv[2]); print(r.decision, r.trust_state)"
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.path), review.review_id],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "accept unverified"


def test_legacy_invalidation_marker_is_not_reported_as_active(tmp_path: Path):
    store, review = review_fixture(tmp_path)
    save_review(store, review)
    with store._connect() as db:
        db.execute(
            "INSERT INTO form_review_invalidation VALUES(?)", (review.review_id,)
        )
    assert get_review(Store(store.path), review.review_id).invalidated is True
