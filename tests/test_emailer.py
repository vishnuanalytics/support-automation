"""Phase 20c -- offline tests for the outbound email path + the hard guard."""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

load_dotenv()

from api import worker
from interpreter import emailer, mailbox
from interpreter.emailer import decide, send_reply
from interpreter.mailbox import MailboxConfig

GLOBEX_TENANT = "22222222-2222-2222-2222-222222222222"


def _cfg(auto_send=True, provider="imap", **kw):
    base = dict(tenant_id="t", provider=provider, team="support",
                username="support@acme.com", from_addr="support@acme.com",
                from_name="Acme Support", smtp_host="smtp.acme.com",
                auto_send_enabled=auto_send)
    base.update(kw)
    return MailboxConfig(**base)


# ── decide() -- the hard guard ──────────────────────────────────────
def test_decide_auto_reply_sends_only_when_switch_on_and_draft_present():
    on = _cfg(auto_send=True)
    assert decide({"action": "auto_reply", "reply": "here you go"}, on, None) == \
        ("send_reply", {"body": "here you go"})
    # switch off -> hand to a human, never send
    k, m = decide({"action": "auto_reply", "reply": "x"}, _cfg(auto_send=False), None)
    assert k == "needs_human" and "auto-send is off" in m["reason"]
    # empty draft -> human
    k, m = decide({"action": "auto_reply", "reply": "   "}, on, None)
    assert k == "needs_human" and "empty draft" in m["reason"]


def test_decide_escalations_never_send():
    for action in ("ask_human", "handover"):
        assert decide({"action": action}, _cfg(), None)[0] == "needs_human"


def test_decide_need_info_requires_switch_and_node_opt_in():
    o = {"action": "need_info", "questions": ["what plan?", "what error?"]}
    assert decide(o, _cfg(auto_send=True), {"auto_send": True})[0] == "send_questions"
    assert decide(o, _cfg(auto_send=True), {"auto_send": False})[0] == "needs_human"
    assert decide(o, _cfg(auto_send=False), {"auto_send": True})[0] == "needs_human"
    assert decide({"action": "need_info", "questions": []},
                  _cfg(auto_send=True), {"auto_send": True})[0] == "needs_human"


def test_decide_unknown_action_is_noop():
    assert decide({"action": "task_dispatched"}, _cfg(), None)[0] == "noop"
    assert decide({}, _cfg(), None)[0] == "noop"


# ── send_reply ─────────────────────────────────────────────────────
def test_send_reply_dry_run_without_creds_does_not_raise():
    cfg = _cfg(smtp_host="", secret={})            # no creds
    r = send_reply(cfg, to="jane@x.com", subject="Broken export",
                   body="Try step 3.", in_reply_to="<m1@x.com>")
    assert r["sent"] is False and r["dry_run"] is True
    assert r["to"] == "jane@x.com" and r["message_id"]


def test_send_reply_needs_a_recipient_and_a_body():
    cfg = _cfg()
    assert send_reply(cfg, to="", subject="x", body="hi")["dry_run"] is True
    assert send_reply(cfg, to="a@b.com", subject="x", body="")["dry_run"] is True


def test_subject_and_questions_helpers():
    assert emailer._subject_reply("Broken") == "Re: Broken"
    assert emailer._subject_reply("re: Broken") == "re: Broken"
    body = emailer._questions_body(["Which plan?", "Exact error?"])
    assert "1. Which plan?" in body and "2. Exact error?" in body


def test_send_reply_builds_a_threaded_bot_stamped_message(monkeypatch):
    sent = {}
    monkeypatch.setattr(emailer, "_send_smtp", lambda cfg, msg: sent.update(msg=msg))
    cfg = _cfg(secret={"password": "pw"})
    r = send_reply(cfg, to="jane@x.com", subject="Broken", body="fixed",
                   in_reply_to="<m1@x.com>", references=["<root@x.com>"])
    assert r["sent"] is True
    msg = sent["msg"]
    assert msg["X-Support-Bot"] == "1"
    assert msg["In-Reply-To"] == "<m1@x.com>"
    assert "<root@x.com>" in msg["References"] and "<m1@x.com>" in msg["References"]
    assert msg["From"] == "Acme Support <support@acme.com>"
    assert msg["Subject"] == "Re: Broken"


# ── worker._email_post_run -- the guard applied ────────────────────
@pytest.fixture
def patched_channel(monkeypatch):
    cfg = _cfg(auto_send=True, secret={"password": "pw"})
    monkeypatch.setattr(mailbox, "load_channel", lambda tid, sb: cfg)
    calls = {"sent": [], "flagged": []}
    monkeypatch.setattr(emailer, "send_reply",
                        lambda cfg, **kw: (calls["sent"].append(kw) or
                                           {"sent": True, "dry_run": False, "to": kw["to"]}))
    monkeypatch.setattr(mailbox, "mark_needs_human",
                        lambda cfg, mid: calls["flagged"].append(mid))
    return calls


