"""Multi-provider connectors step 3 -- offline tests for the HubSpot case
connector. Every real-HTTP call goes through requests.request, monkeypatched
here; interpreter.hubspot._creds is monkeypatched directly to control the
dry-run vs configured path without touching Vault/Supabase."""

from __future__ import annotations

import pytest

from interpreter import hubspot

_CREDS = {"access_token": "tok", "portal_id": "999"}

_PIPELINE = {"results": [{
    "id": "0", "label": "Support Pipeline",
    "stages": [
        {"id": "1", "label": "New"},
        {"id": "2", "label": "Waiting on contact"},
        {"id": "3", "label": "Waiting on us"},
        {"id": "4", "label": "Closed"},
    ],
}]}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(hubspot, "_creds", lambda tenant_id, sb=None: _CREDS)


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

    def fake_request(method, url, *, headers=None, json=None, params=None, timeout=None):
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
    assert hubspot.available(None) is False
    assert hubspot.available("t") is False


def test_update_case_fields_dry_runs_without_creds():
    out = hubspot.update_case_fields("1", {"Status": "Escalated"})
    assert out["dry_run"] is True and out["planned"] == {"Status": "Escalated"}


def test_post_note_dry_runs_without_creds():
    assert hubspot.post_note("1", "hi") == {"posted": False, "dry_run": True, "mention_id": None}


def test_add_case_comment_dry_runs_without_creds():
    assert hubspot.add_case_comment("1", "hi") == {"created": False, "dry_run": True, "id": None}


def test_assign_case_dry_runs_without_creds():
    out = hubspot.assign_case("1", queue="Support")
    assert out == {"assigned": False, "dry_run": True, "queue": "Support", "user_id": None}


def test_assign_case_no_target_is_a_clean_noop():
    assert hubspot.assign_case("1") == {"assigned": False, "reason": "no queue or user configured"}


def test_ensure_case_dry_runs_without_creds():
    out = hubspot.ensure_case({"from": "a@b.com"})
    assert out["dry_run"] is True and out["reason"] == "hubspot not configured"


def test_log_email_message_dry_runs_without_creds():
    out = hubspot.log_email_message("1", incoming=True)
    assert out["created"] is False and out["dry_run"] is True


def test_identify_sender_dry_runs_without_creds():
    out = hubspot.identify_sender("a@b.com")
    assert out["match"] == "none" and out["reason"] == "hubspot not configured"


def test_send_case_reply_dry_runs_without_creds():
    out = hubspot.send_case_reply("1", "hi")
    assert out == {"sent": False, "dry_run": True, "via": "dry_run", "to": None}


# ── configured: real HTTP calls, mocked ──────────────────────────────────
def test_update_case_fields_maps_status_via_pipeline_stage_label(configured, monkeypatch):
    calls = _record(monkeypatch, [
        ("GET", "/crm/v3/pipelines/tickets", _PIPELINE),
        ("PATCH", "/crm/v3/objects/tickets/1", {}),
    ])
    out = hubspot.update_case_fields(
        "1", {"Status": "Escalated", "Routed_Team__c": "csm", "AI_Confidence__c": 0.9})
    assert out["dry_run"] is False
    assert out["written"] == {"Status": "Escalated"}
    assert out["skipped"] == {"Routed_Team__c": "csm", "AI_Confidence__c": 0.9}
    patch_call = next(c for c in calls if c[0] == "PATCH")
    assert patch_call[2] == {"properties": {"hs_pipeline_stage": "3"}}


def test_update_case_fields_unmapped_status_is_skipped_not_raised(configured, monkeypatch):
    _record(monkeypatch, [("GET", "/crm/v3/pipelines/tickets", _PIPELINE)])
    out = hubspot.update_case_fields("1", {"Status": "totally-unknown-status"})
    assert out["skipped"] == {"Status": "totally-unknown-status"}
    assert out["written"] == {}


def test_update_case_fields_append_becomes_an_internal_note(configured, monkeypatch):
    calls = _record(monkeypatch, [
        ("POST", "/crm/v3/objects/notes", {"id": "n1"}),
        ("PUT", "/associations/default/", {}),
    ])
    out = hubspot.update_case_fields("1", {}, append={"Description": "note text"})
    note_call = next(c for c in calls if c[0] == "POST")
    assert note_call[2]["properties"]["hs_note_body"] == "note text"
    assert out["written"]["_append_as_note"] == ["note text"]


