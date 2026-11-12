import hashlib
import socket
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator.preparation import prepare_form
from ecommerce_product_image_creator.provider import (
    ProviderError,
    build_request,
    parse_query,
    parse_submission,
    transport_unknown,
)
from ecommerce_product_image_creator.storage import ResourceBudget, Store


def setup_task(tmp_path: Path):
    refs = []
    for category in ("artwork", "form"):
        path = tmp_path / f"{category}.png"
        Image.new("RGB", (3, 3)).save(path)
        refs.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(path),
                "mime": "image/png",
            }
        )
    store = Store(tmp_path / "state.sqlite")
    budget = ResourceBudget(100000, 10000, 100000)
    result = prepare_form(
        store, tmp_path, budget, refs, "request", "prompt", "model", "endpoint", "group"
    )
    return store, budget, refs, result


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def test_form_candidate_uses_frozen_bytes_not_source_paths(tmp_path: Path):
    store, budget, refs, first = setup_task(tmp_path)
    result = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "F L R B T Q",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        task_id=first["task_id"],
    )
    original = Path(refs[1]["path"]).read_bytes()
    Path(refs[1]["path"]).write_bytes(b"changed")
    request = build_request(store, result["task_id"], result["revision"])
    assert request.prompt == "F L R B T Q"
    assert request.model == "gpt-image-2.5-sunburst"
    assert request.image_field == "image[]"
    assert request.group == "reverse"
    assert request.images[0].data == original
    assert request.images[0].sha256 == hashlib.sha256(original).hexdigest()
    assert request.images[0].mime == "image/png"
    assert len(request.images) == 1


def test_submission_and_query_states():
    assert parse_submission(200, {"task_id": "abc"}).state == "accepted"
    assert parse_submission(200, {}).state == "unknown"
    assert parse_submission(500, {"error": "secret"}).state == "unknown"
    assert (
        parse_query(
            200, {"data": {"task_id": "abc", "status": "NOT_START"}}, "abc"
        ).state
        == "pending"
    )
    assert (
        parse_query(
            200, {"data": {"task_id": "abc", "status": "IN_PROGRESS"}}, "abc"
        ).state
        == "running"
    )
    assert (
        parse_query(
            200,
            {
                "data": {
                    "task_id": "abc",
                    "status": "FAILURE",
                    "fail_reason": "Bearer SECRET",
                }
            },
            "abc",
        ).state
        == "failed"
    )
    assert "SECRET" not in repr(
        parse_query(
            200,
            {
                "data": {
                    "task_id": "abc",
                    "status": "FAILURE",
                    "fail_reason": "Bearer SECRET",
                }
            },
            "abc",
        )
    )
    assert (
        parse_query(
            200,
            {
                "data": {
                    "task_id": "abc",
                    "status": "SUCCESS",
                    "data": {"data": [{"url": "https://example.invalid/a?secret=x"}]},
                }
            },
            "abc",
        ).state
        == "succeeded"
    )
    assert (
        parse_query(
            200, {"data": {"task_id": "different", "status": "SUCCESS"}}, "abc"
        ).state
        == "unknown"
    )
    assert transport_unknown().state == "unknown"


def test_reject_unsupported_combination(tmp_path: Path):
    store, _, _, result = setup_task(tmp_path)
    with pytest.raises(ProviderError):
        build_request(store, result["task_id"], 1)


@pytest.mark.parametrize(
    "model,endpoint,group",
    [
        ("unsupported", "https://ai.t8star.org/v1/images/edits?async=true", "reverse"),
        ("gpt-image-2.5-sunburst", "https://example.invalid/other", "reverse"),
        (
            "gpt-image-2.5-sunburst",
            "https://ai.t8star.org/v1/images/edits?async=true",
            "other",
        ),
    ],
)
def test_individual_unsupported_routes_rejected(
    tmp_path: Path, model: str, endpoint: str, group: str
):
    store, budget, refs, first = setup_task(tmp_path)
    result = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "prompt",
        model,
        endpoint,
        group,
        task_id=first["task_id"],
    )
    with pytest.raises(ProviderError):
        build_request(store, result["task_id"], result["revision"])


