"""Offline tests for `api.worker._hubspot_post_run` (mirrors
test_zendesk_worker.py; the emailer.decide matrix itself is covered in
test_emailer.py, here only the HubSpot-specific delivery)."""

from __future__ import annotations

import pytest
from dotenv import load_dotenv

load_dotenv()

from api import worker
from interpreter import hubspot

_CASE = {"channel": "hubspot", "sf_id": "42", "id": "42", "from": "dana@acme.com",
         "subject": "help"}
_FLOW = {"tenant_id": "t", "team": "support"}
_SB = object()


@pytest.fixture
def patched(monkeypatch):
    cfg = hubspot.HubSpotConfig(tenant_id="t", portal_id="999",
                                access_token="tok", auto_send_enabled=True)
    monkeypatch.setattr(hubspot, "load_channel", lambda tid, sb: cfg)
    sent = []
    monkeypatch.setattr(hubspot, "send_case_reply",
                        lambda cid, body, **kw: sent.append((cid, body, kw)) or
                        {"sent": False, "dry_run": False, "via": "note_fallback"})
    return sent


def test_auto_reply_calls_send_case_reply(patched):
    final = {"outcome": {"action": "auto_reply", "reply": "Here's the fix."}}
    res = worker._hubspot_post_run(final, _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "send_reply"
    cid, body, kw = patched[0]
    assert cid == "42" and body == "Here's the fix."
    assert kw["to_email"] == "dana@acme.com" and kw["tenant_id"] == "t"


def test_auto_reply_is_flagged_when_auto_send_is_off(monkeypatch):
    cfg = hubspot.HubSpotConfig(tenant_id="t", portal_id="999",
                                access_token="tok", auto_send_enabled=False)
    monkeypatch.setattr(hubspot, "load_channel", lambda tid, sb: cfg)
    sent = []
    monkeypatch.setattr(hubspot, "send_case_reply", lambda *a, **k: sent.append(1))
    res = worker._hubspot_post_run({"outcome": {"action": "auto_reply", "reply": "x"}},
                                   _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "needs_human" and sent == []


def test_ask_human_sends_nothing(patched):
    res = worker._hubspot_post_run({"outcome": {"action": "ask_human"}}, _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "needs_human" and patched == []


def test_need_info_opted_in_sends_questions(patched):
    final = {"outcome": {"action": "need_info", "questions": ["Which plan?"]},
             "clarification": {"auto_send": True}}
    res = worker._hubspot_post_run(final, _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "send_questions"
    assert "Which plan?" in patched[0][1]


def test_no_connection_is_a_clean_skip(monkeypatch):
    monkeypatch.setattr(hubspot, "load_channel", lambda tid, sb: None)
    res = worker._hubspot_post_run({"outcome": {"action": "auto_reply"}}, _CASE, _FLOW, sb=_SB)
    assert res == {"skipped": "no hubspot connection"}


def test_no_ticket_id_is_a_noop(patched):
    case = {**_CASE, "sf_id": None, "id": None}
    res = worker._hubspot_post_run({"outcome": {"action": "auto_reply", "reply": "x"}},
                                   case, _FLOW, sb=_SB)
    assert res["decision"] == "noop" and patched == []


def test_delivery_error_is_swallowed(monkeypatch):
    cfg = hubspot.HubSpotConfig(tenant_id="t", portal_id="999",
                                access_token="tok", auto_send_enabled=True)
    monkeypatch.setattr(hubspot, "load_channel", lambda tid, sb: cfg)

    def boom(*a, **k):
        raise RuntimeError("hubspot 500")

    monkeypatch.setattr(hubspot, "send_case_reply", boom)
    res = worker._hubspot_post_run({"outcome": {"action": "auto_reply", "reply": "x"}},
                                   _CASE, _FLOW, sb=_SB)
    assert "error" in res


# ── config round-trip ──────────────────────────────────────────────
def test_hubspot_config_auto_send_round_trips():
    cfg = hubspot.HubSpotConfig.from_row(
        "t", {"portal_id": "999", "auto_send_enabled": True}, "active", {"access_token": "tok"})
    assert cfg.auto_send_enabled is True
    assert cfg.to_config()["auto_send_enabled"] is True
    assert cfg.public_status()["auto_send_enabled"] is True
    d = hubspot.HubSpotConfig.from_row("t", {}, "inactive", {})
    assert d.auto_send_enabled is False
    assert "tok" not in repr(cfg)