def test_post_note_includes_mention_as_cc_text(configured, monkeypatch):
    calls = _record(monkeypatch, [
        ("POST", "/crm/v3/objects/notes", {"id": "n1"}),
        ("PUT", "/associations/default/", {}),
    ])
    out = hubspot.post_note("1", "please review", mention_id="owner42")
    assert out == {"posted": True, "dry_run": False, "mention_id": "owner42"}
    note_call = next(c for c in calls if c[0] == "POST")
    assert "cc: owner42" in note_call[2]["properties"]["hs_note_body"]


def test_add_case_comment_creates_a_note_regardless_of_published(configured, monkeypatch):
    calls = _record(monkeypatch, [
        ("POST", "/crm/v3/objects/notes", {"id": "n7"}),
        ("PUT", "/associations/default/", {}),
    ])
    out = hubspot.add_case_comment("1", "customer-visible?", published=True)
    assert out == {"created": True, "dry_run": False, "id": "n7"}
    assert any(c[0] == "PUT" and "/notes/n7/associations/default/tickets/1" in c[1] for c in calls)


def test_assign_case_by_user_id_sets_owner_directly(configured, monkeypatch):
    calls = _record(monkeypatch, [("PATCH", "/crm/v3/objects/tickets/1", {})])
    out = hubspot.assign_case("1", user_id="55")
    assert out == {"assigned": True, "dry_run": False, "owner_id": "55", "owner_type": "user"}
    assert calls[0][2] == {"properties": {"hubspot_owner_id": "55"}}


def test_assign_case_by_queue_resolves_a_matching_owner(configured, monkeypatch):
    owners = {"results": [{"id": "9", "email": "support-team@acme.com", "firstName": "S", "lastName": "T"}]}
    calls = _record(monkeypatch, [
        ("GET", "/crm/v3/owners", owners),
        ("PATCH", "/crm/v3/objects/tickets/1", {}),
    ])
    out = hubspot.assign_case("1", queue="support-team")
    assert out == {"assigned": True, "dry_run": False, "owner_id": "9", "owner_type": "queue"}
    patch_call = next(c for c in calls if c[0] == "PATCH")
    assert patch_call[2] == {"properties": {"hubspot_owner_id": "9"}}


def test_assign_case_by_queue_not_found(configured, monkeypatch):
    _record(monkeypatch, [("GET", "/crm/v3/owners", {"results": []})])
    out = hubspot.assign_case("1", queue="nope")
    assert out["assigned"] is False
    assert "no queue object" in out["reason"]


def test_log_email_message_creates_a_real_email_engagement(configured, monkeypatch):
    calls = _record(monkeypatch, [
        ("POST", "/crm/v3/objects/emails", {"id": "e1"}),
        ("PUT", "/associations/default/", {}),
    ])
    out = hubspot.log_email_message("1", incoming=True, from_addr="a@b.com", subject="hi", body="hello")
    assert out == {"created": True, "dry_run": False, "id": "e1"}
    post_call = next(c for c in calls if c[0] == "POST")
    assert post_call[2]["properties"]["hs_email_direction"] == "INCOMING_EMAIL"


def test_identify_sender_matches_existing_contact(configured, monkeypatch):
    contact = {"results": [{"id": "c1", "properties": {"firstname": "A", "lastname": "B"}}]}
    calls = _record(monkeypatch, [
        ("POST", "/crm/v3/objects/contacts/search", contact),
        ("GET", "/associations/companies", {"results": []}),
    ])
    out = hubspot.identify_sender("a@b.com")
    assert out["known"] is True and out["match"] == "contact" and out["contact_id"] == "c1"
    assert out["name"] == "A B"


def test_identify_sender_no_match_and_no_create_lead(configured, monkeypatch):
    _record(monkeypatch, [
        ("POST", "/crm/v3/objects/contacts/search", {"results": []}),
        ("POST", "/crm/v3/objects/companies/search", {"results": []}),
    ])
    out = hubspot.identify_sender("nobody@nowhere.com")
    assert out["match"] == "none" and out["contact_id"] is None


def test_send_case_reply_without_thread_falls_back_to_a_note(configured, monkeypatch):
    calls = _record(monkeypatch, [
        ("POST", "/crm/v3/objects/notes", {"id": "n1"}),
        ("PUT", "/associations/default/", {}),
    ])
    out = hubspot.send_case_reply("1", "here is your answer", to_email="a@b.com")
    assert out["sent"] is False
    assert out["via"] == "note_fallback"
    assert "no native" in out["reason"]
    assert any(c[0] == "POST" and "/notes" in c[1] for c in calls)


