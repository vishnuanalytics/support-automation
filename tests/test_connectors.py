"""FR-47 — the declarative connector registry + the generic `connector_action`
node. Builtins (salesforce/slack) are thin wrappers over the existing,
unmodified `salesforce.py`/`slack.py` — faked here the same way the rest of
the suite fakes them, not re-tested. A tenant's own HTTP connection becomes a
connector via its saved `connection_actions` rows (interpreter/connections.py)."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import connections, connectors
from interpreter.registry import h_connector_action


# ── registry ─────────────────────────────────────────────────────────
def test_builtins_are_registered():
    slugs = {s.slug for s in connectors.list_connectors(None)}
    assert {"salesforce", "slack"} <= slugs


# ── multi-provider connectors step 1: resolve_case_connector (migration 084) ──
class _TenantSB:
    """.table('tenants').select('case_connector').eq('tenant_id', x).execute()"""
    def __init__(self, row: dict | None = None, *, raises: bool = False):
        self._row = row
        self._raises = raises

    def table(self, name):
        assert name == "tenants"
        return self

    def select(self, *_a):
        return self

    def eq(self, *_a):
        return self

    def execute(self):
        if self._raises:
            raise RuntimeError("boom")
        return type("R", (), {"data": [self._row] if self._row else []})()


def test_resolve_case_connector_no_tenant_defaults_salesforce():
    assert connectors.resolve_case_connector(None, {}) == "salesforce"


def test_resolve_case_connector_config_override_wins_over_everything():
    sb = _TenantSB({"case_connector": "zendesk"})
    assert connectors.resolve_case_connector("t", {"connector": "custom"}, sb=sb) == "custom"


def test_resolve_case_connector_reads_the_tenant_default():
    sb = _TenantSB({"case_connector": "zendesk"})
    assert connectors.resolve_case_connector("t", {}, sb=sb) == "zendesk"


def test_resolve_case_connector_falls_back_when_tenant_row_missing():
    sb = _TenantSB(None)
    assert connectors.resolve_case_connector("t", {}, sb=sb) == "salesforce"


def test_resolve_case_connector_falls_back_on_fetch_error():
    sb = _TenantSB(raises=True)
    assert connectors.resolve_case_connector("t", {}, sb=sb) == "salesforce"


def test_resolve_case_connector_never_hits_network_offline_with_no_sb():
    """Under pytest, with no `sb` passed and no override, this must not reach
    for a real Supabase client (matches routing.py's `_fetch_rows` — see
    `PYTEST_CURRENT_TEST` guard) -- proven by monkeypatching connectors._sb
    to blow up if it's ever called."""
    def boom():
        raise AssertionError("should not construct a real Supabase client offline")
    import interpreter.connectors as _c
    orig = _c._sb
    _c._sb = boom
    try:
        assert connectors.resolve_case_connector("t", {}) == "salesforce"
    finally:
        _c._sb = orig


def test_get_action_unknown_connector_raises_keyerror(monkeypatch):
    monkeypatch.setattr(connections, "resolve", lambda *a, **k: None)  # stay offline
    with pytest.raises(KeyError):
        connectors.get_action("t", "nope", "whatever")


def test_get_action_unknown_action_raises_keyerror():
    with pytest.raises(KeyError):
        connectors.get_action("t", "slack", "nope")


def test_get_action_finds_a_builtin():
    spec, action = connectors.get_action("t", "salesforce", "post_note")
    assert spec.slug == "salesforce" and action.name == "post_note"


# ── salesforce/slack builtin actions call the real modules ─────────────
def test_sf_post_note_calls_salesforce_post_chatter(monkeypatch):
    from interpreter import salesforce
    cap = {}
    monkeypatch.setattr(salesforce, "post_chatter",
                        lambda cid, body, **k: cap.update(case_id=cid, body=body, **k) or {"posted": True})
    _spec, action = connectors.get_action("t1", "salesforce", "post_note")
    out = action.impl("t1", "orgA", {"case_id": "500x", "body": "hello"})
    assert out == {"posted": True}
    assert cap == {"case_id": "500x", "body": "hello", "mention_id": None,
                   "tenant_id": "t1", "org_label": "orgA"}


