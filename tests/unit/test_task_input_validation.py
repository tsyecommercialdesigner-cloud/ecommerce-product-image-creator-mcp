from pathlib import Path

import pytest

from ecommerce_product_image_creator.domain import (
    InputValidationError,
    Reference,
    ReferenceCategory,
    validate_task_input,
)


def reference(
    category: ReferenceCategory, number: int, mime: str = "image/png"
) -> Reference:
    return Reference(
        f"item-{number}", category, Path(f"/input/{number}"), mime, 1, "a" * 64
    )


@pytest.mark.parametrize("art,form", [(1, 1), (2, 6)])
def test_accepted_reference_counts_and_optional_view(art: int, form: int) -> None:
    refs = [reference(ReferenceCategory.ARTWORK, i) for i in range(art)] + [
        reference(ReferenceCategory.FORM, i + art, "image/jpeg") for i in range(form)
    ]
    assert validate_task_input(refs, "自然语言要求") is None
    assert all(ref.view is None for ref in refs)


@pytest.mark.parametrize(
    "art,form,category",
    [(0, 1, "artwork"), (3, 1, "artwork"), (1, 0, "form"), (1, 7, "form")],
)
def test_count_error_identifies_category(art: int, form: int, category: str) -> None:
    refs = [reference(ReferenceCategory.ARTWORK, i) for i in range(art)] + [
        reference(ReferenceCategory.FORM, i + art) for i in range(form)
    ]
    with pytest.raises(InputValidationError) as caught:
        validate_task_input(refs, "request")
    assert caught.value.category == category


def test_empty_instruction_rejected() -> None:
    refs = [
        reference(ReferenceCategory.ARTWORK, 1),
        reference(ReferenceCategory.FORM, 2),
    ]
    with pytest.raises(InputValidationError) as caught:
        validate_task_input(refs, "  ")
    assert caught.value.field == "instructions"


@pytest.mark.parametrize(
    "category,mime", [(None, "image/png"), (ReferenceCategory.FORM, "image/gif")]
)
def test_invalid_reference_indicates_affected_image(
    category: object, mime: str
) -> None:
    with pytest.raises(InputValidationError) as caught:
        Reference("image-1", category, Path("/input/1"), mime, 1, "a" * 64)  # type: ignore[arg-type]
    assert caught.value.ref_id == "image-1"