def test_multiple_form_images_keep_order_and_bytes(tmp_path: Path, monkeypatch):
    from unit.revision_fixtures import confirm_fixture_revision

    store, budget, refs, first = setup_task(tmp_path)
    original = []
    for index in range(1, 6):
        path = tmp_path / (f"extra-{index}.jpg" if index == 5 else f"extra-{index}.png")
        Image.new("RGB", (3 + index, 3), "blue").save(path)
        refs.append(
            {
                "ref_id": f"f{index}",
                "category": "form",
                "path": str(path),
                "mime": "image/jpeg" if index == 5 else "image/png",
            }
        )
    for ref in refs:
        if ref["category"] == "form":
            original.append(Path(ref["path"]).read_bytes())
    decision = confirm_fixture_revision(
        store, tmp_path, budget, first["task_id"], refs, monkeypatch
    )
    result = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "unchanged 描述",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        task_id=first["task_id"],
        _revision_decision_id=decision,
    )
    request = build_request(store, result["task_id"], result["revision"])
    assert [part.data for part in request.images] == original
    assert [part.ref_id for part in request.images] == [
        "form",
        "f1",
        "f2",
        "f3",
        "f4",
        "f5",
    ]
    assert request.images[-1].mime == "image/jpeg"
    assert request.fields == (
        ("model", "gpt-image-2.5-sunburst"),
        ("prompt", "unchanged 描述"),
    )
    assert not request.protocol_verified
    _assert_captured_images(request)


def test_mock_candidate_uses_artifact_and_artworks_only(tmp_path: Path, monkeypatch):
    import time

    from unit.revision_fixtures import confirm_fixture_revision

    from ecommerce_product_image_creator import preparation
    from ecommerce_product_image_creator.preparation import prepare_mock
    from ecommerce_product_image_creator.review import FormReview, save_review

    store, budget, refs, first = setup_task(tmp_path)
    form = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "form",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        task_id=first["task_id"],
    )
    store.claim_operation("op", form["task_id"], form["revision"], "digest", "attempt")
    artifact = tmp_path / "artifact.png"
    Image.new("RGB", (4, 4), "green").save(artifact)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    with store._connect() as db:
        db.execute(
            "UPDATE attempt SET upstream_status='succeeded',delivery_status='delivered' WHERE attempt_id='attempt'"
        )
        db.execute(
            "INSERT INTO artifact VALUES(?,?,?,?)",
            ("artifact", "attempt", str(artifact), digest),
        )
    save_review(
        store,
        FormReview(
            "review",
            form["task_id"],
            form["revision"],
            "artifact",
            digest,
            "accept",
            "test-only",
            time.time(),
            None,
        ),
    )
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: True)
    mock = prepare_mock(
        store,
        tmp_path,
        budget,
        form["task_id"],
        "artifact",
        "review",
        [refs[0]],
        "mock prompt",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
    )
    request = build_request(store, mock["task_id"], mock["revision"])
    assert [part.ref_id for part in request.images] == ["artifact", "artwork"]
    assert request.images[0].data == artifact.read_bytes()
    assert request.prompt == "mock prompt"
    _assert_captured_images(request)
    artwork2 = tmp_path / "artwork2.png"
    Image.new("RGB", (5, 4), "yellow").save(artwork2)
    second_ref = {
        "ref_id": "artwork2",
        "category": "artwork",
        "path": str(artwork2),
        "mime": "image/png",
    }
    decision = confirm_fixture_revision(
        store,
        tmp_path,
        budget,
        form["task_id"],
        [refs[0], second_ref],
        monkeypatch,
        artifact_id="artifact",
    )
    second = prepare_mock(
        store,
        tmp_path,
        budget,
        form["task_id"],
        "artifact",
        "review",
        [refs[0], second_ref],
        "mock prompt",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        _revision_decision_id=decision,
    )
    second_request = build_request(store, second["task_id"], second["revision"])
    assert [part.ref_id for part in second_request.images] == [
        "artifact",
        "artwork",
        "artwork2",
    ]
    assert second_request.images[-1].data == artwork2.read_bytes()
    _assert_captured_images(second_request)
    monkeypatch.setattr(preparation, "_review_is_trusted", lambda *_: False)
    with pytest.raises(ProviderError):
        build_request(store, mock["task_id"], mock["revision"])


