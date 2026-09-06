"""Phase 31 chunk 2 — offline tests for `api.worker._zendesk_post_run`
(mirrors test_freshchat_worker.py; the emailer.decide matrix itself is
covered in test_emailer.py, here only the Zendesk-specific delivery)."""

from __future__ import annotations

import pytest
from dotenv import load_dotenv

load_dotenv()

from api import worker
from interpreter import zendesk

_CASE = {"channel": "zendesk", "sf_id": "42", "id": "42", "from": "dana@acme.com",
         "subject": "help"}
_FLOW = {"tenant_id": "t", "team": "support"}
_SB = object()


@pytest.fixture
def patched(monkeypatch):
    cfg = zendesk.ZendeskConfig(tenant_id="t", subdomain="acme", email="bot@acme.com",
                                api_token="tok", auto_send_enabled=True)
    monkeypatch.setattr(zendesk, "load_channel", lambda tid, sb: cfg)
    sent = []
    monkeypatch.setattr(zendesk, "send_case_reply",
                        lambda cid, body, **kw: sent.append((cid, body, kw)) or
                        {"sent": True, "dry_run": False, "via": "ticket_comment"})
    return sent


def test_auto_reply_sends_a_public_comment(patched):
    final = {"outcome": {"action": "auto_reply", "reply": "Here's the fix."}}
    res = worker._zendesk_post_run(final, _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "send_reply" and res["delivery"]["sent"] is True
    cid, body, kw = patched[0]
    assert cid == "42" and body == "Here's the fix."
    assert kw["to_email"] == "dana@acme.com" and kw["tenant_id"] == "t"


def test_auto_reply_is_flagged_when_auto_send_is_off(monkeypatch):
    cfg = zendesk.ZendeskConfig(tenant_id="t", subdomain="acme", email="b@a.com",
                                api_token="tok", auto_send_enabled=False)
    monkeypatch.setattr(zendesk, "load_channel", lambda tid, sb: cfg)
    sent = []
    monkeypatch.setattr(zendesk, "send_case_reply", lambda *a, **k: sent.append(1))
    res = worker._zendesk_post_run({"outcome": {"action": "auto_reply", "reply": "x"}},
                                   _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "needs_human" and sent == []


def test_ask_human_sends_nothing(patched):
    res = worker._zendesk_post_run({"outcome": {"action": "ask_human"}}, _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "needs_human" and patched == []


def test_need_info_opted_in_sends_questions(patched):
    final = {"outcome": {"action": "need_info", "questions": ["Which plan?"]},
             "clarification": {"auto_send": True}}
    res = worker._zendesk_post_run(final, _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "send_questions"
    assert "Which plan?" in patched[0][1]


def test_no_connection_is_a_clean_skip(monkeypatch):
    monkeypatch.setattr(zendesk, "load_channel", lambda tid, sb: None)
    res = worker._zendesk_post_run({"outcome": {"action": "auto_reply"}}, _CASE, _FLOW, sb=_SB)
    assert res == {"skipped": "no zendesk connection"}


def test_no_ticket_id_is_a_noop(patched):
    case = {**_CASE, "sf_id": None, "id": None}
    res = worker._zendesk_post_run({"outcome": {"action": "auto_reply", "reply": "x"}},
                                   case, _FLOW, sb=_SB)
    assert res["decision"] == "noop" and patched == []


def test_delivery_error_is_swallowed(monkeypatch):
    cfg = zendesk.ZendeskConfig(tenant_id="t", subdomain="a", email="b@a.com",
                                api_token="t", auto_send_enabled=True)
    monkeypatch.setattr(zendesk, "load_channel", lambda tid, sb: cfg)

    def boom(*a, **k):
        raise RuntimeError("zendesk 500")

    monkeypatch.setattr(zendesk, "send_case_reply", boom)
    res = worker._zendesk_post_run({"outcome": {"action": "auto_reply", "reply": "x"}},
                                   _CASE, _FLOW, sb=_SB)
    assert "error" in res


# ── config round-trip ──────────────────────────────────────────────
def test_zendesk_config_auto_send_round_trips():
    cfg = zendesk.ZendeskConfig.from_row(
        "t", {"subdomain": "acme", "email": "b@a.com", "auto_send_enabled": True},
        "active", {"api_token": "tok"})
    assert cfg.auto_send_enabled is True
    assert cfg.to_config()["auto_send_enabled"] is True
    assert cfg.public_status()["auto_send_enabled"] is True
    # default off, and "tok" never leaks into repr
    d = zendesk.ZendeskConfig.from_row("t", {}, "inactive", {})
    assert d.auto_send_enabled is False
    assert "tok" not in repr(cfg)
