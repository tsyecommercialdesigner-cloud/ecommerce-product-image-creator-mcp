"""T045: capture the offline candidate on a local HTTP receiver, never a provider."""

import hashlib
import socket
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx2
import pytest

from ecommerce_product_image_creator.preparation import prepare_form, read_prepared
from ecommerce_product_image_creator.provider import (
    ENDPOINT,
    CandidateRequest,
    ProviderError,
    build_request,
    serialize_request,
)
from ecommerce_product_image_creator.storage import ResourceBudget, Store

from .test_prepare_inputs import setup_inputs


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def capture(candidate: CandidateRequest):
    request = serialize_request(candidate)
    seen = []

    def receive(incoming):
        seen.append(incoming)
        return httpx2.Response(200, json={"task_id": "fixture-id"})

    with httpx2.Client(
        transport=httpx2.MockTransport(receive), trust_env=False
    ) as client:
        response = client.request(
            request.method, request.url, headers=request.headers, content=request.body
        )
    assert response.json() == {"task_id": "fixture-id"} and len(seen) == 1
    parsed = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + seen[0].headers["content-type"].encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + seen[0].content
    )
    return seen[0], list(parsed.iter_parts())


@pytest.mark.parametrize("count", [1, 6])
def test_form_snapshot_received_in_order_with_frozen_bytes(tmp_path: Path, count: int):
    refs = setup_inputs(tmp_path, 1, count)
    store = Store(tmp_path / "state.sqlite")
    result = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "原始 Prompt F/L/R/B/T/Q",
        "gpt-image-2.5-sunburst",
        ENDPOINT,
        "reverse",
    )
    saved = read_prepared(store, result["task_id"], result["revision"])
    candidate = build_request(store, result["task_id"], result["revision"])
    frozen = [
        Path(ref["path"]).read_bytes() for ref in refs if ref["category"] == "form"
    ]
    for ref in refs:
        Path(ref["path"]).write_bytes(b"changed after prepare")
    received, parts = capture(candidate)
    assert received.method == "POST" and str(received.url) == ENDPOINT
    assert [p.get_param("name", header="content-disposition") for p in parts] == (
        ["model", "prompt"] + ["image[]"] * count
    )
    assert parts[0].get_payload(decode=True) == candidate.model.encode()
    assert parts[1].get_payload(decode=True) == "原始 Prompt F/L/R/B/T/Q".encode()
    assert candidate.group == "reverse" and candidate.stage == "form"
    assert saved["stage_references"] == [i.ref_id for i in candidate.images]
    assert [p.get_payload(decode=True) for p in parts[2:]] == frozen
    for part, image, expected in zip(parts[2:], candidate.images, frozen, strict=True):
        assert part.get_content_type() == image.mime
        assert hashlib.sha256(expected).hexdigest() == image.sha256
    assert not candidate.protocol_verified


@pytest.mark.parametrize(
    "stage,count", [("form", 0), ("form", 7), ("mock", 0), ("mock", 1), ("mock", 4)]
)
def test_out_of_range_images_never_reach_receiver(stage, count):
    from .test_provider_matrix import image_part

    candidate = CandidateRequest(
        "gpt-image-2.5-sunburst",
        ENDPOINT,
        "reverse",
        "prompt",
        tuple(image_part(i) for i in range(count)),
        stage=stage,
    )
    with pytest.raises(ProviderError):
        capture(candidate)


def test_frozen_content_tampering_and_field_substitution_rejected(tmp_path: Path):
    refs = setup_inputs(tmp_path, 1, 1)
    store = Store(tmp_path / "state.sqlite")
    result = prepare_form(
        store,
        tmp_path,
        ResourceBudget(100000, 10000, 100000),
        refs,
        "request",
        "prompt",
        "gpt-image-2.5-sunburst",
        ENDPOINT,
        "reverse",
    )
    candidate = build_request(store, result["task_id"], result["revision"])
    with pytest.raises(ProviderError):
        capture(replace(candidate, image_field="image"))
    with pytest.raises(ProviderError):
        capture(
            replace(
                candidate, images=(replace(candidate.images[0], data=b"substituted"),)
            )
        )


@pytest.mark.parametrize("count", [2, 3])
def test_mock_snapshot_receiver_uses_form_artifact_and_artwork_only(
    tmp_path, monkeypatch, count
):
    from integration.test_mock_workflow import mock_fixture
    from PIL import Image
    from unit.revision_fixtures import confirm_fixture_revision

    from ecommerce_product_image_creator.preparation import prepare_mock

    store, budget, form, mock, _, _ = mock_fixture(tmp_path, monkeypatch)
    if count == 3:
        extra = tmp_path / "art2.png"
        Image.new("RGB", (7, 4), "red").save(extra)
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
        mock = prepare_mock(
            store,
            tmp_path,
            budget,
            form["task_id"],
            "form-artifact",
            "form-review",
            artwork,
            "mock",
            "gpt-image-2.5-sunburst",
            ENDPOINT,
            "reverse",
            _revision_decision_id=decision,
        )
    saved = read_prepared(store, form["task_id"], mock["revision"])
    candidate = build_request(store, form["task_id"], mock["revision"])
    _, parts = capture(candidate)
    expected_ids = ["form-artifact", "artwork"] + (["art2"] if count == 3 else [])
    assert (
        saved["stage_references"]
        == expected_ids
        == [image.ref_id for image in candidate.images]
    )
    assert "form" not in expected_ids and candidate.stage == "mock"
    assert len(parts) == count + 2
    for part, image in zip(parts[2:], candidate.images, strict=True):
        actual = part.get_payload(decode=True)
        assert isinstance(actual, bytes)
        assert (
            actual == image.data and hashlib.sha256(actual).hexdigest() == image.sha256
        )
        assert part.get_content_type() == image.mime
