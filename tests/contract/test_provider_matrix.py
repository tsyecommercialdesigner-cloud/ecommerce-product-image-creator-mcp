"""T045 offline candidates; all real provider combinations remain unverified."""

import hashlib
import socket
from dataclasses import replace
from io import BytesIO

import httpx2
import pytest
from PIL import Image

from ecommerce_product_image_creator.provider import (
    ENDPOINT,
    CandidateRequest,
    ImagePart,
    ProviderError,
    build_query_path,
    parse_query,
    parse_submission,
    serialize_request,
    transport_unknown,
)

from .test_provider_multipart import capture


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def image_part(index: int) -> ImagePart:
    out = BytesIO()
    mime = "image/jpeg" if index % 2 else "image/png"
    Image.new("RGB", (index + 3, 4), (index, 20, 30)).save(
        out, format="JPEG" if index % 2 else "PNG"
    )
    data = out.getvalue()
    return ImagePart(f"reference-{index}", mime, hashlib.sha256(data).hexdigest(), data)


@pytest.mark.parametrize("model", ["gpt-image-2.5-sunburst", "gpt-image-2.5-flare"])
@pytest.mark.parametrize(
    "stage,count", [("form", 1), ("form", 6), ("mock", 2), ("mock", 3)]
)
def test_model_stage_candidate_received(model, stage, count):
    # Candidate serialization only: Mock provenance is independently checked by prepare/build tests.
    candidate = CandidateRequest(
        model,
        ENDPOINT,
        "reverse",
        "候选提示",
        tuple(image_part(i) for i in range(count)),
        stage=stage,
    )
    received, parts = capture(candidate)
    assert str(received.url) == ENDPOINT and received.method == "POST"
    assert [p.get_param("name", header="content-disposition") for p in parts] == [
        "model",
        "prompt",
    ] + ["image[]"] * count
    assert parts[0].get_payload(decode=True) == model.encode()
    assert parts[1].get_payload(decode=True) == candidate.prompt.encode()
    assert [p.get_payload(decode=True) for p in parts[2:]] == [
        i.data for i in candidate.images
    ]
    assert [p.get_content_type() for p in parts[2:]] == [
        i.mime for i in candidate.images
    ]
    assert candidate.group == "reverse" and not candidate.protocol_verified


@pytest.mark.parametrize(
    "change",
    [
        {"model": "other"},
        {"endpoint": "https://example.invalid"},
        {"group": "other"},
        {"stage": "other"},
        {"image_field": "image"},
    ],
)
def test_unsupported_candidate_is_rejected(change):
    candidate = CandidateRequest(
        "gpt-image-2.5-flare",
        ENDPOINT,
        "reverse",
        "prompt",
        (image_part(0),),
        stage="form",
    )
    with pytest.raises(ProviderError):
        serialize_request(replace(candidate, **change))


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (200, {"task_id": "up-1"}, "accepted"),
        (200, {}, "unknown"),
        (200, {"task_id": "../bad"}, "unknown"),
        (200, {"task_id": "up-1", "data": "other"}, "unknown"),
        (429, {}, "unknown"),
        (503, {}, "unknown"),
        (200, [], "unknown"),
    ],
)
def test_fake_submission_identity_and_ambiguous_errors(status, payload, expected):
    calls = []

    def receiver(request):
        calls.append(request)
        return httpx2.Response(status, json=payload)

    with httpx2.Client(
        transport=httpx2.MockTransport(receiver), trust_env=False
    ) as client:
        response = client.post(ENDPOINT, content=b"fixture")
    result = parse_submission(response.status_code, response.json())
    assert result.state == expected and len(calls) == 1
    assert result.task_id == ("up-1" if expected == "accepted" else None)


@pytest.mark.parametrize(
    "status,remote_id,expected",
    [
        ("NOT_START", "up-1", "pending"),
        ("IN_PROGRESS", "up-1", "running"),
        ("SUCCESS", "up-1", "succeeded"),
        ("FAILURE", "up-1", "failed"),
        ("UNKNOWN", "up-1", "unknown"),
        ("SUCCESS", "other", "unknown"),
        ("SUCCESS", None, "unknown"),
    ],
)
def test_fake_query_bound_to_submission_identity(status, remote_id, expected):
    calls = []

    def receiver(request):
        calls.append(request)
        return httpx2.Response(
            200,
            json={
                "data": {
                    "task_id": remote_id,
                    "status": status,
                    "data": {"data": [{"url": "https://example.invalid/a"}]},
                }
            },
        )

    accepted = parse_submission(200, {"task_id": "up-1"})
    assert accepted.task_id == "up-1"
    with httpx2.Client(
        transport=httpx2.MockTransport(receiver), trust_env=False
    ) as client:
        response = client.get(
            "https://ai.t8star.org" + build_query_path(accepted.task_id)
        )
    result = parse_query(response.status_code, response.json(), accepted.task_id)
    assert result.state == expected and len(calls) == 1
    assert result.task_id == ("up-1" if expected != "unknown" else None)
    # A parsed success is not an Artifact registration or delivery.
    assert not hasattr(result, "delivery_status")


@pytest.mark.parametrize("error", [httpx2.ReadTimeout, httpx2.ConnectError])
def test_transport_interruption_never_retries(error):
    calls = []

    def receiver(request):
        calls.append(request)
        raise error("isolated failure", request=request)

    with (
        httpx2.Client(
            transport=httpx2.MockTransport(receiver), trust_env=False
        ) as client,
        pytest.raises(error),
    ):
        client.post(ENDPOINT, content=b"fixture")
    assert len(calls) == 1 and transport_unknown().state == "unknown"
