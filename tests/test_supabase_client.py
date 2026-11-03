"""interpreter/supabase_client.py — the hardened Supabase transport (offline).

Reads retry on a dropped connection; writes never do (the server may have
applied them); HTTP/2 is off; and a transport error that escapes an API
handler is a 503 + Retry-After, not a 500.
"""

from __future__ import annotations

import os

import httpx
import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")

from interpreter import supabase_client as sc  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)


def _flaky(monkeypatch, fails: int, exc: type[Exception] = httpx.RemoteProtocolError):
    """Make the underlying HTTPTransport raise `exc` `fails` times, then 200."""
    calls = {"n": 0}

    def fake(self, request):
        calls["n"] += 1
        if calls["n"] <= fails:
            raise exc("Server disconnected", request=request)
        return httpx.Response(200, json=[{"ok": True}], request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake)
    return calls


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_idempotent_request_retries_through_a_dropped_connection(monkeypatch, method):
    calls = _flaky(monkeypatch, fails=2)
    with sc._http_client() as c:
        r = c.request(method, "https://example.supabase.co/rest/v1/tenants")
    assert r.status_code == 200
    assert calls["n"] == 3  # 2 drops + the success


def test_read_gives_up_after_the_retry_budget(monkeypatch):
    calls = _flaky(monkeypatch, fails=sc.READ_RETRIES + 1)
    with sc._http_client() as c, pytest.raises(httpx.RemoteProtocolError):
        c.get("https://example.supabase.co/rest/v1/tenants")
    assert calls["n"] == sc.READ_RETRIES + 1


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_writes_and_rpcs_are_never_retried(monkeypatch, method):
    """A dropped write may already have been applied server-side (an
    insert, a claim_job RPC) -- retrying could duplicate it."""
    calls = _flaky(monkeypatch, fails=1)
    with sc._http_client() as c, pytest.raises(httpx.RemoteProtocolError):
        c.request(method, "https://example.supabase.co/rest/v1/rpc/claim_job", json={})
    assert calls["n"] == 1


def test_read_error_is_also_retried(monkeypatch):
    calls = _flaky(monkeypatch, fails=1, exc=httpx.ReadError)
    with sc._http_client() as c:
        assert c.get("https://example.supabase.co/rest/v1/x").status_code == 200
    assert calls["n"] == 2


def test_make_client_uses_http1_and_a_private_http_client():
    a = sc.make_client("https://example.supabase.co", "k")
    b = sc.make_client("https://example.supabase.co", "k")
    sa, sb_ = a.postgrest.session, b.postgrest.session
    assert isinstance(sa._transport, sc._RetryTransport)
    assert sa._transport._pool._http2 is False
    # never shared: per-user RLS clients must not have a common http object
    assert sa is not sb_


def test_api_turns_an_escaped_transport_error_into_a_503(monkeypatch):
    from fastapi.testclient import TestClient

    from api import main

    def boom():
        raise httpx.RemoteProtocolError("Server disconnected")

    main.app.add_api_route("/__test_transport_boom", boom, methods=["GET"])
    try:
        r = TestClient(main.app).get("/__test_transport_boom")
    finally:
        main.app.router.routes[:] = [rt for rt in main.app.router.routes
                                     if getattr(rt, "path", None) != "/__test_transport_boom"]
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "2"
    assert "retry" in r.json()["detail"]
