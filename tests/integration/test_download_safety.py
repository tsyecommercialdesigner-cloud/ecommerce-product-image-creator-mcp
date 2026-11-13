"""Isolated URL, DNS and streaming contract; the real transport stays disabled."""

import hashlib
import socket
from contextlib import contextmanager

import httpx2
import pytest

from ecommerce_product_image_creator import download_safety, provider
from ecommerce_product_image_creator.storage import ResourceBudget

from .test_artifact_reconciliation import first_context  # noqa: F401


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def download_case(first_context, monkeypatch):  # noqa: F811
    service, _, attempt, sends, locator, content, digest, _ = first_context
    monkeypatch.setattr(
        download_safety,
        "_verify_download_scope",
        lambda e, url, sha, mime: (
            e.locator_id == locator.locator_id
            and url == "https://artifact.example.org/result"
            and sha == digest
            and mime == "image/png"
        ),
    )
    monkeypatch.setattr(download_safety, "_resolve", lambda host, port: ("1.1.1.1",))
    calls = []
    peer = {"value": "1.1.1.1", "tls": True}
    response = {
        "status": 200,
        "body": content,
        "headers": {"content-type": "image/png"},
    }

    @contextmanager
    def open_pinned(url, hostname, addresses, limits):
        class FixtureStream(httpx2.SyncByteStream):
            def __iter__(self):
                if response.get("fault") == "connect":
                    raise httpx2.ConnectTimeout("fixture")
                if response.get("fault") == "read":
                    yield b"partial"
                    raise httpx2.ReadTimeout("fixture")
                if response.get("on_read"):
                    response["on_read"]()
                yield response["body"]

        def handle(request):
            calls.append((str(request.url), hostname, addresses))
            return httpx2.Response(
                response["status"],
                headers=response["headers"],
                stream=FixtureStream(),
            )

        with (
            httpx2.Client(
                transport=httpx2.MockTransport(handle),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx2.Timeout(
                    connect=limits.connect_seconds,
                    read=limits.read_seconds,
                    write=limits.read_seconds,
                    pool=limits.connect_seconds,
                ),
            ) as client,
            client.stream("GET", url) as result,
        ):
            yield result

    monkeypatch.setattr(download_safety, "_open_pinned", open_pinned)
    monkeypatch.setattr(
        download_safety,
        "_verify_peer",
        lambda _r, _h, ips: peer["tls"] and peer["value"] in ips,
    )
    limits = download_safety.DownloadLimits(100000, 8, 1, 1, 5)
    budget = ResourceBudget(100000, 10000, 100000)

    def fetch(
        url="https://artifact.example.org/result",
        sha=digest,
        mime="image/png",
        limit=limits,
    ):
        return provider.download_artifact_candidate(
            service.store, locator.locator_id, url, sha, mime, limit, budget
        )

    return (
        fetch,
        service,
        attempt,
        sends,
        locator,
        content,
        digest,
        calls,
        peer,
        response,
    )


def test_fake_https_stream_and_source_are_bound(download_case, monkeypatch):
    fetch, service, attempt, sends, locator, content, digest, calls, _, _ = (
        download_case
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    result = fetch()
    assert (
        result.data == content
        and result.sha256 == digest
        and result.locator_id == locator.locator_id
    )
    assert calls == [
        ("https://artifact.example.org/result", "artifact.example.org", ("1.1.1.1",))
    ]
    assert sends == [1]
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://artifact.example.org/result",
        "https://user@artifact.example.org/result",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://artifact.example.org:444/",
        "https://artifact.example.org/#frag",
        "https://artifact.example.org\\@evil.test/",
    ],
)
def test_unsafe_url_rejected_before_transport(url, download_case, monkeypatch):
    fetch, _, _, _, _, _, _, calls, _, _ = download_case
    monkeypatch.setattr(download_safety, "_verify_download_scope", lambda *_: True)
    with pytest.raises(ValueError):
        fetch(url)
    assert calls == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
    ],
)
def test_forbidden_dns_answer_blocks_even_mixed_set(
    address, download_case, monkeypatch
):
    fetch, _, _, _, _, _, _, calls, _, _ = download_case
    monkeypatch.setattr(download_safety, "_resolve", lambda *_: ("1.1.1.1", address))
    with pytest.raises(ValueError, match="download_dns_unverified"):
        fetch()
    assert calls == []


def test_rebinding_or_unverified_tls_stops_before_body(download_case):
    fetch, _, _, _, _, _, _, calls, peer, _ = download_case
    peer["value"] = "127.0.0.1"
    with pytest.raises(ValueError, match="download_peer_unverified"):
        fetch()
    peer["value"] = "1.1.1.1"
    peer["tls"] = False
    with pytest.raises(ValueError, match="download_peer_unverified"):
        fetch()
    assert len(calls) == 2


