"""Test-only Python socket guard; loaded via PYTHONPATH, not production configuration."""

import ipaddress
import socket


def _is_loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except (AttributeError, ValueError):
        return False


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_sendto = socket.socket.sendto
_getaddrinfo = socket.getaddrinfo
_gethostbyname = socket.gethostbyname
_gethostbyname_ex = socket.gethostbyname_ex
_gethostbyaddr = socket.gethostbyaddr
_getnameinfo = socket.getnameinfo


def _check_address(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and (
        not isinstance(address, tuple) or not _is_loopback(address[0])
    ):
        raise PermissionError("ci_network_guard: non-loopback destination blocked")


def _guarded_connect(sock, address):
    _check_address(sock, address)
    return _connect(sock, address)


def _guarded_connect_ex(sock, address):
    _check_address(sock, address)
    return _connect_ex(sock, address)


def _guarded_sendto(sock, data, *args):
    _check_address(sock, args[-1])
    return _sendto(sock, data, *args)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _is_loopback(host):
        raise PermissionError("ci_network_guard: external DNS blocked")
    return _getaddrinfo(host, *args, **kwargs)


def _guarded_gethostbyname(host):
    if not _is_loopback(host):
        raise PermissionError("ci_network_guard: external DNS blocked")
    return _gethostbyname(host)


def _guarded_gethostbyname_ex(host):
    if not _is_loopback(host):
        raise PermissionError("ci_network_guard: external DNS blocked")
    return _gethostbyname_ex(host)


def _guarded_gethostbyaddr(host):
    if not _is_loopback(host):
        raise PermissionError("ci_network_guard: external DNS blocked")
    return _gethostbyaddr(host)


def _guarded_getnameinfo(address, flags):
    if not isinstance(address, tuple) or not _is_loopback(address[0]):
        raise PermissionError("ci_network_guard: external DNS blocked")
    return _getnameinfo(address, flags)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.socket.sendto = _guarded_sendto
socket.getaddrinfo = _guarded_getaddrinfo
socket.gethostbyname = _guarded_gethostbyname
socket.gethostbyname_ex = _guarded_gethostbyname_ex
socket.gethostbyaddr = _guarded_gethostbyaddr
socket.getnameinfo = _guarded_getnameinfo
