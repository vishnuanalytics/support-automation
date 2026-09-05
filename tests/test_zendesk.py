"""Multi-provider connectors step 2 -- offline tests for the Zendesk case
connector. Every real-HTTP call goes through requests.request, monkeypatched
here; interpreter.zendesk._creds is monkeypatched directly to control the
dry-run vs configured path without touching Vault/Supabase."""

from __future__ import annotations

import pytest

from interpreter import zendesk

_CREDS = {"subdomain": "acme", "email": "bot@acme.com", "api_token": "tok"}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(zendesk, "_creds", lambda tenant_id, sb=None: _CREDS)


class _FakeResp:
    def __init__(self, json_body=None, status=200):
        self._json = json_body or {}
        self.status_code = status
        self.content = b"1" if json_body is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def _record(monkeypatch, responses):
    """responses: list of (method, path_substr) -> json body, consumed in order."""
    calls = []

    def fake_request(method, url, *, auth=None, json=None, params=None, timeout=None):
        calls.append((method, url, json, params))
        for i, (m, sub, body) in enumerate(responses):
            if m == method and sub in url:
                responses.pop(i)
                return _FakeResp(body)
        return _FakeResp({})

    import requests
    monkeypatch.setattr(requests, "request", fake_request)
    return calls


# ── dry-run (no creds) ───────────────────────────────────────────────────
def test_available_false_without_creds():
    assert zendesk.available(None) is False
    assert zendesk.available("t") is False


def test_update_case_fields_dry_runs_without_creds():
    out = zendesk.update_case_fields("1", {"Status": "Escalated"})
    assert out["dry_run"] is True and out["planned"] == {"Status": "Escalated"}


def test_post_note_dry_runs_without_creds():
    assert zendesk.post_note("1", "hi") == {"posted": False, "dry_run": True, "mention_id": None}


def test_add_case_comment_dry_runs_without_creds():
    assert zendesk.add_case_comment("1", "hi") == {"created": False, "dry_run": True, "id": None}


def test_assign_case_dry_runs_without_creds():
    out = zendesk.assign_case("1", queue="Support")
    assert out == {"assigned": False, "dry_run": True, "queue": "Support", "user_id": None}


def test_assign_case_no_target_is_a_clean_noop():
    assert zendesk.assign_case("1") == {"assigned": False, "reason": "no queue or user configured"}


def test_ensure_case_dry_runs_without_creds():
    out = zendesk.ensure_case({"from": "a@b.com"})
    assert out["dry_run"] is True and out["reason"] == "zendesk not configured"


def test_log_email_message_is_always_a_documented_noop(configured):
    out = zendesk.log_email_message("1", incoming=True)
    assert out["created"] is False and "already is the email log" in out["reason"]


def test_identify_sender_dry_runs_without_creds():
    out = zendesk.identify_sender("a@b.com")
    assert out["match"] == "none" and out["reason"] == "zendesk not configured"


def test_send_case_reply_dry_runs_without_creds():
    out = zendesk.send_case_reply("1", "hi")
    assert out == {"sent": False, "dry_run": True, "via": "dry_run", "to": None}


# ── configured: real HTTP calls, mocked ──────────────────────────────────
def test_update_case_fields_maps_status_and_skips_unknown_fields(configured, monkeypatch):
    calls = _record(monkeypatch, [("PUT", "/tickets/1.json", {})])
    out = zendesk.update_case_fields(
        "1", {"Status": "Escalated", "Routed_Team__c": "csm", "AI_Confidence__c": 0.9})
    assert out["dry_run"] is False
    assert out["written"] == {"Status": "Escalated"}
    assert out["skipped"] == {"Routed_Team__c": "csm", "AI_Confidence__c": 0.9}
    assert calls[0][2] == {"ticket": {"status": "open"}}


def test_update_case_fields_append_becomes_a_private_comment(configured, monkeypatch):
    calls = _record(monkeypatch, [("PUT", "/tickets/1.json", {})])
    out = zendesk.update_case_fields("1", {}, append={"Description": "note text"})
    assert calls[0][2] == {"ticket": {"comment": {"body": "note text", "public": False}}}
    assert out["written"]["_append_as_comment"] == ["note text"]


def test_post_note_includes_mention_as_cc_text(configured, monkeypatch):
    calls = _record(monkeypatch, [("PUT", "/tickets/1.json", {})])
    out = zendesk.post_note("1", "please review", mention_id="agent42")
    assert out == {"posted": True, "dry_run": False, "mention_id": "agent42"}
    assert calls[0][2]["ticket"]["comment"]["body"] == "cc: agent42\n\nplease review"
    assert calls[0][2]["ticket"]["comment"]["public"] is False


