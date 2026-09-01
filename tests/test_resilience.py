"""
Phase 23 — Tier 1 resilience: LLM fallback chain + cache, channel
auto-recovery, Salesforce write idempotency, CDC self-write filter.
Offline; every provider / client is stubbed.
"""

from __future__ import annotations

import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ingestion.sf_pubsub.plan import plan_events
from interpreter import llm, mailbox, salesforce

_KEYS = ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")
for _k in _KEYS:
    os.environ.pop(_k, None)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("SF_DEDUP_WRITES", raising=False)   # don't inherit .env
    llm._cache.clear()


# --------------------------------------------------------------------------
# LLM fallback chain + cache
# --------------------------------------------------------------------------
def test_recoverable_classification():
    assert llm._is_recoverable(RuntimeError("openrouter 429: rate limited")) is True
    assert llm._is_recoverable(RuntimeError("Read timed out")) is True
    assert llm._is_recoverable(ValueError("bad prompt")) is False


def test_fallback_chain_only_keyed_providers(monkeypatch):
    assert llm._fallback_chain("openai/gpt-oss-20b") == []          # no keys -> stub
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    ch = llm._fallback_chain("openai/gpt-oss-20b")
    assert llm.FALLBACK_MODEL in ch and "openai/gpt-oss-20b" not in ch  # groq unkeyed
    monkeypatch.setenv("GROQ_API_KEY", "x")
    ch = llm._fallback_chain("openai/gpt-oss-20b")
    assert ch[0] == "openai/gpt-oss-20b" and llm.FALLBACK_MODEL in ch


