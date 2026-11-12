from dataclasses import replace
from pathlib import Path

import pytest

from ecommerce_product_image_creator.domain import (
    Artifact,
    Attempt,
    FormRevision,
    MockRevision,
    ViewRevision,
    validate_mock_revision,
)


def test_combined_mock_revision_keeps_source_and_legacy_view():
    form_attempt = Attempt("form-attempt", "task", 1)
    form_artifact = Artifact(
        "form-image",
        "form-attempt",
        Path("/form.png"),
        "a" * 64,
        task_id="task",
        revision=1,
    )
    form = FormRevision("task", 1, "form-attempt", "a" * 64)
    mock_attempt = Attempt("mock-attempt", "task", 2)
    mock = MockRevision("task", 2, "mock-attempt", 1, "form-image", "a" * 64)
    validate_mock_revision(mock, form, form_attempt, form_artifact, mock_attempt)
    assert ViewRevision("task", 1, "F", 2, "legacy").view == "F"
    for bad in (
        replace(mock, task_id="other"),
        replace(mock, form_revision=3),
        replace(mock, form_artifact_id="other"),
        replace(mock, form_artifact_digest="b" * 64),
        replace(mock, attempt_id="other"),
    ):
        with pytest.raises(ValueError):
            validate_mock_revision(bad, form, form_attempt, form_artifact, mock_attempt)
