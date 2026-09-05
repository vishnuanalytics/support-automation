"""Multi-provider connectors, step 3 -- offline tests for
`api.worker._freshchat_post_run` (mirrors test_emailer.py's coverage of
`_email_post_run`; reuses the same `emailer.decide`, so the decision matrix
itself isn't re-tested here, only the Freshchat-specific delivery + the
channel_threads bookkeeping)."""

from __future__ import annotations

import pytest
from dotenv import load_dotenv

load_dotenv()

from api import worker
from interpreter import channel_threads, freshchat

_CASE = {"channel": "freshchat", "conversation_id": "c1", "sf_id": "500CASE",
        "case_number": "00001", "subject": "help"}
_FLOW = {"tenant_id": "t", "team": "support"}
_SB = object()


@pytest.fixture
def patched_channel(monkeypatch):
    cfg = freshchat.FreshchatConfig(tenant_id="t", domain="acme.freshchat.com",
                                    auto_send_enabled=True, api_token="tok",
                                    webhook_public_key="pem")
    monkeypatch.setattr(freshchat, "load_channel", lambda tid, sb: cfg)
    calls = {"sent": [], "linked": []}
    monkeypatch.setattr(freshchat, "send_message",
                        lambda cfg, conv_id, text: (calls["sent"].append((conv_id, text)) or
                                                    {"sent": True, "dry_run": False}))
    monkeypatch.setattr(channel_threads, "link",
                        lambda tid, ch, tk, **kw: calls["linked"].append((tid, ch, tk, kw)))
    return calls


def test_post_run_auto_reply_sends_and_links_the_thread(patched_channel):
    final = {"outcome": {"action": "auto_reply", "reply": "Here's the fix."}}
    res = worker._freshchat_post_run(final, _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "send_reply" and res["delivery"]["sent"] is True
    assert patched_channel["sent"] == [("c1", "Here's the fix.")]
    assert patched_channel["linked"] == [("t", "freshchat", "c1",
                                         {"case_ref": "500CASE", "case_number": "00001", "sb": _SB})]


def test_post_run_ask_human_sends_nothing_but_still_links(patched_channel):
    final = {"outcome": {"action": "ask_human"}}
    res = worker._freshchat_post_run(final, _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "needs_human"
    assert patched_channel["sent"] == []
    assert patched_channel["linked"], "the thread mapping must stay fresh even without a send"


def test_post_run_need_info_opted_in_sends_questions(patched_channel):
    final = {"outcome": {"action": "need_info", "questions": ["Which plan?"]},
             "clarification": {"auto_send": True}}
    res = worker._freshchat_post_run(final, _CASE, _FLOW, sb=_SB)
    assert res["decision"] == "send_questions"
    assert "Which plan?" in patched_channel["sent"][0][1]


def test_post_run_no_channel_is_a_clean_skip(monkeypatch):
    monkeypatch.setattr(freshchat, "load_channel", lambda tid, sb: None)
    res = worker._freshchat_post_run({"outcome": {"action": "auto_reply"}}, _CASE, _FLOW, sb=_SB)
    assert res == {"skipped": "no freshchat channel"}


def test_post_run_no_conversation_id_is_a_clean_noop(patched_channel):
    case = {**_CASE, "conversation_id": None, "sf_id": None}
    res = worker._freshchat_post_run({"outcome": {"action": "auto_reply"}}, case, _FLOW, sb=_SB)
    assert res == {"decision": "noop", "reason": "no conversation_id on case"}
    assert patched_channel["sent"] == [] and patched_channel["linked"] == []


def test_post_run_never_raises_on_a_delivery_error(patched_channel, monkeypatch):
    def boom(cfg, conv_id, text):
        raise RuntimeError("network blew up")
    monkeypatch.setattr(freshchat, "send_message", boom)
    final = {"outcome": {"action": "auto_reply", "reply": "hi"}}
    res = worker._freshchat_post_run(final, _CASE, _FLOW, sb=_SB)
    assert "error" in res
