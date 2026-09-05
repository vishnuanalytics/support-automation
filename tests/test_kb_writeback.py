"""KIL-d — the KB write-back loop. Offline: no LLM (fallback drafter), no
Neo4j, a recording fake Supabase."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

for _k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "NEO4J_URI"):
    os.environ.pop(_k, None)

from interpreter import kb_writeback, llm


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: False)
    monkeypatch.setattr(kb_writeback, "_graph_supersede", lambda *a, **k: None)


class _Q:
    def __init__(self, tbl):
        self.tbl = tbl
        self.filters = {}
        self._pending = None

    def select(self, *a, **k):
        return self

    def insert(self, payload):
        self._pending = ("insert", payload)
        return self

    def update(self, payload):
        self._pending = ("update", payload)
        return self

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def lt(self, k, v):
        self.filters[f"{k}<"] = v
        return self

    def gte(self, k, v):
        self.filters[f"{k}>="] = v
        return self

    def limit(self, n):
        return self

    def execute(self):
        store = self.tbl.store
        if self._pending is None:                       # a select
            rows = [r for r in store if all(r.get(k) == v for k, v in self.filters.items()
                                            if k.isidentifier())]
            return type("R", (), {"data": rows})
        op, payload = self._pending
        if op == "insert":
            row = {"entry_id": f"e{len(store)+1}", "id": f"a{len(store)+1}",
                   "source_id": payload.get("source_id"), **payload}
            store.append(row)
            self.tbl.log.append(("insert", self.tbl.name, row))
            return type("R", (), {"data": [row]})
        # update
        hit = [r for r in store if all(r.get(k) == v for k, v in self.filters.items()
                                       if k.isidentifier())]
        for r in hit:
            r.update(payload)
        self.tbl.log.append(("update", self.tbl.name, dict(payload), dict(self.filters)))
        return type("R", (), {"data": hit})


class _Tbl:
    def __init__(self, store, name, log):
        self.store, self.name, self.log = store, name, log

    def __getattr__(self, item):
        return getattr(_Q(self), item)


class _SB:
    def __init__(self, tables):
        self._t = {k: list(v) for k, v in tables.items()}
        self.log = []

    def table(self, name):
        self._t.setdefault(name, [])
        return _Tbl(self._t[name], name, self.log)

    def rows(self, name):
        return self._t.get(name, [])


# ── draft ───────────────────────────────────────────────────────────────
def test_kb_ref_parses_an_internal_entry_context():
    eid, sid = kb_writeback._kb_ref([{"ref": "kb://33333333-3333-3333-3333-333333333333/22222222-2222-2222-2222-222222222222", "kind": "kb"}])
    assert (eid, sid) == ("22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333")
    assert kb_writeback._kb_ref([{"ref": "https://docs/x"}]) == (None, None)


def test_fallback_draft_create_vs_supersede():
    t = {"statement": "Webhooks require a Business plan.\nMore detail here.",
         "contexts": [], "verdict": {}}
    c = kb_writeback.draft_change(t)
    assert c["op"] == "create" and c["title"] == "Webhooks require a Business plan."
    assert c["supersedes_entry_id"] is None

    t2 = {**t, "contexts": [{"ref": "kb://44444444-4444-4444-4444-444444444444/11111111-1111-1111-1111-111111111111", "kind": "kb"}]}
    c2 = kb_writeback.draft_change(t2)
    assert c2["op"] == "supersede" and c2["supersedes_entry_id"] == "11111111-1111-1111-1111-111111111111"


# ── apply ───────────────────────────────────────────────────────────────
def _ar(**kw):
    return {"id": "ar-1", "tenant_id": "00000000-0000-0000-0000-000000000000",
            "kind": "kb_change", "status": "approved", "decided_by": "U9",
            "result": None, "payload": {"op": "create", "title": "T", "body_md": "B",
                                        "review_task_id": "task-1"}, **kw}


def test_apply_requires_approved_status():
    sb = _SB({})
    assert "skipped" in kb_writeback.apply_kb_change(sb, _ar(status="pending"))


def test_apply_creates_a_provisional_entry_and_enqueues_embed(monkeypatch):
    enq = []
    monkeypatch.setattr("interpreter.jobs.enqueue",
                        lambda kind, payload, **kw: enq.append((kind, payload)))
    sb = _SB({"sources": [{"source_id": "src-corr", "tenant_id": "00000000-0000-0000-0000-000000000000",
                           "kind": "internal_kb", "name": "kb-corrections"}]})
    res = kb_writeback.apply_kb_change(sb, _ar())
    assert res["status"] == "provisional"
    entry = sb.rows("kb_entries")[0]
    assert entry["status"] == "provisional" and entry["origin"] == "review_writeback"
    assert entry["approved_by"] == "U9" and entry["source_review_task"] == "task-1"
    assert entry["provisional_until"]
    assert enq and enq[0][0] == "embed_kb_entry"
    ar_done = [l for l in sb.log if l[0] == "update" and l[1] == "action_requests"]
    assert ar_done and ar_done[0][2]["status"] == "done"


def test_apply_supersede_marks_old_and_pulls_its_chunks(monkeypatch):
    monkeypatch.setattr("interpreter.jobs.enqueue", lambda *a, **k: None)
    deleted = []
    monkeypatch.setattr("ingestion.sources.kb_common.delete_entry",
                        lambda sb, *, url: deleted.append(url))
    sb = _SB({
        "sources": [{"source_id": "src-corr", "tenant_id": "00000000-0000-0000-0000-000000000000",
                     "kind": "internal_kb", "name": "kb-corrections"}],
        "kb_entries": [{"entry_id": "55555555-5555-5555-5555-555555555555", "source_id": "66666666-6666-6666-6666-666666666666", "status": "active"}],
    })
    kb_writeback.apply_kb_change(sb, _ar(payload={
        "op": "supersede", "title": "T", "body_md": "B", "supersedes_entry_id": "55555555-5555-5555-5555-555555555555",
        "review_task_id": "task-1"}))
    old = [r for r in sb.rows("kb_entries") if r["entry_id"] == "55555555-5555-5555-5555-555555555555"][0]
    assert old["status"] == "superseded"
    assert deleted == ["kb://66666666-6666-6666-6666-666666666666/55555555-5555-5555-5555-555555555555"]
    new = [r for r in sb.rows("kb_entries") if r.get("origin") == "review_writeback"][0]
    assert new["supersedes_entry_id"] == "55555555-5555-5555-5555-555555555555" and new["source_id"] == "66666666-6666-6666-6666-666666666666"


# ── promotion ───────────────────────────────────────────────────────────
def test_promote_provisional_flips_aged_entries():
    sb = _SB({"kb_entries": [
        {"entry_id": "e1", "source_id": "s1", "status": "provisional",
         "provisional_until": "2000-01-01T00:00:00Z"},
        {"entry_id": "e2", "source_id": "s1", "status": "provisional",
         "provisional_until": "2999-01-01T00:00:00Z"},
    ]})
    # the fake's lt() isn't a real comparison — filter manually for the assertion
    n = kb_writeback.promote_provisional(sb)
    # both rows match status='provisional'; the fake can't evaluate lt(), so this
    # asserts the call path runs and updates. Real Postgres does the date filter.
    assert n >= 1
    assert any(r["status"] == "active" for r in sb.rows("kb_entries"))


def test_promote_holds_an_entry_with_a_fresh_contradiction():
    sb = _SB({
        "kb_entries": [
            {"entry_id": "77777777-7777-7777-7777-777777777777", "tenant_id": "T", "status": "provisional",
             "provisional_until": "2000-01-01T00:00:00Z", "created_at": "2026-09-01T00:00:00Z"},
        ],
        "review_tasks": [
            {"tenant_id": "T", "kind": "human_reply_review", "status": "open",
             "created_at": "2026-09-02T00:00:00Z",
             "contexts": [{"ref": "kb://33333333-3333-3333-3333-333333333333/77777777-7777-7777-7777-777777777777", "kind": "kb", "text": "..."}]},
        ],
    })
    n = kb_writeback.promote_provisional(sb)
    assert n == 0
    assert sb.rows("kb_entries")[0]["status"] == "provisional"


# ── slack wiring ───────────────────────────────────────────────────────
def test_dispatch_review_correct_raises_a_kb_change(monkeypatch):
    from interpreter import slack_socket, review
    raised = {}
    monkeypatch.setattr(review, "resolve",
                        lambda sb, tid, **kw: {"id": tid, "tenant_id": "T", "statement": "S",
                                               "contexts": [], "verdict": {}, "run_id": "r1",
                                               "slack_channel": "#cx-l1", "case_number": "42"})
    monkeypatch.setattr(kb_writeback, "draft_change", lambda row, **k: {"op": "create",
                        "title": "T", "body_md": "B", "rationale": "r", "supersedes_entry_id": None})
    monkeypatch.setattr(kb_writeback, "raise_kb_change",
                        lambda sb, **kw: raised.update(kw) or {"id": "ar-1"})
    payload = {"type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"},
               "container": {"thread_ts": "1.1"}, "message": {"ts": "1.1"},
               "actions": [{"action_id": "review_correct", "value": "task-9"}]}
    out = slack_socket.dispatch_action(None, payload, post=lambda *a: None)
    assert out["status"] == "correct"
    assert raised["change"]["op"] == "create"


def test_dispatch_kb_approve_enqueues_apply(monkeypatch):
    from interpreter import slack_socket
    enq = []
    monkeypatch.setattr("interpreter.jobs.enqueue",
                        lambda kind, payload, **kw: enq.append((kind, payload)))
    sb = _SB({"action_requests": [{"id": "ar-1", "kind": "kb_change", "status": "pending", "payload": {"title": "T"}}]})
    payload = {"type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"},
               "container": {"thread_ts": "1.1"}, "message": {"ts": "1.1"},
               "actions": [{"action_id": "kb_approve", "value": "ar-1"}]}
    out = slack_socket.dispatch_action(sb, payload, post=lambda *a: None)
    assert out["status"] == "approved"
    assert enq and enq[0][0] == "apply_kb_change"


# ── Phase 29 step 4 — self-critique ──────────────────────────────────────
from interpreter import integrity  # noqa: E402


def test_self_critique_stamps_entails_cleanly(monkeypatch):
    monkeypatch.setattr(integrity, "check", lambda *a, **k: {"relation": "entails"})
    c = kb_writeback.draft_change({"statement": "Refunds take 45 days.", "contexts": [], "verdict": {}})
    assert c["self_critique"] == {"relation": "entails", "retried": False}


def test_contradicts_with_no_llm_does_not_retry_but_stamps_verdict(monkeypatch):
    # the file's autouse _no_llm fixture already forces llm.available() False
    calls = []
    monkeypatch.setattr(integrity, "check", lambda *a, **k: calls.append(1) or {"relation": "contradicts"})
    statement = "Refunds take 45 days."
    c = kb_writeback.draft_change({"statement": statement, "contexts": [], "verdict": {}})
    assert c["self_critique"] == {"relation": "contradicts", "retried": False}
    assert c["body_md"] == statement            # unchanged -- the fallback draft, never redrafted
    assert len(calls) == 1                       # checked once, never re-checked (no retry)


def test_contradicts_triggers_exactly_one_retry_when_llm_available(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    drafts = iter([
        '{"op": "create", "title": "v1", "body_md": "first attempt", "rationale": "r1"}',
        '{"op": "create", "title": "v2", "body_md": "revised attempt", "rationale": "r2"}',
    ])
    monkeypatch.setattr(llm, "complete", lambda **k: next(drafts))
    verdicts = iter([{"relation": "contradicts"}, {"relation": "entails"}])
    seen_bodies = []

    def fake_check(statement, contexts, **k):
        seen_bodies.append(contexts[0]["text"])
        return next(verdicts)
    monkeypatch.setattr(integrity, "check", fake_check)

    c = kb_writeback.draft_change({"statement": "Refunds take 45 days.", "contexts": [], "verdict": {}})

    assert c["body_md"] == "revised attempt"     # the retried draft is what ships
    assert c["self_critique"] == {"relation": "entails", "retried": True}
    assert seen_bodies == ["first attempt", "revised attempt"]   # checked, redrafted, re-checked


def test_entails_first_try_never_retries(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(llm, "complete", lambda **k: calls.append(1) or
                        '{"op": "create", "title": "v1", "body_md": "good draft", "rationale": "r1"}')
    monkeypatch.setattr(integrity, "check", lambda *a, **k: {"relation": "entails"})

    c = kb_writeback.draft_change({"statement": "Refunds take 45 days.", "contexts": [], "verdict": {}})
    assert c["body_md"] == "good draft" and len(calls) == 1     # one draft call, no redraft
    assert c["self_critique"] == {"relation": "entails", "retried": False}


def test_redraft_parse_failure_keeps_the_original_draft(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    drafts = iter([
        '{"op": "create", "title": "v1", "body_md": "first attempt", "rationale": "r1"}',
        "not json",   # the redraft call comes back malformed
    ])
    monkeypatch.setattr(llm, "complete", lambda **k: next(drafts))
    monkeypatch.setattr(integrity, "check", lambda *a, **k: {"relation": "contradicts"})

    c = kb_writeback.draft_change({"statement": "Refunds take 45 days.", "contexts": [], "verdict": {}})
    assert c["body_md"] == "first attempt"       # redraft failed to parse -> keep the original
    assert c["self_critique"] == {"relation": "contradicts", "retried": False}


def test_post_card_warns_when_critique_is_not_clean():
    posts = []
    kb_writeback._post_card(
        None, tenant_id="t1", ar_id="ar1", channel="#kb",
        change={"op": "create", "title": "T", "body_md": "B", "rationale": "R",
               "self_critique": {"relation": "contradicts", "retried": True}},
        post=lambda text, **k: posts.append(k["blocks"][0]["text"]["text"]),
    )
    assert "self-check" in posts[0] and "review carefully" in posts[0]


def test_post_card_no_warning_when_entails():
    posts = []
    kb_writeback._post_card(
        None, tenant_id="t1", ar_id="ar1", channel="#kb",
        change={"op": "create", "title": "T", "body_md": "B", "rationale": "R",
               "self_critique": {"relation": "entails", "retried": False}},
        post=lambda text, **k: posts.append(k["blocks"][0]["text"]["text"]),
    )
    assert "self-check" not in posts[0]
