"""KIL-e — the post-handover watcher. Offline: SF, Neo4j, LLM all stubbed;
`_new_messages` / `_context_for` / `_target` monkeypatched so `watch_case`
logic is what's under test."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

for _k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "NEO4J_URI"):
    os.environ.pop(_k, None)

from interpreter import handoff_watch as hw
from interpreter import integrity, llm


_CTX = [{"text": "Webhooks are not available on the Free plan; they require a "
                 "Business plan for every account.", "ref": "kb", "kind": "kb"}]
_CASE = {"Id": "500HW", "CaseNumber": "00099", "Routed_Team__c": "tier2",
         "Type": "Problem", "CreatedDate": "2026-08-30T09:00:00Z"}


class _Tbl:
    def __init__(self, store, name):
        self.store, self.name, self._f, self._p = store, name, {}, None

    def select(self, *a, **k): return self
    def eq(self, k, v): self._f[k] = v; return self
    def order(self, *a, **k): return self
    def limit(self, n): return self
    @property
    def not_(self): return self
    def in_(self, *a, **k): return self

    def upsert(self, payload, **k): self._p = ("upsert", payload); return self

    def execute(self):
        if self._p:
            self.store.setdefault(self.name, []).append(self._p[1])
            return type("R", (), {"data": [self._p[1]]})
        return type("R", (), {"data": list(self.store.get(self.name + ":seed", []))})


class _SB:
    def __init__(self, seed=None):
        self.store = {}
        for k, v in (seed or {}).items():
            self.store[k + ":seed"] = v

    def table(self, name): return _Tbl(self.store, name)


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: False)
    monkeypatch.setattr(hw, "_context_for", lambda *a, **k: _CTX)
    monkeypatch.setattr(hw, "_target", lambda *a, **k: ("#cx-tier2", "t.1"))
    monkeypatch.setattr(hw, "_missed_pointers", lambda *a, **k: [])
    monkeypatch.setattr("interpreter.salesforce.available", lambda: True)


def _run(sb, msgs, *, dry=False):
    posts = []

    def fake_post(text, *, channel=None, thread_ts=None, **kw):
        posts.append((channel, thread_ts, text))
        return {"sent": True}

    import interpreter.handoff_watch as m
    m._new_messages = lambda sf, cid, since: msgs
    return m.watch_case(sb, _CASE, sf=object(), post=fake_post, dry=dry), posts


def test_sig_is_stable_and_text_sensitive():
    assert hw._sig("contra", "Webhooks are free") == hw._sig("contra", "webhooks  are FREE ")
    assert hw._sig("contra", "a") != hw._sig("contra", "b")


def test_contradicting_agent_reply_is_flagged_once():
    sb = _SB()
    msg = [{"id": "m1", "role": "agent_reply", "author_kind": "agent", "ts": "2026-08-31T10:00:00Z",
            "text": "Webhooks are available on the Free plan for every account."}]
    res, posts = _run(sb, msg)
    assert res["flags"] == 1 and res["new_messages"] == 1
    assert posts and posts[0][0] == "#cx-tier2" and "contradic" in posts[0][2].lower()
    saved = sb.store["handoff_watch_state"][0]
    assert saved["flags_sent"] == 1 and len(saved["seen_sigs"]) == 1


def test_clean_message_no_flag():
    sb = _SB()
    msg = [{"id": "m2", "role": "agent_reply", "author_kind": "agent", "ts": "x",
            "text": "Thanks for your patience, we've reproduced this and are on it."}]
    res, posts = _run(sb, msg)
    assert res["flags"] == 0 and posts == []


def test_rate_limit_caps_flags(monkeypatch):
    monkeypatch.setattr(hw, "_MAX_FLAGS", 3)
    sb = _SB({"handoff_watch_state": [{"case_sf_id": "500HW", "flags_sent": 3, "seen_sigs": []}]})
    msg = [{"id": "m3", "role": "agent_reply", "author_kind": "agent", "ts": "z",
            "text": "Webhooks are available on the Free plan for every account."}]
    res, posts = _run(sb, msg)
    assert res["flags"] == 0 and posts == []


def test_dedup_skips_an_already_seen_signature():
    from interpreter import integrity as _i
    reply = "Webhooks are available on the Free plan for every account."
    sig = hw._sig("contra", _i.check(reply, _CTX, kind="human_reply")["salient"][0])
    sb = _SB({"handoff_watch_state": [{"case_sf_id": "500HW", "flags_sent": 1, "seen_sigs": [sig]}]})
    res, posts = _run(sb, [{"id": "m4", "role": "agent_reply", "author_kind": "agent",
                            "ts": "z", "text": reply}])
    assert res["flags"] == 0 and posts == []


def test_no_new_messages_after_prior_flag_is_a_fast_return():
    sb = _SB({"handoff_watch_state": [{"case_sf_id": "500HW", "flags_sent": 1, "seen_sigs": [],
                                       "last_seen_ts": "2026-08-31T00:00:00Z"}]})
    res, posts = _run(sb, [])
    assert res == {"case": "00099", "new_messages": 0} and posts == []
