"""Phase 20b -- offline tests for the inbound email poller.

The IMAP/Gmail fetch is monkeypatched; what's under test is the driver
logic: which messages get enqueued, with what keys, and what gets marked
processed / what status is written."""

from __future__ import annotations

import os
from email.message import EmailMessage

import pytest
from dotenv import load_dotenv

load_dotenv()

from ingestion import email_watch
from interpreter import mailbox
from interpreter.mailbox import MailboxConfig, should_process, thread_key

GLOBEX_TENANT = "22222222-2222-2222-2222-222222222222"

SB = object()  # sentinel; every sb use in poll_channel is monkeypatched


def _cfg(**kw):
    base = dict(tenant_id="t1", provider="imap", team="support",
                username="support@acme.com", from_addr="support@acme.com")
    base.update(kw)
    return MailboxConfig(**base)


def _raw(frm="Jane <jane@customer.com>", subject="Help", body="my export fails",
         mid="<m1@customer.com>", extra=None):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "support@acme.com"
    m["Subject"] = subject
    m["Message-ID"] = mid
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(body)
    return m.as_bytes()


@pytest.fixture
def patched(monkeypatch):
    calls = {"enqueue": [], "marked": [], "status": []}
    monkeypatch.setattr(email_watch, "_published_flow_id", lambda sb, t, team: "flow-1")
    monkeypatch.setattr(mailbox, "set_status",
                        lambda tid, sb, status, error=None: calls["status"].append((tid, status, error)))
    monkeypatch.setattr(mailbox, "mark_processed",
                        lambda cfg, refs: calls["marked"].extend(refs))

    def fake_enqueue(kind, payload, *, dedupe_key=None, sb=None):
        calls["enqueue"].append({"kind": kind, "payload": payload, "dedupe_key": dedupe_key})
        return f"job-{len(calls['enqueue'])}"

    monkeypatch.setattr(email_watch.jobs, "enqueue", fake_enqueue)
    return calls


# ── pure helpers ──────────────────────────────────────────────────
def test_should_process_gate():
    cfg = _cfg()
    assert should_process({"from": "jane@customer.com", "subject": "hi"}, cfg)[0]
    assert not should_process({"from": "", "subject": "hi"}, cfg)[0]
    assert not should_process({"from": "jane@x.com", "is_autoreply": True}, cfg)[0]
    assert not should_process({"from": "support@acme.com", "subject": "loop"}, cfg)[0]
    assert not should_process({"from": "jane@x.com"}, cfg)[0]          # empty
    ok, reason = should_process({"from": "SUPPORT@ACME.COM", "subject": "x"}, cfg)
    assert not ok and "itself" in reason


def test_thread_key_prefers_the_thread_root():
    assert thread_key({"references": ["<root@a>", "<r2@a>"], "message_id": "<m@a>"}) == "<root@a>"
    assert thread_key({"in_reply_to": "<p@a>", "message_id": "<m@a>"}) == "<p@a>"
    assert thread_key({"message_id": "<m@a>"}) == "<m@a>"


# ── poll_channel ─────────────────────────────────────────────────
def test_answerable_mail_is_enqueued_with_message_id_keys(patched, monkeypatch):
    monkeypatch.setattr(mailbox, "fetch_new",
                        lambda cfg, **kw: [mailbox.FetchedMessage(ref="7", raw=_raw())])
    n = email_watch.poll_channel(SB, _cfg(), lookback_days=3, limit=50, dry_run=False)
    assert n == 1
    job = patched["enqueue"][0]
    assert job["kind"] == "run_flow"
    assert job["payload"]["idempotency_key"] == "<m1@customer.com>"
    assert job["dedupe_key"] == "email:<m1@customer.com>"
    assert job["payload"]["case"]["channel"] == "email"
    assert job["payload"]["case"]["tenant_id"] == "t1"
    assert job["payload"]["case"]["team"] == "support"
    assert patched["marked"] == ["7"]
    assert patched["status"][-1] == ("t1", "active", None)


def test_autoresponder_is_skipped_but_still_marked(patched, monkeypatch):
    raw = _raw(extra={"Auto-Submitted": "auto-replied"})
    monkeypatch.setattr(mailbox, "fetch_new",
                        lambda cfg, **kw: [mailbox.FetchedMessage(ref="8", raw=raw)])
    n = email_watch.poll_channel(SB, _cfg(), lookback_days=3, limit=50, dry_run=False)
    assert n == 0 and patched["enqueue"] == []
    assert patched["marked"] == ["8"]            # so we don't look at it again


def test_own_mail_is_skipped(patched, monkeypatch):
    raw = _raw(frm="support@acme.com")
    monkeypatch.setattr(mailbox, "fetch_new",
                        lambda cfg, **kw: [mailbox.FetchedMessage(ref="9", raw=raw)])
    assert email_watch.poll_channel(SB, _cfg(), lookback_days=3, limit=50, dry_run=False) == 0
    assert patched["enqueue"] == []


def test_dry_run_enqueues_and_marks_nothing(patched, monkeypatch):
    monkeypatch.setattr(mailbox, "fetch_new",
                        lambda cfg, **kw: [mailbox.FetchedMessage(ref="1", raw=_raw())])
    n = email_watch.poll_channel(SB, _cfg(), lookback_days=3, limit=50, dry_run=True)
    assert n == 1 and patched["enqueue"] == [] and patched["marked"] == []


def test_no_published_flow_sets_error_status(patched, monkeypatch):
    monkeypatch.setattr(email_watch, "_published_flow_id", lambda sb, t, team: None)
    monkeypatch.setattr(mailbox, "fetch_new", lambda cfg, **kw: [])
    n = email_watch.poll_channel(SB, _cfg(), lookback_days=3, limit=50, dry_run=False)
    assert n == 0
    assert patched["status"][-1][0] == "t1" and patched["status"][-1][1] == "error"


def test_fetch_failure_is_caught_and_recorded(patched, monkeypatch):
    def boom(cfg, **kw):
        raise RuntimeError("imap down")

    monkeypatch.setattr(mailbox, "fetch_new", boom)
    n = email_watch.poll_channel(SB, _cfg(), lookback_days=3, limit=50, dry_run=False)
    assert n == 0
    assert patched["status"][-1][1] == "error" and "imap down" in patched["status"][-1][2]


# ── integration: live Supabase + Vault ───────────────────────────
@pytest.mark.integration
def test_tick_finds_an_active_channel_and_records_a_fetch_error():
    if not os.environ.get("SUPABASE_SERVICE_KEY"):
        pytest.skip("no SUPABASE_SERVICE_KEY")
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    cfg = MailboxConfig(tenant_id=GLOBEX_TENANT, provider="imap", team="support",
                        username="watch-test@example.test", from_addr="watch-test@example.test",
                        imap_host="unreachable.invalid.test", status="active")
    mailbox.save_channel(GLOBEX_TENANT, sb, cfg,
                         plaintext_secret='{"kind":"imap","password":"x"}')
    try:
        # a published 'support' flow exists for Globex -> the poller gets past
        # flow-resolution, tries IMAP against a bogus host, records the error.
        total = email_watch.tick(tenant=GLOBEX_TENANT, lookback_days=1, limit=5, dry_run=False)
        assert total == 0
        row = (sb.table("tenant_integrations")
               .select("status,last_error").eq("tenant_id", GLOBEX_TENANT)
               .eq("kind", "email").execute().data[0])
        assert row["status"] == "error" and row["last_error"]
    finally:
        mailbox.delete_channel(GLOBEX_TENANT, sb)
