"""
Security fix (2026-09-03) — SSRF audit found two gaps: `ingestion.webcrawl`'s
private-host check was a hostname-*string* match (bypassable by DNS
rebinding: a domain resolves publicly when checked, privately when
requested) and never re-applied after a redirect; `api.main.create_connection`
had no host check on `base_url` at all. `interpreter.net_safety` is the
shared fix — resolve the real IP(s), not the hostname string.
"""

from __future__ import annotations

import pathlib
import socket
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import net_safety


def test_ip_literal_targets_are_checked_directly():
    assert net_safety.is_public_http_url("http://93.184.216.34/x") == (True, "")
    ok, reason = net_safety.is_public_http_url("http://127.0.0.1/x")
    assert not ok and "non-public" in reason
    ok, reason = net_safety.is_public_http_url("http://169.254.169.254/latest/meta-data")
    assert not ok and "non-public" in reason  # cloud instance metadata


def test_rejects_non_http_schemes():
    ok, reason = net_safety.is_public_http_url("ftp://example.com/x")
    assert not ok and "http" in reason


def test_rejects_a_host_with_no_hostname():
    ok, reason = net_safety.is_public_http_url("http:///x")
    assert not ok


def test_unresolvable_host_is_rejected(monkeypatch):
    def fake_getaddrinfo(host, port):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(net_safety.socket, "getaddrinfo", fake_getaddrinfo)
    ok, reason = net_safety.is_public_http_url("http://does-not-exist.example/x")
    assert not ok and "does not resolve" in reason


def test_a_public_looking_hostname_is_allowed(monkeypatch):
    monkeypatch.setattr(net_safety.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])
    ok, reason = net_safety.is_public_http_url("http://help.acme.example/docs")
    assert ok and reason == ""


def test_dns_rebinding_a_hostname_that_resolves_privately_is_rejected(monkeypatch):
    """The exact gap a hostname-string check misses: the domain itself
    contains no 'localhost'/'127.'/etc, but it resolves to a private
    address -- this is what the resolved-IP check is FOR."""
    monkeypatch.setattr(net_safety.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("10.0.0.5", 0))])
    ok, reason = net_safety.is_public_http_url("http://looks-external.example/x")
    assert not ok and "non-public" in reason


def test_any_resolved_address_being_private_fails_the_whole_host(monkeypatch):
    """A host with multiple A records, one public and one private, must be
    rejected -- a round-robin/load-balancer could otherwise land on the
    private one on the actual request."""
    monkeypatch.setattr(net_safety.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0)),
                                            (2, 1, 6, "", ("192.168.1.1", 0))])
    ok, reason = net_safety.is_public_http_url("http://mixed.example/x")
    assert not ok