def test_fake_transport_timeout_is_unknown_without_retry():
    calls = []

    def fake_send():
        calls.append(1)
        raise TimeoutError("Bearer secret")

    with pytest.raises(TimeoutError):
        fake_send()
    result = transport_unknown()
    assert calls == [1]
    assert result.state == "unknown" and "secret" not in repr(result)


def test_query_identifier_and_response_ambiguities():
    from ecommerce_product_image_creator.provider import build_query_path

    assert build_query_path("abc-1") == "/v1/images/tasks/abc-1"
    with pytest.raises(ProviderError):
        build_query_path("../secret")
    assert parse_submission(200, {"task_id": "one", "data": "two"}).state == "unknown"
    assert (
        parse_query(200, {"data": {"task_id": "abc", "status": []}}, "abc").state
        == "unknown"
    )
    result = parse_query(
        200,
        {
            "data": {
                "task_id": "abc",
                "status": "SUCCESS",
                "data": {
                    "data": [
                        {"url": "https://example.invalid/1"},
                        {"url": "https://example.invalid/2"},
                    ]
                },
            }
        },
        "abc",
    )
    assert (
        result.state == "succeeded"
        and len(result.image_urls) == 2
        and not result.result_available
    )
    assert "example.invalid" not in repr(result)


def test_tampered_frozen_bytes_fail_closed(tmp_path: Path):
    store, budget, refs, first = setup_task(tmp_path)
    result = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "prompt",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        task_id=first["task_id"],
    )
    with store._connect() as db:
        db.execute(
            "UPDATE prepared_reference SET data=? WHERE task_id=? AND revision=? AND ref_id='form'",
            (b"changed", result["task_id"], result["revision"]),
        )
    with pytest.raises(ValueError):
        build_request(store, result["task_id"], result["revision"])


def test_cost_and_error_are_conservative():
    result = parse_query(
        200,
        {
            "code": "success",
            "data": {
                "task_id": "abc",
                "status": "SUCCESS",
                "cost": 0.15,
                "data": {
                    "data": [{"url": "https://example.invalid/?credential=secret"}]
                },
            },
        },
        "abc",
    )
    assert result.cost == 0.15 and result.result_available
    assert "secret" not in repr(result)
    assert (
        parse_query(
            200,
            {"data": {"task_id": "abc", "status": "SUCCESS", "cost": 10**1000}},
            "abc",
        ).cost
        is None
    )
    assert (
        parse_query(
            200,
            {"code": "failure", "data": {"task_id": "abc", "status": "SUCCESS"}},
            "abc",
        ).state
        == "unknown"
    )


def test_serialized_form_captured_by_fake_http(tmp_path: Path):
    from email import policy
    from email.parser import BytesParser

    import httpx2

    from ecommerce_product_image_creator.provider import serialize_request

    store, budget, refs, first = setup_task(tmp_path)
    result = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "提示词 F",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        task_id=first["task_id"],
    )
    candidate = build_request(store, result["task_id"], result["revision"])
    serialized = serialize_request(candidate)
    seen = []

    def handle(request):
        seen.append(request)
        return httpx2.Response(200, json={"task_id": "fixture-id"})

    with httpx2.Client(
        transport=httpx2.MockTransport(handle), trust_env=False
    ) as client:
        response = client.request(
            serialized.method,
            serialized.url,
            headers=serialized.headers,
            content=serialized.body,
        )
    assert len(seen) == 1
    request = seen[0]
    assert (
        request.method == "POST"
        and request.url.path == "/v1/images/edits"
        and request.url.query == b"async=true"
    )
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + request.headers["content-type"].encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + request.content
    )
    parts = list(message.iter_parts())
    assert [p.get_param("name", header="content-disposition") for p in parts] == [
        "model",
        "prompt",
        "image[]",
    ]
    assert parts[1].get_payload(decode=True) == "提示词 F".encode()
    assert parts[2].get_filename() == "image-01.png"
    assert parts[2].get_content_type() == "image/png"
    assert parts[2].get_payload(decode=True) == candidate.images[0].data
    assert parse_submission(response.status_code, response.json()).state == "accepted"
    assert serialized == serialize_request(candidate)


