"""
Shared SSRF guard for anything that fetches a URL derived from tenant input
(the KB web-crawler, a connection's `base_url`).

A hostname-*string* check (`^(localhost|127\\.|10\\.|...)`) is bypassable by
DNS rebinding: a domain can resolve to a public IP when it's validated and a
private one when it's actually requested. `is_public_http_url` resolves the
host and checks the real IP(s) instead.

    ok, reason = is_public_http_url(url)
    if not ok:
        raise ValueError(reason)
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _is_unsafe_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True  # unparseable -- don't let it through
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def is_public_http_url(url: str) -> tuple[bool, str]:
    """(ok, reason) -- resolves the hostname and rejects anything that
    resolves to a private / loopback / link-local / reserved / multicast
    address, on top of requiring http(s). Every resolved address must be
    safe (not just the first) -- a host with mixed public/private A records
    could otherwise round-robin to the private one."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False, "must be http(s)"
    host = p.hostname
    if not host:
        return False, "no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return False, f"host does not resolve ({e})"
    if not infos:
        return False, "host does not resolve"
    for info in infos:
        if _is_unsafe_ip(info[4][0]):
            return False, f"{host!r} resolves to a non-public address"
    return True, ""
