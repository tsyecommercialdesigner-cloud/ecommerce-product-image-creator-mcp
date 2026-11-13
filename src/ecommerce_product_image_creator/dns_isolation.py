"""Request-scoped, killable DNS subprocess; caller must separately authorize it."""

import json
import math
import multiprocessing
import socket
from multiprocessing.connection import Connection


class DNSResolutionError(ValueError):
    """Sanitized DNS worker failure."""


def _dns_worker(host: str, port: int, max_answers: int, output: Connection) -> None:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(str(row[4][0]) for row in answers))
        if not addresses or len(addresses) > max_answers:
            output.send_bytes(b'{"error":"dns_result_invalid"}')
        else:
            output.send_bytes(
                json.dumps({"addresses": addresses}, separators=(",", ":")).encode(
                    "ascii"
                )
            )
    except (OSError, ValueError, UnicodeError):
        output.send_bytes(b'{"error":"dns_resolution_failed"}')
    finally:
        output.close()


def resolve_in_process(
    host: str, port: int, timeout: float, max_answers: int
) -> tuple[str, ...]:
    """Wait for worker exit; on timeout terminate, kill if necessary, then reap."""
    if (
        not isinstance(host, str)
        or not host
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or not math.isfinite(timeout)
        or timeout <= 0
        or not isinstance(max_answers, int)
        or isinstance(max_answers, bool)
        or max_answers <= 0
    ):
        raise DNSResolutionError("download_dns_unverified")
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    worker = ctx.Process(
        target=_dns_worker, args=(host, port, max_answers, writer), daemon=True
    )
    started = False
    try:
        worker.start()
        started = True
        writer.close()
        worker.join(timeout)
        if worker.is_alive():
            try:
                worker.terminate()
            except OSError:
                pass
            worker.join(1)
            if worker.is_alive():
                try:
                    worker.kill()
                except OSError:
                    pass
                worker.join(1)
            if worker.is_alive():
                raise DNSResolutionError("download_dns_worker_unstopped")
            raise DNSResolutionError("download_dns_timeout")
        try:
            if worker.exitcode != 0 or not reader.poll():
                raise DNSResolutionError("download_dns_unverified")
            data = json.loads(reader.recv_bytes(maxlength=max_answers * 64 + 128))
        except (OSError, EOFError, ValueError, UnicodeError):
            raise DNSResolutionError("download_dns_unverified") from None
        addresses = data.get("addresses") if isinstance(data, dict) else None
        if (
            not isinstance(addresses, list)
            or not addresses
            or len(addresses) > max_answers
            or any(not isinstance(item, str) for item in addresses)
        ):
            raise DNSResolutionError("download_dns_unverified")
        return tuple(addresses)
    finally:
        writer.close()
        reader.close()
        if started:
            if worker.is_alive():
                try:
                    worker.terminate()
                except OSError:
                    pass
                worker.join(1)
            if worker.is_alive():
                try:
                    worker.kill()
                except OSError:
                    pass
                worker.join(1)
            if worker.is_alive():
                raise DNSResolutionError("download_dns_worker_unstopped")
            worker.close()
