"""P6c — the `http_request` node + per-tenant connections."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import connections
from interpreter.registry import h_http_request


# ── connections helpers ──────────────────────────────────────────────
def test_auth_headers_by_type():
    assert connections.auth_headers({"type": "bearer", "token": "t"}) == {"Authorization": "Bearer t"}
    assert connections.auth_headers({"type": "header", "header_name": "X-Key", "value": "v"}) == {"X-Key": "v"}
    assert connections.auth_headers({"type": "basic", "username": "u", "password": "p"})["Authorization"].startswith("Basic ")
    assert connections.auth_headers({"type": "none"}) == {}
    assert connections.auth_headers(None) == {}


def test_redact_strips_the_secret():
    r = connections.redact({"slug": "vendor", "base_url": "https://x",
                            "auth": {"type": "bearer", "token": "SECRET"},
                            "created_at": "t"})
    assert r["auth"] == {"type": "bearer"} and r["has_secret"] is True
    assert "token" not in str(r)
    r2 = connections.redact({"slug": "s", "base_url": "https://x", "auth": {"type": "none"}})
    assert r2["has_secret"] is False


# ── the node ─────────────────────────────────────────────────────────
@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(connections, "resolve",
                        lambda tid, slug, **k: (
                            {"base_url": "https://api.vendor.com",
                             "auth": {"type": "bearer", "token": "tok"}}
                            if slug == "vendor" else None))


def _fake_requests(monkeypatch, *, status=200, json_body=None, capture=None):
    class _Resp:
        status_code = status
        headers = {"content-type": "application/json"}
        def json(self): return json_body if json_body is not None else {"ok": 1}
        text = "{}"

    def _req(method, url, **kw):
        if capture is not None:
            capture.update(method=method, url=url, **kw)
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "request", _req)


def test_http_request_success_writes_context(monkeypatch, conn):
    cap = {}
    _fake_requests(monkeypatch, status=201, json_body={"id": 9}, capture=cap)
    out = h_http_request(
        {"tenant_id": "t", "context": {"id": "42", "term": "webhooks"}},
        {"_node_id": "h", "connection": "vendor", "method": "get",
         "path": "/v1/things/{{context.id}}", "query": {"q": "{{context.term}}"},
         "out_key": "vendor_thing"})
    assert cap["url"] == "https://api.vendor.com/v1/things/42"
    assert cap["params"] == {"q": "webhooks"}
    assert cap["headers"]["Authorization"] == "Bearer tok"
    res = out["context"]["vendor_thing"]
    assert res["status"] == 201 and res["ok"] is True and res["json"] == {"id": 9}


def test_http_request_unknown_connection_is_a_soft_error(conn):
    out = h_http_request({"tenant_id": "t", "context": {}},
                         {"_node_id": "h", "connection": "nope", "out_key": "x"})
    assert "error" in out["context"]["x"] and "nope" in out["context"]["x"]["error"]


def test_http_request_rejects_an_absolute_url_in_path(conn):
    with pytest.raises(ValueError):
        h_http_request({"tenant_id": "t", "context": {}},
                       {"_node_id": "h", "connection": "vendor",
                        "path": "https://evil.example/steal"})


def test_http_request_on_error_fail_raises(monkeypatch, conn):
    import requests
    monkeypatch.setattr(requests, "request",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("slow")))
    with pytest.raises(TimeoutError):
        h_http_request({"tenant_id": "t", "context": {}},
                       {"_node_id": "h", "connection": "vendor", "path": "/x",
                        "on_error": "fail"})
    # default (passthrough) swallows it
    out = h_http_request({"tenant_id": "t", "context": {}},
                         {"_node_id": "h", "connection": "vendor", "path": "/x", "out_key": "y"})
    assert "error" in out["context"]["y"]


# ── transform node ──────────────────────────────────────────────────
def test_transform_maps_and_templates_into_context():
    from interpreter.registry import h_transform
    state = {"context": {"http": {"json": {"total": 7, "name": "acme"}}},
             "classification": {"topic": "billing"}}
    out = h_transform(state, {"_node_id": "x",
                              "map": {"count": "context.http.json.total",
                                      "topic": "classification.topic"},
                              "set": {"summary": "{{context.http.json.name}} · {{context.http.json.total}}"},
                              "drop": ["http"]})
    c = out["context"]
    assert c["count"] == 7 and c["topic"] == "billing"
    assert c["summary"] == "acme · 7"
    assert c["http"] is None            # nulled
    assert out["trace"][0]["type"] == "transform"


def test_transform_respects_into():
    from interpreter.registry import h_transform
    out = h_transform({"context": {"a": 1}}, {"_node_id": "x", "map": {"b": "context.a"}, "into": "ai"})
    assert out["ai"] == {"b": 1} and "context" not in out