def test_add_case_comment_public_flag(configured, monkeypatch):
    calls = _record(monkeypatch, [("PUT", "/tickets/1.json", {})])
    zendesk.add_case_comment("1", "draft", published=True)
    assert calls[0][2]["ticket"]["comment"]["public"] is True


def test_assign_case_by_user_id(configured, monkeypatch):
    calls = _record(monkeypatch, [("PUT", "/tickets/1.json", {})])
    out = zendesk.assign_case("1", user_id="42")
    assert out == {"assigned": True, "dry_run": False, "owner_id": "42", "owner_type": "user"}
    assert calls[0][2] == {"ticket": {"assignee_id": "42"}}


def test_assign_case_by_queue_resolves_group_name(configured, monkeypatch):
    calls = _record(monkeypatch, [
        ("GET", "/groups.json", {"groups": [{"id": 7, "name": "Support"}, {"id": 8, "name": "Billing"}]}),
        ("PUT", "/tickets/1.json", {}),
    ])
    out = zendesk.assign_case("1", queue="Billing")
    assert out == {"assigned": True, "dry_run": False, "owner_id": 8, "owner_type": "queue"}
    assert calls[-1][2] == {"ticket": {"group_id": 8}}


def test_assign_case_queue_not_found(configured, monkeypatch):
    _record(monkeypatch, [("GET", "/groups.json", {"groups": []})])
    out = zendesk.assign_case("1", queue="Nope")
    assert out == {"assigned": False, "reason": "queue 'Nope' not found"}


def test_ensure_case_creates_a_new_ticket_and_user(configured, monkeypatch):
    calls = _record(monkeypatch, [
        ("GET", "/users/search.json", {"users": []}),
        ("GET", "/organizations/search.json", {"organizations": []}),
        ("POST", "/organizations.json", {"organization": {"id": 5, "name": "Acme"}}),
        ("POST", "/users.json", {"user": {"id": 9, "organization_id": 5}}),
        ("GET", "/search.json", {"results": []}),
        ("POST", "/tickets.json", {"ticket": {"id": 100, "status": "new"}}),
        ("GET", "/organizations/5.json", {"organization": {"name": "Acme"}}),
    ])
    out = zendesk.ensure_case({"from": "jane@acme.com", "subject": "Help", "body": "please help"})
    assert out["created"] is True and out["sf_id"] == 100 and out["case_number"] == "100"
    assert out["contact_id"] == 9 and out["account_id"] == 5
    ticket_call = [c for c in calls if c[1].endswith("/tickets.json") and c[0] == "POST"][0]
    assert ticket_call[2]["ticket"]["requester_id"] == 9


def test_ensure_case_reuses_an_open_ticket_from_the_same_requester(configured, monkeypatch):
    _record(monkeypatch, [
        ("GET", "/users/search.json", {"users": [{"id": 9, "organization_id": None}]}),
        ("GET", "/search.json", {"results": [{"id": 55, "status": "open"}]}),
    ])
    out = zendesk.ensure_case({"from": "jane@acme.com"}, reuse="thread")
    assert out["reused"] is True and out["sf_id"] == 55 and out["created"] is False


def test_ensure_case_already_has_sf_id_is_a_passthrough(configured):
    out = zendesk.ensure_case({"sf_id": 100, "from": "jane@acme.com"})
    assert out["sf_id"] == 100 and out["created"] is False and out["reused"] is False


def test_identify_sender_matches_an_existing_user(configured, monkeypatch):
    _record(monkeypatch, [
        ("GET", "/users/search.json", {"users": [{"id": 9, "name": "Jane", "organization_id": 5}]}),
        ("GET", "/organizations/5.json", {"organization": {"name": "Acme"}}),
    ])
    out = zendesk.identify_sender("jane@acme.com")
    assert out["match"] == "contact" and out["contact_id"] == 9
    assert out["account_matched"] is True and out["account_name"] == "Acme"


def test_identify_sender_matches_by_domain(configured, monkeypatch):
    _record(monkeypatch, [
        ("GET", "/users/search.json", {"users": []}),
        ("GET", "/organizations/search.json",
         {"organizations": [{"id": 5, "name": "Acme", "domain_names": ["acme.com"]}]}),
    ])
    out = zendesk.identify_sender("newperson@acme.com")
    assert out["match"] == "domain" and out["account_id"] == 5


def test_identify_sender_creates_a_user_when_missing_and_requested(configured, monkeypatch):
    _record(monkeypatch, [
        ("GET", "/users/search.json", {"users": []}),
        ("GET", "/organizations/search.json", {"organizations": []}),
        ("POST", "/users.json", {"user": {"id": 12}}),
    ])
    out = zendesk.identify_sender("nobody@nowhere.test", domain_match=False, create_lead=True)
    assert out["match"] == "lead_created" and out["lead_id"] == 12