def test_slack_post_message_calls_slack_post_message(monkeypatch):
    from interpreter import slack
    cap = {}
    monkeypatch.setattr(slack, "post_message",
                        lambda text, **k: cap.update(text=text, **k) or {"sent": True})
    _spec, action = connectors.get_action("t1", "slack", "post_message")
    out = action.impl("t1", None, {"text": "hi", "channel": "#ops"})
    assert out == {"sent": True}
    assert cap["text"] == "hi" and cap["channel"] == "#ops" and cap["tenant_id"] == "t1"


# ── the connector_action node ───────────────────────────────────────────
def test_connector_action_renders_params_and_writes_context(monkeypatch):
    from interpreter import salesforce
    monkeypatch.setattr(salesforce, "update_case_fields",
                        lambda cid, fields, **k: {"written": fields, "case_id": cid})
    state = {"tenant_id": "t1", "case": {"sf_id": "500x"}, "context": {}}
    out = h_connector_action(state, {
        "_node_id": "n1", "connector": "salesforce", "action": "update_fields",
        "params": {"case_id": "{{case.sf_id}}", "fields": {"Priority": "High"}},
        "out_key": "sf_out",
    })
    assert out["context"]["sf_out"] == {"written": {"Priority": "High"}, "case_id": "500x"}
    assert out["trace"][0]["type"] == "connector_action"


def test_connector_action_unknown_connector_is_a_soft_error(monkeypatch):
    monkeypatch.setattr(connections, "resolve", lambda *a, **k: None)  # stay offline
    out = h_connector_action({"tenant_id": "t1", "context": {}},
                             {"_node_id": "n1", "connector": "nope", "action": "x", "out_key": "y"})
    assert "error" in out["context"]["y"]


def test_connector_action_unknown_connector_on_error_fail_raises(monkeypatch):
    monkeypatch.setattr(connections, "resolve", lambda *a, **k: None)  # stay offline
    with pytest.raises(KeyError):
        h_connector_action({"tenant_id": "t1", "context": {}},
                           {"_node_id": "n1", "connector": "nope", "action": "x", "on_error": "fail"})


def test_connector_action_impl_error_respects_on_error(monkeypatch):
    from interpreter import slack
    monkeypatch.setattr(slack, "post_message",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = h_connector_action({"tenant_id": "t1", "context": {}},
                             {"_node_id": "n1", "connector": "slack", "action": "post_message",
                              "params": {"text": "hi", "channel": "#x"}, "out_key": "y"})
    assert "boom" in out["context"]["y"]["error"]

    with pytest.raises(RuntimeError):
        h_connector_action({"tenant_id": "t1", "context": {}},
                           {"_node_id": "n1", "connector": "slack", "action": "post_message",
                            "params": {"text": "hi", "channel": "#x"}, "on_error": "fail"})


# ── a tenant's own HTTP connection as a connector (connection_actions) ──
@pytest.fixture
def http_conn(monkeypatch):
    conn_row = {"connection_id": "c1", "base_url": "https://api.vendor.com",
               "auth": {"type": "bearer", "token": "tok"}}
    action_row = {"name": "create_ticket", "method": "POST", "path": "/tickets/{{id}}.json",
                 "params": [{"key": "id", "label": "Id", "type": "template", "required": True},
                           {"key": "subject", "label": "Subject", "type": "template", "required": True}],
                 "body_template": {"ticket": {"subject": "{{subject}}"}}}
    monkeypatch.setattr(connections, "resolve",
                        lambda tid, slug, **k: conn_row if slug == "vendor" else None)
    monkeypatch.setattr(connections, "list_actions", lambda cid, **k: [action_row] if cid == "c1" else [])

    class _Query:
        def __init__(self, data): self._data = data
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def order(self, *a, **k): return self
        def execute(self): return type("R", (), {"data": self._data})()

    class _FakeSb:
        def table(self, name):
            return _Query([{"slug": "vendor"}] if name == "connections" else [])

    monkeypatch.setattr(connections, "_sb", lambda: _FakeSb())
    return conn_row, action_row


def test_http_connection_is_exposed_as_a_connector(http_conn):
    spec = connections.as_connector("t1", "vendor")
    assert spec.slug == "vendor" and spec.auth == "apikey"
    assert set(spec.actions) == {"create_ticket"}
    assert spec.actions["create_ticket"].params[0]["key"] == "id"

    specs = connectors.list_connectors("t1")
    assert any(s.slug == "vendor" for s in specs)


def test_http_connection_missing_slug_returns_none(http_conn):
    assert connections.as_connector("t1", "not-a-slug") is None


def test_connector_action_calls_a_saved_http_action(monkeypatch, http_conn):
    cap = {}

    class _Resp:
        status_code = 201
        headers = {"content-type": "application/json"}
        def json(self): return {"id": 7}
        text = "{}"

    def _req(method, url, **kw):
        cap.update(method=method, url=url, **kw)
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "request", _req)

    state = {"tenant_id": "t1", "context": {}}
    out = h_connector_action(state, {
        "_node_id": "n1", "connector": "vendor", "action": "create_ticket",
        "params": {"id": "42", "subject": "help"}, "out_key": "vendor_out",
    })
    assert cap["method"] == "POST"
    assert cap["url"] == "https://api.vendor.com/tickets/42.json"
    assert cap["json"] == {"ticket": {"subject": "help"}}
    assert out["context"]["vendor_out"]["status"] == 201