def test_send_case_reply_with_thread_id_uses_conversations_api(configured, monkeypatch):
    calls = _record(monkeypatch, [
        ("POST", "/conversations/v3/conversations/threads/t1/messages", {}),
    ])
    out = hubspot.send_case_reply("1", "hi", to_email="a@b.com", thread_id="t1")
    assert out == {"sent": True, "dry_run": False, "via": "conversations_thread", "to": "a@b.com"}
    assert calls[0][0] == "POST" and "threads/t1/messages" in calls[0][1]


# ── ingestion (ticket watcher) ────────────────────────────────────────────
def test_list_new_tickets_returns_empty_without_creds():
    assert hubspot.list_new_tickets(None) == []


def test_list_new_tickets_filters_by_new_stage(configured, monkeypatch):
    tickets = {"results": [{"id": "5", "properties": {"subject": "help", "content": "hi"}}]}
    calls = _record(monkeypatch, [
        ("GET", "/crm/v3/pipelines/tickets", _PIPELINE),
        ("POST", "/crm/v3/objects/tickets/search", tickets),
    ])
    out = hubspot.list_new_tickets("t")
    assert out == tickets["results"]
    search_call = next(c for c in calls if c[0] == "POST")
    stage_filter = search_call[2]["filterGroups"][0]["filters"][0]
    assert stage_filter == {"propertyName": "hs_pipeline_stage", "operator": "EQ", "value": "1"}


def test_ticket_as_case_normalises_and_resolves_contact(configured, monkeypatch):
    ticket = {"id": "5", "properties": {"subject": "help", "content": "hi", "hs_pipeline_stage": "1"}}
    calls = _record(monkeypatch, [
        ("GET", "/associations/contacts", {"results": [{"toObjectId": "9"}]}),
        ("GET", "/crm/v3/objects/contacts/9",
         {"properties": {"email": "a@b.com", "firstname": "A", "lastname": "B"}}),
    ])
    case = hubspot.ticket_as_case(ticket, "t")
    assert case["sf_id"] == "5" and case["channel"] == "hubspot"
    assert case["from"] == "a@b.com" and case["from_name"] == "A B"
    assert any("/tickets/5/associations/contacts" in c[1] for c in calls)


# ── org_metadata (flow editor live pickers) ───────────────────────────────
def test_org_metadata_unavailable_without_creds():
    out = hubspot.org_metadata(None)
    assert out == {"available": False, "queues": [], "case_types": [], "modules": [],
                   "case_fields": [], "users": []}


def test_org_metadata_maps_owners_and_ticket_properties(configured, monkeypatch):
    owners = {"results": [{"id": "9", "email": "a@b.com", "firstName": "A", "lastName": "B"}]}
    props = {"results": [
        {"name": "priority", "label": "Priority", "type": "enumeration", "hubspotDefined": True,
         "options": [{"value": "high", "label": "High"}]},
        {"name": "custom_field", "label": "Custom", "type": "string", "hubspotDefined": False, "options": []},
    ]}
    _record(monkeypatch, [
        ("GET", "/crm/v3/owners", owners),
        ("GET", "/crm/v3/properties/tickets", props),
    ])
    out = hubspot.org_metadata("t")
    assert out["available"] is True
    assert out["users"] == [{"id": "9", "name": "A B", "email": "a@b.com"}]
    assert out["queues"] == [{"id": "a@b.com", "name": "A B", "developer_name": "a@b.com"}]
    assert out["case_types"] == [] and out["modules"] == []
    assert out["case_fields"][0] == {"name": "priority", "label": "Priority", "type": "enumeration",
                                     "custom": False, "picklist_values": [{"value": "high", "label": "High"}]}
    assert out["case_fields"][1]["custom"] is True and out["case_fields"][1]["picklist_values"] == []


def test_org_metadata_degrades_per_call_not_all_or_nothing(configured, monkeypatch):
    """A real live-testing finding: a Private App missing only
    `crm.objects.owners.read` must not blank out the ticket-properties
    picker too — that's a completely different scope and works fine."""
    props = {"results": [{"name": "priority", "label": "Priority", "type": "string",
                          "hubspotDefined": True, "options": []}]}

    def fake_request(method, url, *, headers=None, json=None, params=None, timeout=None):
        if "/crm/v3/owners" in url:
            resp = type("R", (), {"status_code": 403, "content": b"1",
                                  "json": lambda self: {"message": "missing scope"}})()

            def raise_for_status(self=resp):
                raise RuntimeError("403 missing scope")
            resp.raise_for_status = raise_for_status
            return resp
        return type("R", (), {"status_code": 200, "content": b"1",
                              "raise_for_status": lambda self: None,
                              "json": lambda self: props})()

    import requests
    monkeypatch.setattr(requests, "request", fake_request)

    out = hubspot.org_metadata("t")
    assert out["available"] is True          # still usable, not blanked
    assert out["users"] == [] and out["queues"] == []   # only the owners-dependent part is empty
    assert len(out["case_fields"]) == 1      # properties still came through
    assert "owners" in out["error"]


