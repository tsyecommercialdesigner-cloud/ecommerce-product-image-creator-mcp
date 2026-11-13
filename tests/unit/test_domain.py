from pathlib import Path

import pytest

from ecommerce_product_image_creator.domain import (
    DeliveryStatus,
    Reference,
    ReferenceCategory,
    TaskStatus,
    UpstreamStatus,
    check_task_transition,
    check_transition,
    validate_references,
)


def reference(category: ReferenceCategory, number: int) -> Reference:
    return Reference(
        f"r{number}", category, Path(f"/test/{number}.png"), "image/png", 1, "a" * 64
    )


@pytest.mark.parametrize(
    "art,form,valid",
    [(0, 1, False), (1, 1, True), (2, 6, True), (3, 1, False), (1, 7, False)],
)
def test_reference_count_boundaries(art: int, form: int, valid: bool) -> None:
    refs = [reference(ReferenceCategory.ARTWORK, i) for i in range(art)] + [
        reference(ReferenceCategory.FORM, i + art) for i in range(form)
    ]
    if valid:
        validate_references(refs)
    else:
        with pytest.raises(ValueError):
            validate_references(refs)


def test_reference_rejects_invalid_metadata() -> None:
    with pytest.raises(ValueError):
        Reference(
            "r", ReferenceCategory.FORM, Path("/test/x"), "image/gif", 1, "a" * 64
        )
    with pytest.raises(ValueError):
        Reference("r", ReferenceCategory.FORM, Path("/test/x"), "image/png", 1, "bad")


def test_three_status_dimensions_remain_distinct() -> None:
    assert TaskStatus.ACTIVE.value == "active"
    assert UpstreamStatus.SUCCEEDED.value == "succeeded"
    assert DeliveryStatus.DOWNLOAD_FAILED.value == "download_failed"
    check_transition(UpstreamStatus.ACCEPTED, UpstreamStatus.SUCCEEDED)
    with pytest.raises(ValueError):
        check_transition(UpstreamStatus.SUCCEEDED, UpstreamStatus.FAILED)
    check_task_transition(TaskStatus.DRAFT, TaskStatus.AWAITING_CONFIRMATION)
    with pytest.raises(ValueError):
        check_task_transition(TaskStatus.DRAFT, TaskStatus.ACTIVE)