def test_send_case_reply_posts_a_public_comment(configured, monkeypatch):
    calls = _record(monkeypatch, [("PUT", "/tickets/1.json", {})])
    out = zendesk.send_case_reply("1", "here's the answer", to_email="jane@acme.com")
    assert out == {"sent": True, "dry_run": False, "via": "ticket_comment", "to": "jane@acme.com"}
    assert calls[0][2]["ticket"]["comment"]["public"] is True


# ── never raises on a transport error ─────────────────────────────────────
def test_functions_never_raise_on_a_transport_error(configured, monkeypatch):
    import requests

    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(requests, "request", boom)

    assert "error" in zendesk.update_case_fields("1", {"Status": "New"})
    assert zendesk.post_note("1", "x")["posted"] is False
    assert zendesk.add_case_comment("1", "x")["created"] is False
    assert "error" in zendesk.assign_case("1", user_id="1")
    assert "reason" in zendesk.ensure_case({"from": "a@b.com"})
    assert "reason" in zendesk.identify_sender("a@b.com")
    assert zendesk.send_case_reply("1", "x")["sent"] is False


# ── connect-account: config model + storage + test_connection ────────────
from interpreter.zendesk import ZendeskConfig  # noqa: E402


def test_zendesk_config_from_row_and_public_status_never_leaks_the_token():
    cfg = ZendeskConfig.from_row("t", {"subdomain": "acme", "email": "bot@acme.com"},
                                "active", {"api_token": "hunter2"})
    assert cfg.subdomain == "acme" and cfg.email == "bot@acme.com"
    status = cfg.public_status()
    assert "hunter2" not in str(status) and status["configured"] is True
    assert "hunter2" not in repr(cfg)


class _TenantIntegrationsSB:
    def __init__(self):
        self.upserts: list[dict] = []
        self.deletes: list[tuple] = []
        self._vault: dict = {}
        self._pending = None

    def table(self, name):
        self._table = name
        return self

    def select(self, *_a):
        return self

    def eq(self, *_a):
        return self

    def rpc(self, name, params):
        self._pending = ("rpc", name, params)
        return self

    def upsert(self, row, on_conflict=None):
        assert on_conflict == "tenant_id,kind,org_label"
        self.upserts.append(row)
        self._pending = ("upsert",)
        return self

    def delete(self):
        self.deletes.append((self._table,))
        self._pending = ("delete",)
        return self

    def execute(self):
        kind, *rest = self._pending
        if kind == "rpc":
            name, params = rest
            if name == "integration_secret_put":
                self._vault[params["p_kind"]] = params["p_plaintext"]
                return type("R", (), {"data": "vault-id"})()
            if name == "integration_secret_get":
                return type("R", (), {"data": self._vault.get(params["p_kind"])})()
            if name == "integration_secret_delete":
                self._vault.pop(params["p_kind"], None)
                return type("R", (), {"data": None})()
            raise AssertionError(f"unexpected rpc {name}")
        if kind == "upsert":
            row = self.upserts[-1]
            return type("R", (), {"data": [{"config": row["config"], "status": row["status"]}]})()
        return type("R", (), {"data": []})()


def test_save_and_delete_channel_round_trip():
    from interpreter.zendesk import delete_channel, save_channel

    sb = _TenantIntegrationsSB()
    cfg = ZendeskConfig(tenant_id="t", subdomain="acme", email="bot@acme.com")
    save_channel(cfg, sb, api_token="tok1")
    import json
    assert json.loads(sb._vault["zendesk"]) == {"api_token": "tok1"}
    assert sb.upserts[-1]["config"] == {"subdomain": "acme", "email": "bot@acme.com"}

    delete_channel("t", sb)
    assert "zendesk" not in sb._vault
    assert sb.deletes == [("tenant_integrations",)]


def test_test_connection_requires_all_three_fields():
    assert zendesk.test_connection(ZendeskConfig(tenant_id="t"))["ok"] is False


def test_test_connection_succeeds(monkeypatch):
    import requests

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    cfg = ZendeskConfig(tenant_id="t", subdomain="acme", email="bot@acme.com", api_token="tok")
    assert zendesk.test_connection(cfg) == {"ok": True, "error": None}


def test_test_connection_reports_bad_auth(monkeypatch):
    import requests

    class _Resp:
        status_code = 401
        def raise_for_status(self):
            pass
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    cfg = ZendeskConfig(tenant_id="t", subdomain="acme", email="bot@acme.com", api_token="bad")
    out = zendesk.test_connection(cfg)
    assert out["ok"] is False and "401" in out["error"]