# ── integration (live Supabase — GET /api/connectors + connection_actions CRUD) ──
GLOBEX_TENANT = "22222222-2222-2222-2222-222222222222"

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module")
def auth_headers():
    if os.environ.get("SUPABASE_ANON_KEY", "test-anon-key") == "test-anon-key":
        pytest.skip("no real SUPABASE_ANON_KEY — integration tests skipped")
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])
    sess = sb.auth.sign_in_with_password(
        {"email": "globex-owner@example.test", "password": "editor-test-pw-8891"}
    )
    return {"Authorization": f"Bearer {sess.session.access_token}"}


@pytest.mark.integration
def test_connectors_catalog_includes_builtins(auth_headers):
    r = client.get(f"/api/connectors?tenant_id={GLOBEX_TENANT}", headers=auth_headers)
    assert r.status_code == 200, r.text
    slugs = {c["slug"] for c in r.json()}
    assert {"salesforce", "slack"} <= slugs


@pytest.mark.integration
def test_a_user_defined_connection_action_is_a_real_connector_end_to_end(auth_headers):
    """FR-47's actual proof: save a connection + a named action purely
    through the HTTP API (no code) and see it appear in the connector
    catalog with its declared params — real Supabase, real RLS, real
    migration 083, not a mock."""
    slug = "test-connectors-vendor"
    r = client.post("/api/connections", headers=auth_headers, json={
        "slug": slug, "base_url": "https://example.com",
        "auth": {"type": "bearer", "token": "tok"}, "tenant_id": GLOBEX_TENANT,
    })
    assert r.status_code == 201, r.text

    r = client.post(f"/api/connections/{slug}/actions?tenant_id={GLOBEX_TENANT}",
                    headers=auth_headers, json={
                        "name": "ping", "method": "GET", "path": "/ping",
                        "params": [{"key": "note", "label": "Note", "type": "string", "required": False}],
                    })
    assert r.status_code == 201, r.text

    try:
        r = client.get(f"/api/connectors?tenant_id={GLOBEX_TENANT}", headers=auth_headers)
        assert r.status_code == 200, r.text
        by_slug = {c["slug"]: c for c in r.json()}
        assert slug in by_slug
        actions = {a["name"]: a for a in by_slug[slug]["actions"]}
        assert actions["ping"]["params"][0]["key"] == "note"

        r = client.get(f"/api/connections/{slug}/actions?tenant_id={GLOBEX_TENANT}", headers=auth_headers)
        assert r.status_code == 200 and any(a["name"] == "ping" for a in r.json())
    finally:
        client.delete(f"/api/connections/{slug}/actions/ping?tenant_id={GLOBEX_TENANT}", headers=auth_headers)
        client.delete(f"/api/connections/{slug}?tenant_id={GLOBEX_TENANT}", headers=auth_headers)