def test_complete_falls_through_to_the_next_provider(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    calls: list[str] = []

    def fake_dispatch(model, *a, **k):
        calls.append(model)
        if llm.provider(model) == "groq":
            raise RuntimeError("Error code: 429 rate_limit_exceeded")
        return '{"ok": 1}'

    monkeypatch.setattr(llm, "_dispatch", fake_dispatch)
    out = llm.complete("s", "u json", model="openai/gpt-oss-20b", json_object=True)
    assert out == '{"ok": 1}'
    assert calls[0] == "openai/gpt-oss-20b" and llm.provider(calls[-1]) == "openrouter"


def test_complete_caches(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    n = [0]
    monkeypatch.setattr(llm, "_dispatch", lambda *a, **k: (n.__setitem__(0, n[0] + 1), '{"a":1}')[1])
    a = llm.complete("sys", "user", model=llm.FALLBACK_MODEL, json_object=True, cache=True)
    b = llm.complete("sys", "user", model=llm.FALLBACK_MODEL, json_object=True, cache=True)
    assert a == b == '{"a":1}' and n[0] == 1          # second call served from cache


def test_complete_gives_up_to_stub(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setattr(llm, "_dispatch",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503 overloaded")))
    out = llm.complete("classify", "billing dispute", model=llm.FALLBACK_MODEL, json_object=True)
    assert '"_stub": true' in out


# --------------------------------------------------------------------------
# channel auto-recovery
# --------------------------------------------------------------------------
def test_error_backoff_grows_and_caps():
    got = [mailbox._error_backoff_minutes(i) for i in range(8)]
    assert got == [1, 2, 4, 8, 16, 30, 30, 30]


class _SB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, *_):
        return self

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def in_(self, *_):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


def test_list_pollable_skips_a_not_due_errored_channel(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"tenant_id": "A", "status": "active", "last_poll_at": now.isoformat(), "config": {}},
        {"tenant_id": "B", "status": "error", "config": {"error_retries": 3},
         "last_poll_at": (now - timedelta(minutes=2)).isoformat()},     # backoff 8m -> not due
        {"tenant_id": "C", "status": "error", "config": {"error_retries": 3},
         "last_poll_at": (now - timedelta(minutes=20)).isoformat()},    # due
    ]
    monkeypatch.setattr(mailbox, "load_channel",
                        lambda tid, sb: type("MC", (), {"tenant_id": tid})())
    out = {c.tenant_id for c in mailbox.list_pollable_channels(_SB(rows))}
    assert out == {"A", "C"}


# --------------------------------------------------------------------------
# Salesforce write idempotency
# --------------------------------------------------------------------------
def test_add_case_comment_dedupes(monkeypatch):
    monkeypatch.setattr(salesforce, "available", lambda: True)
    created: list = []

    class _Client:
        class CaseComment:
            @staticmethod
            def create(rec):
                created.append(rec)
                return {"id": "cc1"}

        def query(self, soql):
            # first call: an identical comment already exists
            return {"records": [{"Id": "old"}] if _Client._dupe else []}

    _Client._dupe = True
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _Client())
    r = salesforce.add_case_comment("500X", "Return 2xx within 10s.", tenant_id=None)
    assert r["deduped"] is True and created == []

    _Client._dupe = False
    r = salesforce.add_case_comment("500X", "Return 2xx within 10s.", tenant_id=None)
    assert r["created"] is True and len(created) == 1


# --------------------------------------------------------------------------
# CDC self-write filter
# --------------------------------------------------------------------------
def _owner_evt(commit_user: str) -> dict:
    return {
        "ChangeEventHeader": {"entityName": "Case", "changeType": "UPDATE",
                              "recordIds": ["500X"], "changedFields": ["OwnerId"],
                              "commitUser": commit_user},
        "OwnerId": "00G000000000QUE",
    }


def test_plan_events_drops_the_bots_own_owner_change():
    assert plan_events(_owner_evt("005BOT"), "aa", bot_user_id="005BOT") == []
    specs = plan_events(_owner_evt("005HUMAN"), "aa", bot_user_id="005BOT")
    assert specs and specs[0].trigger == "case_owner_changed"
    # no bot id known -> don't filter (back-compat)
    assert plan_events(_owner_evt("005BOT"), "aa") != []


# --------------------------------------------------------------------------
# Salesforce: cross-path Case dedup + append idempotency
# --------------------------------------------------------------------------
def test_thread_ids_include_the_messages_own_id():
    ids = salesforce._thread_msg_ids({
        "message_id": "<own@x>", "in_reply_to": "<parent@y>", "references": ["<root@z>"],
    })
    assert "<own@x>" in ids and "own@x" in ids           # so E2C's Case is found + reused
    assert ids.index("<own@x>") < ids.index("<parent@y>")


def test_update_case_fields_append_is_idempotent(monkeypatch):
    monkeypatch.setattr(salesforce, "available", lambda: True)
    updates: list = []

    class _C:
        class Case:
            _desc = "line one"

            @classmethod
            def get(cls, cid):
                return {"Description": cls._desc}

            @classmethod
            def update(cls, cid, rec):
                updates.append(rec)
                if "Description" in rec:
                    cls._desc = rec["Description"]

    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: _C())

    salesforce.update_case_fields("500X", {}, append={"Description": "[triage] hello"})
    salesforce.update_case_fields("500X", {}, append={"Description": "[triage] hello"})
    # the block is written once; the second run sees it already present -> no-op
    assert sum("[triage] hello" in (u.get("Description") or "") for u in updates) == 1


def test_routing_cache_hits(monkeypatch):
    from interpreter import routing

    routing._cache.clear()
    monkeypatch.setattr(routing, "_fetch_rows",
                        lambda t, sb: [{"match_kind": "case_type", "match_value": "Billing",
                                        "resolver": "static", "sf_target_id": "005X",
                                        "sf_target_type": "user", "label": "Billing"}])
    calls = [0]
    orig = routing._fetch_rows
    monkeypatch.setattr(routing, "_fetch_rows", lambda t, sb: (calls.__setitem__(0, calls[0] + 1), orig(t, sb))[1])
    routing.resolve_notify_target("t", "Billing", None)
    routing.resolve_notify_target("t", "Billing", None)
    assert calls[0] == 1                                   # second call served from cache
