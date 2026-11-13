"""Real loopback TLS; the private test CA and loopback exception are test-only."""

import datetime
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ecommerce_product_image_creator import download_safety, pinned_https, provider
from ecommerce_product_image_creator.storage import ResourceBudget

from .test_artifact_reconciliation import first_context  # noqa: F401


def _loopback_dns_worker(host, port, max_answers, output):
    del host, port, max_answers
    output.send_bytes(b'{"addresses":["127.0.0.1"]}')
    output.close()


@pytest.fixture
def tls_server(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pinned_https,
        "_allow_test_loopback",
        lambda address, _port: str(address) == "127.0.0.1",
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "artifact.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("artifact.test")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    certfile = tmp_path / "test-ca.pem"
    keyfile = tmp_path / "test-key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    sni = []
    context.set_servername_callback(lambda _sock, name, _ctx: sni.append(name))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(5)
    state = {
        "body": b"",
        "status": b"200 OK",
        "requests": [],
        "sni": sni,
        "error": None,
        "extra_headers": b"",
    }
    stop = threading.Event()
    hold = threading.Event()
    release = threading.Event()
    state["hold"] = hold
    state["release"] = release

    def serve():
        try:
            while not stop.is_set():
                try:
                    raw, _ = listener.accept()
                except OSError:
                    return
                try:
                    with context.wrap_socket(raw, server_side=True) as conn:
                        request = b""
                        while b"\r\n\r\n" not in request:
                            piece = conn.recv(4096)
                            if not piece:
                                break
                            request += piece
                        state["requests"].append(request)
                        body = state["body"]
                        conn.sendall(
                            b"HTTP/1.1 "
                            + state["status"]
                            + b"\r\nContent-Type: image/png\r\nConnection: close\r\n"
                            + state["extra_headers"]
                            + b"\r\n"
                            + (body[:8] if hold.is_set() else body)
                        )
                        if hold.is_set():
                            release.wait(5)
                except (OSError, ssl.SSLError):
                    pass
        except Exception as exc:  # noqa: BLE001 - report fixture failures
            state["error"] = repr(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield certfile, listener.getsockname()[1], state
    finally:
        stop.set()
        release.set()
        listener.close()
        thread.join(timeout=7)
        assert not thread.is_alive() and state["error"] is None


def test_real_socket_tls_hostname_and_dns_pinning(
    first_context,  # noqa: F811
    tls_server,
    monkeypatch,
):
    service, _, attempt, sends, locator, content, digest, _ = first_context
    certfile, port, state = tls_server
    state["body"] = content
    monkeypatch.setattr(download_safety, "_verify_download_scope", lambda *_: True)
    calls = []

    def test_only_addresses(host, port_number):
        calls.append((host, port_number))
        return ("127.0.0.1",) if len(calls) == 1 else ("127.0.0.2",)

    monkeypatch.setattr(download_safety, "_addresses", test_only_addresses)
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    monkeypatch.setattr(
        download_safety,
        "_open_pinned",
        lambda url, hostname, addresses, limits: pinned_https.open_pinned(
            url, hostname, addresses, limits, port=port
        ),
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    limits = download_safety.DownloadLimits(100000, 8, 2, 2, 5, max_header_bytes=16384)
    result = provider.download_artifact_candidate(
        service.store,
        locator.locator_id,
        "https://artifact.test/image",
        digest,
        "image/png",
        limits,
        ResourceBudget(100000, 10000, 100000),
    )
    assert result.data == content and calls == [("artifact.test", 443)]
    assert state["sni"] == ["artifact.test"]
    assert b"Host: artifact.test\r\n" in state["requests"][0]
    assert b"Authorization:" not in state["requests"][0]
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == (1,)
    assert sends == [1]


def test_real_tls_rejects_hostname_mismatch(tls_server, monkeypatch):
    certfile, port, _ = tls_server
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    limits = download_safety.DownloadLimits(100, 8, 2, 2, 5, max_header_bytes=16384)
    with (
        pytest.raises(ValueError, match="download_tls_unverified"),
        pinned_https.open_pinned(
            "https://wrong.test/", "wrong.test", ("127.0.0.1",), limits, port=port
        ),
    ):
        pass


def test_real_tls_rejects_untrusted_ca(tls_server, monkeypatch):
    _, port, _ = tls_server
    monkeypatch.setattr(pinned_https, "_ssl_context", ssl.create_default_context)
    limits = download_safety.DownloadLimits(100, 8, 2, 2, 5, max_header_bytes=16384)
    with (
        pytest.raises(ValueError, match="download_tls_unverified"),
        pinned_https.open_pinned(
            "https://artifact.test/", "artifact.test", ("127.0.0.1",), limits, port=port
        ),
    ):
        pass


def test_real_socket_read_timeout_closes_without_result(tls_server, monkeypatch):
    certfile, port, state = tls_server
    state["body"] = b"abcdefgh12345678"
    state["hold"].set()
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    limits = download_safety.DownloadLimits(100, 8, 2, 0.2, 2, max_header_bytes=16384)
    try:
        with pinned_https.open_pinned(
            "https://artifact.test/", "artifact.test", ("127.0.0.1",), limits, port=port
        ) as response:
            stream = response.iter_raw(chunk_size=8)
            assert next(stream) == b"abcdefgh"
            with pytest.raises(ValueError, match="download_read_timeout"):
                next(stream)
    finally:
        state["release"].set()


def test_real_socket_total_deadline_closes_without_result(tls_server, monkeypatch):
    certfile, port, state = tls_server
    state["body"] = b"abcdefgh12345678"
    state["hold"].set()
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    limits = download_safety.DownloadLimits(100, 8, 2, 2, 0.3, max_header_bytes=16384)
    try:
        with pinned_https.open_pinned(
            "https://artifact.test/", "artifact.test", ("127.0.0.1",), limits, port=port
        ) as response:
            stream = response.iter_raw(chunk_size=8)
            assert next(stream) == b"abcdefgh"
            with pytest.raises(ValueError, match="download_deadline_exceeded"):
                next(stream)
    finally:
        state["release"].set()


def test_real_redirect_is_not_followed(first_context, tls_server, monkeypatch):  # noqa: F811
    service, _, _, sends, locator, content, digest, _ = first_context
    certfile, port, state = tls_server
    state["body"] = content
    state["status"] = b"302 Found"
    monkeypatch.setattr(download_safety, "_verify_download_scope", lambda *_: True)
    monkeypatch.setattr(download_safety, "_addresses", lambda *_: ("127.0.0.1",))
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    monkeypatch.setattr(
        download_safety,
        "_open_pinned",
        lambda url, hostname, addresses, limits: pinned_https.open_pinned(
            url, hostname, addresses, limits, port=port
        ),
    )
    limits = download_safety.DownloadLimits(100000, 8, 2, 2, 5, max_header_bytes=16384)
    with pytest.raises(ValueError, match="download_status_rejected"):
        provider.download_artifact_candidate(
            service.store,
            locator.locator_id,
            "https://artifact.test/image",
            digest,
            "image/png",
            limits,
            ResourceBudget(100000, 10000, 100000),
        )
    assert len(state["requests"]) == 1 and sends == [1]


def test_real_socket_enforces_stream_budget_without_content_length(
    first_context,  # noqa: F811
    tls_server,
    monkeypatch,
):
    service, _, attempt, sends, locator, content, digest, _ = first_context
    certfile, port, state = tls_server
    state["body"] = content
    monkeypatch.setattr(download_safety, "_verify_download_scope", lambda *_: True)
    monkeypatch.setattr(download_safety, "_addresses", lambda *_: ("127.0.0.1",))
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    monkeypatch.setattr(
        download_safety,
        "_open_pinned",
        lambda url, hostname, addresses, limits: pinned_https.open_pinned(
            url, hostname, addresses, limits, port=port
        ),
    )
    limits = download_safety.DownloadLimits(16, 8, 2, 2, 5, max_header_bytes=16384)
    with pytest.raises(ValueError, match="download_size_invalid"):
        provider.download_artifact_candidate(
            service.store,
            locator.locator_id,
            "https://artifact.test/image",
            digest,
            "image/png",
            limits,
            ResourceBudget(100000, 10000, 100000),
        )
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == (1,)
    assert sends == [1]


def test_two_actions_use_two_distinct_tls_connections(tls_server, monkeypatch):
    certfile, port, state = tls_server
    state["body"] = b"abcdefgh"
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    limits = download_safety.DownloadLimits(100, 8, 2, 2, 5, max_header_bytes=16384)
    for _ in range(2):
        with pinned_https.open_pinned(
            "https://artifact.test/", "artifact.test", ("127.0.0.1",), limits, port=port
        ) as response:
            assert b"".join(response.iter_raw(chunk_size=8)) == b"abcdefgh"
    assert len(state["requests"]) == 2 and state["sni"] == [
        "artifact.test",
        "artifact.test",
    ]


def test_private_addresses_remain_rejected(monkeypatch):
    monkeypatch.setattr(download_safety, "_resolve", lambda *_: ("127.0.0.1",))
    with pytest.raises(ValueError, match="download_dns_unverified"):
        download_safety._addresses("artifact.test", 443)


def test_dns_process_result_is_used_for_actual_loopback_tls(
    first_context,  # noqa: F811
    tls_server,
    monkeypatch,
):
    from ecommerce_product_image_creator import dns_isolation

    service, _, attempt, sends, locator, content, digest, _ = first_context
    certfile, port, state = tls_server
    state["body"] = content
    monkeypatch.setattr(download_safety, "_verify_download_scope", lambda *_: True)
    monkeypatch.setattr(dns_isolation, "_dns_worker", _loopback_dns_worker)
    calls = []

    def loopback_only_test_resolver(host, port_number):
        calls.append((host, port_number))
        result = dns_isolation.resolve_in_process(host, port_number, 3, 8)
        assert result == ("127.0.0.1",)
        return result

    monkeypatch.setattr(download_safety, "_addresses", loopback_only_test_resolver)
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    monkeypatch.setattr(
        download_safety,
        "_open_pinned",
        lambda url, hostname, addresses, limits: pinned_https.open_pinned(
            url, hostname, addresses, limits, port=port
        ),
    )
    limits = download_safety.DownloadLimits(100000, 8, 2, 2, 8, max_header_bytes=16384)
    result = provider.download_artifact_candidate(
        service.store,
        locator.locator_id,
        "https://artifact.test/image",
        digest,
        "image/png",
        limits,
        ResourceBudget(100000, 10000, 100000),
    )
    assert result.data == content and calls == [("artifact.test", 443)]
    assert state["sni"] == ["artifact.test"] and sends == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM operation_attempt WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "mode",
    [
        "duplicate_conflict",
        "duplicate_equal",
        "transfer_encoding",
        "transfer_and_length",
        "content_encoding",
    ],
)
def test_real_response_duplicate_content_length_rejected(
    first_context,  # noqa: F811
    tls_server,
    monkeypatch,
    mode,
):
    service, _, _, sends, locator, content, digest, _ = first_context
    certfile, port, state = tls_server
    state["body"] = content
    header = f"Content-Length: {len(content)}\r\n"
    if mode == "duplicate_conflict":
        header += f"Content-Length: {len(content) + 1}\r\n"
    elif mode == "duplicate_equal":
        header += f"Content-Length: {len(content)}\r\n"
    elif mode == "transfer_encoding":
        header = "Transfer-Encoding: chunked\r\n"
    elif mode == "transfer_and_length":
        header += "Transfer-Encoding: chunked\r\n"
    else:
        header += "Content-Encoding: gzip\r\n"
    state["extra_headers"] = header.encode()
    monkeypatch.setattr(download_safety, "_verify_download_scope", lambda *_: True)
    monkeypatch.setattr(download_safety, "_addresses", lambda *_: ("127.0.0.1",))
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    monkeypatch.setattr(
        download_safety,
        "_open_pinned",
        lambda url, hostname, addresses, limits: pinned_https.open_pinned(
            url, hostname, addresses, limits, port=port
        ),
    )
    limits = download_safety.DownloadLimits(100000, 8, 2, 2, 5, max_header_bytes=16384)
    with pytest.raises(ValueError, match="download_headers_invalid"):
        provider.download_artifact_candidate(
            service.store,
            locator.locator_id,
            "https://artifact.test/image",
            digest,
            "image/png",
            limits,
            ResourceBudget(100000, 10000, 100000),
        )
    assert sends == [1]


def test_real_header_budget_is_required_and_enforced(tls_server, monkeypatch):
    certfile, port, _ = tls_server
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    with (
        pytest.raises(ValueError, match="download_header_budget_required"),
        pinned_https.open_pinned(
            "https://artifact.test/",
            "artifact.test",
            ("127.0.0.1",),
            download_safety.DownloadLimits(100, 8, 2, 2, 5),
            port=port,
        ),
    ):
        pass
    with (
        pytest.raises(ValueError, match="download_headers_too_large"),
        pinned_https.open_pinned(
            "https://artifact.test/",
            "artifact.test",
            ("127.0.0.1",),
            download_safety.DownloadLimits(100, 8, 2, 2, 5, max_header_bytes=32),
            port=port,
        ),
    ):
        pass


def test_real_response_truncated_content_length_rejected(
    first_context,  # noqa: F811
    tls_server,
    monkeypatch,
):
    service, _, _, sends, locator, content, digest, _ = first_context
    certfile, port, state = tls_server
    state["body"] = content
    state["extra_headers"] = f"Content-Length: {len(content) + 1}\r\n".encode()
    monkeypatch.setattr(download_safety, "_verify_download_scope", lambda *_: True)
    monkeypatch.setattr(download_safety, "_addresses", lambda *_: ("127.0.0.1",))
    monkeypatch.setattr(
        pinned_https,
        "_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certfile)),
    )
    monkeypatch.setattr(
        download_safety,
        "_open_pinned",
        lambda url, hostname, addresses, limits: pinned_https.open_pinned(
            url, hostname, addresses, limits, port=port
        ),
    )
    limits = download_safety.DownloadLimits(100000, 8, 2, 2, 5, max_header_bytes=16384)
    with pytest.raises(ValueError, match="download_incomplete"):
        provider.download_artifact_candidate(
            service.store,
            locator.locator_id,
            "https://artifact.test/image",
            digest,
            "image/png",
            limits,
            ResourceBudget(100000, 10000, 100000),
        )
    assert sends == [1]


def test_real_tls_handshake_timeout_closes_socket(monkeypatch):
    monkeypatch.setattr(
        pinned_https,
        "_allow_test_loopback",
        lambda address, _port: str(address) == "127.0.0.1",
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    accepted, release = threading.Event(), threading.Event()

    def hold_connection():
        try:
            conn, _ = listener.accept()
            with conn:
                accepted.set()
                release.wait(5)
        finally:
            listener.close()

    thread = threading.Thread(target=hold_connection)
    thread.start()
    limits = download_safety.DownloadLimits(100, 8, 0.2, 1, 2, max_header_bytes=16384)
    try:
        with (
            pytest.raises(ValueError, match="download_tls_timeout"),
            pinned_https.open_pinned(
                "https://artifact.test/",
                "artifact.test",
                ("127.0.0.1",),
                limits,
                port=port,
            ),
        ):
            pass
        assert accepted.is_set()
    finally:
        release.set()
        thread.join(timeout=6)
        assert not thread.is_alive()


def test_real_local_tcp_timeout_is_sanitized(monkeypatch):
    monkeypatch.setattr(
        pinned_https,
        "_allow_test_loopback",
        lambda address, _port: str(address) == "127.0.0.1",
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        limits = download_safety.DownloadLimits(100, 8, 1, 1, 2, max_header_bytes=16384)
        with (
            pytest.raises(ValueError, match="download_connect_timeout"),
            pinned_https.open_pinned(
                "https://artifact.test/",
                "artifact.test",
                ("127.0.0.1",),
                limits,
                port=port,
            ),
        ):
            pass