_CASE = {"channel": "email", "from": "jane@x.com", "subject": "Export",
         "message_id": "<m1@x.com>", "references": []}
_FLOW = {"tenant_id": "t", "team": "support"}


def test_post_run_auto_reply_sends(patched_channel):
    final = {"outcome": {"action": "auto_reply", "reply": "Here's the fix."}}
    res = worker._email_post_run(final, _CASE, _FLOW, sb=object())
    assert res["decision"] == "send_reply" and res["delivery"]["sent"] is True
    assert patched_channel["sent"][0]["body"] == "Here's the fix."
    assert patched_channel["flagged"] == []


def test_post_run_ask_human_flags_and_sends_nothing(patched_channel):
    final = {"outcome": {"action": "ask_human"}}
    res = worker._email_post_run(final, _CASE, _FLOW, sb=object())
    assert res["decision"] == "needs_human"
    assert patched_channel["sent"] == [] and patched_channel["flagged"] == ["<m1@x.com>"]


def test_post_run_need_info_opted_in_sends_questions(patched_channel):
    final = {"outcome": {"action": "need_info", "questions": ["Which plan?"]},
             "clarification": {"auto_send": True}}
    res = worker._email_post_run(final, _CASE, _FLOW, sb=object())
    assert res["decision"] == "send_questions"
    assert "Which plan?" in patched_channel["sent"][0]["body"]


def test_post_run_no_channel_is_a_clean_skip(monkeypatch):
    monkeypatch.setattr(mailbox, "load_channel", lambda tid, sb: None)
    res = worker._email_post_run({"outcome": {"action": "auto_reply"}}, _CASE, _FLOW, sb=object())
    assert res == {"skipped": "no email channel"}


class _NoDup:
    """Minimal sb stub: the dedupe lookup in _run_flow finds nothing."""
    def table(self, *a): return self
    def select(self, *a): return self
    def eq(self, *a, **k): return self
    def execute(self): return type("R", (), {"data": []})()


def test_run_flow_records_and_posts_the_case_mutated_in_flight(monkeypatch):
    # finding #3: sf_case adds sf_id / refreshes tier during the run;
    # _run_flow must persist and act on that, not the pre-run input.
    flow = {"flow_id": "f1", "tenant_id": "t", "team": "email", "flow_version": 1}
    in_case = dict(_CASE)
    final = {"outcome": {"action": "handover"},
             "case": {**in_case, "sf_id": "500ABC",
                      "account": {"customer_type": "basic"}}}

    monkeypatch.setattr(worker, "load_flow", lambda **kw: flow)
    monkeypatch.setattr(worker, "build_graph",
                        lambda flow: type("G", (), {"invoke": lambda self, s: final})())
    seen = {}
    monkeypatch.setattr(worker, "record_run",
                        lambda flow, final, *, case, source, sb, idempotency_key=None:
                        (seen.update(recorded=case) or "run-1"))
    monkeypatch.setattr(worker, "_email_post_run",
                        lambda final, case, flow, sb: seen.update(posted=case) or {"ok": True})

    out = worker._run_flow({"flow_id": "f1", "case": dict(in_case),
                            "idempotency_key": "<m1@x.com>"}, sb=_NoDup())
    assert out["run_id"] == "run-1"
    assert seen["recorded"]["sf_id"] == "500ABC"
    assert seen["posted"]["sf_id"] == "500ABC"


# ── integration: real channel loaded from Vault, no reachable server ──
@pytest.mark.integration
def test_post_run_against_a_real_channel_dry_runs_the_send():
    if not os.environ.get("SUPABASE_SERVICE_KEY"):
        pytest.skip("no SUPABASE_SERVICE_KEY")
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    cfg = MailboxConfig(tenant_id=GLOBEX_TENANT, provider="imap", team="support",
                        username="ch@example.test", from_addr="ch@example.test",
                        auto_send_enabled=True, status="inactive")   # no smtp_host
    mailbox.save_channel(GLOBEX_TENANT, sb, cfg,
                         plaintext_secret='{"kind":"imap","password":"x"}')
    try:
        flow = {"tenant_id": GLOBEX_TENANT, "team": "support"}
        res = worker._email_post_run(
            {"outcome": {"action": "auto_reply", "reply": "the fix"}},
            _CASE, flow, sb=sb)
        assert res["decision"] == "send_reply"
        assert res["delivery"]["dry_run"] is True and res["delivery"]["sent"] is False
    finally:
        mailbox.delete_channel(GLOBEX_TENANT, sb)