# ── webhook signature (v3) ────────────────────────────────────────────────
def _reference_signature(secret: str, method: str, url: str, body: bytes, ts: int) -> str:
    """A from-scratch re-implementation (not calling the code under test),
    matching @hubspot/api-client's Signature.getSignature('v3') exactly —
    the algorithm this test suite confirmed by reading HubSpot's own SDK
    source, not guessed."""
    import base64
    import hashlib
    import hmac as _hmac

    source = f"{method}{url}{body.decode()}{ts}"
    mac = _hmac.new(secret.encode(), source.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def test_verify_webhook_signature_accepts_a_correctly_signed_request():
    import time
    ts = int(time.time() * 1000)
    body = b'[{"eventId":1,"subscriptionType":"ticket.creation","objectId":42}]'
    sig = _reference_signature("shh", "POST", "https://example.com/webhooks/hubspot/t1", body, ts)
    assert hubspot.verify_webhook_signature(
        "shh", method="POST", url="https://example.com/webhooks/hubspot/t1",
        body=body, signature_b64=sig, timestamp_ms=str(ts)) is True


def test_verify_webhook_signature_rejects_a_tampered_body():
    import time
    ts = int(time.time() * 1000)
    body = b'[{"eventId":1}]'
    sig = _reference_signature("shh", "POST", "https://example.com/webhooks/hubspot/t1", body, ts)
    tampered = b'[{"eventId":2}]'
    assert hubspot.verify_webhook_signature(
        "shh", method="POST", url="https://example.com/webhooks/hubspot/t1",
        body=tampered, signature_b64=sig, timestamp_ms=str(ts)) is False


def test_verify_webhook_signature_rejects_wrong_secret():
    import time
    ts = int(time.time() * 1000)
    body = b'[{"eventId":1}]'
    sig = _reference_signature("shh", "POST", "https://example.com/webhooks/hubspot/t1", body, ts)
    assert hubspot.verify_webhook_signature(
        "different-secret", method="POST", url="https://example.com/webhooks/hubspot/t1",
        body=body, signature_b64=sig, timestamp_ms=str(ts)) is False


def test_verify_webhook_signature_rejects_a_stale_timestamp():
    body = b'[{"eventId":1}]'
    stale_ts = 1000  # way in the past
    sig = _reference_signature("shh", "POST", "https://example.com/webhooks/hubspot/t1", body, stale_ts)
    assert hubspot.verify_webhook_signature(
        "shh", method="POST", url="https://example.com/webhooks/hubspot/t1",
        body=body, signature_b64=sig, timestamp_ms=str(stale_ts)) is False


def test_verify_webhook_signature_rejects_missing_pieces():
    body = b"[]"
    assert hubspot.verify_webhook_signature(
        "", method="POST", url="https://x", body=body, signature_b64="sig", timestamp_ms="1") is False
    assert hubspot.verify_webhook_signature(
        "shh", method="POST", url="https://x", body=body, signature_b64=None, timestamp_ms="1") is False
    assert hubspot.verify_webhook_signature(
        "shh", method="POST", url="https://x", body=body, signature_b64="sig", timestamp_ms=None) is False
    assert hubspot.verify_webhook_signature(
        "shh", method="POST", url="https://x", body=body, signature_b64="sig", timestamp_ms="not-a-number") is False


# ── connect-account round trip: two independently-updatable secrets ───────
def test_save_channel_merges_secrets_not_clobbers(monkeypatch):
    """A real risk this guards against: vault_secrets.put() REPLACES the
    whole blob, so saving the webhook secret alone must not silently erase
    the already-stored access_token (or vice versa)."""
    store = {}

    class _SB:
        def table(self, name):
            return self

        def upsert(self, *a, **k):
            return self

        def on_conflict(self, *a, **k):
            return self

        def execute(self):
            return type("R", (), {"data": []})()

    import interpreter.vault_secrets as vs
    monkeypatch.setattr(vs, "get", lambda tid, kind, sb=None: dict(store))
    monkeypatch.setattr(vs, "put", lambda tid, kind, creds, sb=None: store.update(creds))

    cfg = hubspot.HubSpotConfig(tenant_id="t", portal_id="1")
    hubspot.save_channel(cfg, _SB(), access_token="tok-1")
    assert store == {"access_token": "tok-1"}

    hubspot.save_channel(cfg, _SB(), webhook_client_secret="whs-1")
    assert store == {"access_token": "tok-1", "webhook_client_secret": "whs-1"}