def _assert_captured_images(candidate):
    from email import policy
    from email.parser import BytesParser

    import httpx2

    from ecommerce_product_image_creator.provider import serialize_request

    serialized = serialize_request(candidate)
    requests = []

    def capture(request):
        requests.append(request)
        return httpx2.Response(200, json={"task_id": "fixture-id"})

    with httpx2.Client(
        transport=httpx2.MockTransport(capture), trust_env=False
    ) as client:
        result = client.request(
            serialized.method,
            serialized.url,
            headers=serialized.headers,
            content=serialized.body,
        )
    assert (
        len(requests) == 1
        and parse_submission(result.status_code, result.json()).state == "accepted"
    )
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + requests[0].headers["content-type"].encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + requests[0].content
    )
    parts = list(message.iter_parts())
    assert [p.get_param("name", header="content-disposition") for p in parts] == [
        "model",
        "prompt",
    ] + ["image[]"] * len(candidate.images)
    assert parts[0].get_payload(decode=True) == candidate.model.encode("utf-8")
    assert parts[1].get_payload(decode=True) == candidate.prompt.encode("utf-8")
    for index, (part, image) in enumerate(
        zip(parts[2:], candidate.images, strict=True), 1
    ):
        assert part.get_payload(decode=True) == image.data
        assert part.get_content_type() == image.mime
        assert (
            part.get_filename()
            == f"image-{index:02}.{'png' if image.mime == 'image/png' else 'jpg'}"
        )
    assert serialized == serialize_request(candidate)


def test_serialization_rejects_tampering_and_mime_mismatch(tmp_path: Path):
    from dataclasses import replace

    from ecommerce_product_image_creator.provider import serialize_request

    store, budget, refs, first = setup_task(tmp_path)
    result = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "prompt",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        task_id=first["task_id"],
    )
    request = build_request(store, result["task_id"], result["revision"])
    image = request.images[0]
    with pytest.raises(ProviderError):
        serialize_request(replace(request, images=(replace(image, data=b"tampered"),)))
    with pytest.raises(ProviderError):
        serialize_request(replace(request, images=(replace(image, mime="image/jpeg"),)))
    with pytest.raises(ProviderError):
        serialize_request(replace(request, image_field="image"))


def test_mock_transport_timeout_never_retries_and_ignores_proxy(
    tmp_path: Path, monkeypatch
):
    import httpx2

    from ecommerce_product_image_creator.provider import serialize_request

    store, budget, refs, first = setup_task(tmp_path)
    result = prepare_form(
        store,
        tmp_path,
        budget,
        refs,
        "request",
        "secret prompt",
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
        task_id=first["task_id"],
    )
    request = serialize_request(
        build_request(store, result["task_id"], result["revision"])
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1234")
    calls = []

    def failing_transport(req):
        calls.append(req)
        raise httpx2.ReadTimeout("test secret", request=req)

    with (
        httpx2.Client(
            transport=httpx2.MockTransport(failing_transport), trust_env=False
        ) as client,
        pytest.raises(httpx2.ReadTimeout),
    ):
        client.request(
            request.method, request.url, headers=request.headers, content=request.body
        )
    assert len(calls) == 1
    unknown = transport_unknown()
    assert unknown.state == "unknown" and "secret" not in repr(unknown)
    assert "secret prompt" not in repr(request)


def test_fake_http_query_states_and_ambiguous_submission():
    import httpx2

    responses = [
        {"task_id": "one", "data": "different"},
        {"data": {"task_id": "one", "status": "IN_PROGRESS"}},
        {"data": {"task_id": "one", "status": "FAILURE", "fail_reason": "secret"}},
    ]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json=responses[len(calls) - 1])

    with httpx2.Client(
        transport=httpx2.MockTransport(handler), trust_env=False
    ) as client:
        accepted = client.post(
            "https://ai.t8star.org/v1/images/edits?async=true", content=b"fixture"
        )
        assert (
            parse_submission(accepted.status_code, accepted.json()).state == "unknown"
        )
        for expected in ("running", "failed"):
            response = client.get("https://ai.t8star.org/v1/images/tasks/one")
            assert (
                parse_query(response.status_code, response.json(), "one").state
                == expected
            )
    assert len(calls) == 3
