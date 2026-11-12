"""DNS subprocess tests never use public DNS or production authorization."""

import ctypes
import json
import os
import threading
from pathlib import Path

import pytest

from ecommerce_product_image_creator import dns_isolation, download_safety


def _worker_result(host, port, max_answers, output):
    del port, max_answers
    values = {
        "safe.test": ["1.1.1.1", "2606:4700:4700::1111"],
        "artifact.example.org": ["1.1.1.1"],
        "mixed.test": ["1.1.1.1", "127.0.0.1"],
        "mapped.test": ["::ffff:127.0.0.1"],
        "empty.test": [],
    }
    try:
        output.send_bytes(json.dumps({"addresses": values[host]}).encode("ascii"))
    finally:
        output.close()


def _worker_hang(host, port, max_answers, output):
    del port, max_answers
    Path(host).write_text(str(os.getpid()), encoding="ascii")
    try:
        threading.Event().wait(30)
    finally:
        output.close()


def _worker_fail(host, port, max_answers, output):
    del host, port, max_answers
    output.close()


def _worker_rotate(host, port, max_answers, output):
    del host, port, max_answers
    state = Path(os.environ["T049_DNS_TEST_STATE"])
    count = int(state.read_text(encoding="ascii"))
    state.write_text(str(count + 1), encoding="ascii")
    output.send_bytes(
        json.dumps({"addresses": ["1.1.1.1" if count == 0 else "8.8.8.8"]}).encode(
            "ascii"
        )
    )
    output.close()


def _limits():
    return download_safety.DownloadLimits(
        1000, 8, 2, 2, 4, max_header_bytes=1024, dns_seconds=2, dns_max_answers=8
    )


def test_policy_defaults_reject_without_starting_worker(monkeypatch):
    def fail(*_args):
        raise AssertionError("worker must not start")

    monkeypatch.setattr(dns_isolation, "resolve_in_process", fail)
    with pytest.raises(ValueError, match="download_dns_unverified"):
        download_safety._addresses("safe.test", 443, _limits())


@pytest.mark.parametrize("host", ["mixed.test", "mapped.test", "empty.test"])
def test_isolated_dns_rejects_mixed_mapped_and_empty(host, monkeypatch):
    monkeypatch.setattr(download_safety, "_verify_dns_policy", lambda *_: True)
    monkeypatch.setattr(dns_isolation, "_dns_worker", _worker_result)
    with pytest.raises(ValueError, match="download_dns_unverified"):
        download_safety._addresses(host, 443, _limits())


def test_isolated_dns_accepts_all_public_addresses(monkeypatch):
    monkeypatch.setattr(download_safety, "_verify_dns_policy", lambda *_: True)
    monkeypatch.setattr(dns_isolation, "_dns_worker", _worker_result)
    assert download_safety._addresses("safe.test", 443, _limits()) == (
        "1.1.1.1",
        "2606:4700:4700::1111",
    )


def test_each_request_runs_fresh_dns_worker(tmp_path, monkeypatch):
    state = tmp_path / "dns-state.txt"
    state.write_text("0", encoding="ascii")
    monkeypatch.setenv("T049_DNS_TEST_STATE", str(state))
    monkeypatch.setattr(download_safety, "_verify_dns_policy", lambda *_: True)
    monkeypatch.setattr(dns_isolation, "_dns_worker", _worker_rotate)
    assert download_safety._addresses("same.test", 443, _limits()) == ("1.1.1.1",)
    assert download_safety._addresses("same.test", 443, _limits()) == ("8.8.8.8",)
    assert state.read_text(encoding="ascii") == "2"


def test_crashed_worker_is_rejected(monkeypatch):
    monkeypatch.setattr(dns_isolation, "_dns_worker", _worker_fail)
    with pytest.raises(ValueError, match="download_dns_unverified"):
        dns_isolation.resolve_in_process("fixture.test", 443, 2, 8)


def test_blocked_worker_is_terminated_and_reaped(tmp_path, monkeypatch):
    marker = tmp_path / "worker-pid.txt"
    monkeypatch.setattr(dns_isolation, "_dns_worker", _worker_hang)
    with pytest.raises(ValueError, match="download_dns_timeout"):
        dns_isolation.resolve_in_process(str(marker), 443, 3, 8)
    pid = int(marker.read_text(encoding="ascii"))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if handle:
        try:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            assert code.value != 259
        finally:
            kernel.CloseHandle(handle)


def test_system_getaddrinfo_numeric_input_in_isolated_worker():
    # A numeric loopback input invokes the system API without a public DNS lookup.
    assert dns_isolation.resolve_in_process("127.0.0.1", 443, 5, 8) == ("127.0.0.1",)


def test_unstoppable_worker_reports_failure_without_claiming_cleanup(monkeypatch):
    class Pipe:
        def close(self):
            pass

    class Worker:
        def __init__(self):
            self.calls = []

        def start(self):
            pass

        def join(self, _timeout):
            pass

        def is_alive(self):
            return True

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

    worker = Worker()

    class Context:
        def Pipe(self, duplex):
            assert not duplex
            return Pipe(), Pipe()

        def Process(self, **_kwargs):
            return worker

    monkeypatch.setattr(
        dns_isolation.multiprocessing, "get_context", lambda _: Context()
    )
    with pytest.raises(ValueError, match="download_dns_worker_unstopped"):
        dns_isolation.resolve_in_process("safe.test", 443, 1, 8)
    assert "terminate" in worker.calls and "kill" in worker.calls