@pytest.mark.parametrize("status", [301, 302, 307, 308, 429, 500])
def test_redirect_and_error_status_never_followed(status, download_case):
    fetch, _, _, _, _, _, _, calls, _, response = download_case
    response["status"] = status
    response["headers"] = {"location": "http://127.0.0.1/"}
    with pytest.raises(ValueError, match="download_status_rejected"):
        fetch()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    ["length_large", "length_wrong", "mime", "digest", "invalid_image", "size"],
)
def test_stream_and_content_fail_closed(change, download_case, monkeypatch):
    fetch, _, _, _, _, content, digest, _, _, response = download_case
    limit = download_safety.DownloadLimits(100000, 8, 1, 1, 5)
    sha = digest
    if change == "length_large":
        response["headers"]["content-length"] = "100001"
    elif change == "length_wrong":
        response["headers"]["content-length"] = str(len(content) + 1)
    elif change == "mime":
        response["headers"]["content-type"] = "text/html"
    elif change == "digest":
        response["body"] = content + b"x"
    elif change == "invalid_image":
        response["body"] = b"not an image"
        sha = hashlib.sha256(response["body"]).hexdigest()
        monkeypatch.setattr(download_safety, "_verify_download_scope", lambda *_: True)
    else:
        limit = download_safety.DownloadLimits(16, 8, 1, 1, 5)
    with pytest.raises(ValueError):
        fetch(sha=sha, limit=limit)


def test_default_source_and_real_transport_fail_closed(download_case, monkeypatch):
    fetch, _, _, _, _, _, _, calls, _, _ = download_case
    monkeypatch.setattr(download_safety, "_verify_download_scope", lambda *_: False)
    with pytest.raises(ValueError, match="download_source_unverified"):
        fetch()
    assert calls == []


@pytest.mark.parametrize("fault", ["connect", "read"])
def test_transport_timeout_preserves_attempt_and_no_artifact(fault, download_case):
    fetch, service, attempt, sends, _, _, _, _, _, response = download_case
    response["fault"] = fault
    with pytest.raises(ValueError, match="download_failed"):
        fetch()
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == (1,)
    assert sends == [1]


def test_source_revoked_during_stream_rejects_candidate(download_case, monkeypatch):
    fetch, _, _, _, _, _, _, _, _, response = download_case
    allow = {"value": True}
    monkeypatch.setattr(
        download_safety, "_verify_download_scope", lambda *_: allow["value"]
    )
    response["on_read"] = lambda: allow.update(value=False)
    with pytest.raises(ValueError, match="download_source_changed"):
        fetch()


def test_dns_failure_stops_before_transport(download_case, monkeypatch):
    fetch, _, _, _, _, _, _, calls, _, _ = download_case

    def fail(*_args):
        raise OSError("fixture DNS failure")

    monkeypatch.setattr(download_safety, "_resolve", fail)
    with pytest.raises(ValueError, match="download_dns_unverified"):
        fetch()
    assert calls == []


def test_total_budget_stops_stream(download_case, monkeypatch):
    fetch, _, _, _, _, _, _, _, _, _ = download_case
    timestamps = iter([0.0, 10.0])
    monkeypatch.setattr(download_safety, "_monotonic", lambda: next(timestamps))
    with pytest.raises(ValueError, match="download_deadline_exceeded"):
        fetch()


def test_authorized_fixture_uses_dns_process_before_transport(
    download_case, monkeypatch
):
    from ecommerce_product_image_creator import dns_isolation

    from .test_dns_isolation import _worker_result

    fetch, _, _, _, locator, content, _, calls, _, _ = download_case
    monkeypatch.setattr(download_safety, "_verify_dns_policy", lambda *_: True)
    monkeypatch.setattr(dns_isolation, "_dns_worker", _worker_result)
    limits = download_safety.DownloadLimits(
        100000, 8, 1, 1, 5, dns_seconds=3, dns_max_answers=8
    )
    result = fetch(limit=limits)
    assert result.locator_id == locator.locator_id and result.data == content
    assert calls == [
        ("https://artifact.example.org/result", "artifact.example.org", ("1.1.1.1",))
    ]


def test_dns_consumes_overall_budget_before_connect(download_case, monkeypatch):
    from ecommerce_product_image_creator import dns_isolation

    from .test_dns_isolation import _worker_result

    fetch, _, _, _, _, _, _, calls, _, _ = download_case
    monkeypatch.setattr(download_safety, "_verify_dns_policy", lambda *_: True)
    monkeypatch.setattr(dns_isolation, "_dns_worker", _worker_result)
    times = iter([0.0, 10.0])
    monkeypatch.setattr(download_safety, "_monotonic", lambda: next(times))
    limits = download_safety.DownloadLimits(
        100000, 8, 1, 1, 5, dns_seconds=3, dns_max_answers=8
    )
    with pytest.raises(ValueError, match="download_deadline_exceeded"):
        fetch(limit=limits)
    assert calls == []
