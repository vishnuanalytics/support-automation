"""KIL-c — the human-reply review queue. Offline: no LLM key (heuristic judge),
a recording fake Supabase, Slack `post` injected."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

for _k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)

from interpreter import llm, review


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)


class _Tbl:
    def __init__(self, store, name):
        self.store, self.name, self._flt = store, name, {}

    def upsert(self, payload, **kw):
        self._pending = ("upsert", payload)
        return self

    def update(self, payload):
        self._pending = ("update", payload)
        return self

    def insert(self, payload):
        self._pending = ("insert", payload)
        return self

    def eq(self, k, v):
        self._flt[k] = v
        return self

    def execute(self):
        op, payload = self._pending
        rec = {"op": op, "table": self.name, "payload": payload, "filter": dict(self._flt)}
        self.store.append(rec)
        if op == "upsert":
            row = {"id": "task-1", **payload}
            return type("R", (), {"data": [row]})
        if op == "update":
            return type("R", (), {"data": [{"id": self._flt.get("id", "task-1"), **payload}]})
        return type("R", (), {"data": []})


class _SB:
    def __init__(self):
        self.ops = []

    def table(self, name):
        return _Tbl(self.ops, name)


_RUN = {
    "run_id": "run-9", "tenant_id": "00000000-0000-0000-0000-000000000000",
    "team": "support", "case_payload": {"sf_id": "500X", "case_number": "00042"},
    "retrieval": [{"chunk_text": "Webhooks are not available on the Free plan; they "
                                 "require a Business plan for every account.",
                   "doc_url": "https://docs/webhooks"}],
    "trace": [{"type": "draft", "data": {"prior_resolutions": []}}],
}


def test_assemble_contexts_pulls_retrieval_and_prior():
    run = {**_RUN, "trace": [{"type": "draft", "data": {"prior_resolutions": [
        {"resolution_text": "Ask them to upgrade to Business.", "case_number": "00007"}]}}]}
    ctx = review.assemble_contexts(run)
    kinds = [c["kind"] for c in ctx]
    assert "resolution" in kinds and "kb" in kinds


def test_should_sample_bounds():
    assert review.should_sample(0.0) is False
    assert review.should_sample(1.0) is True


def test_flagged_reply_opens_a_review_task_and_posts(monkeypatch):
    sb = _SB()
    posted = {}

    def fake_post(text, *, tenant_id=None, channel=None, blocks=None, **kw):
        posted.update(channel=channel, blocks=blocks)
        return {"sent": True, "channel": "C1", "ts": "111.1"}

    monkeypatch.setattr(review, "_SAMPLE_RATE", 0.0)
    monkeypatch.setattr("interpreter.routing.resolve_slack_route",
                        lambda *a, **k: {"channel": "#cx-l1", "usergroup": "@cx-l1-oncall"})
    monkeypatch.setattr("interpreter.slack.usergroup_ref", lambda h, **k: "<!subteam^S1>")

    task = review.judge_human_reply(
        sb, run_row=_RUN,
        reply_text="Webhooks are available on the Free plan for every account.",
        post=fake_post)

    assert task and task["kind"] == "human_reply_review"
    assert task["trigger"] == "contradicts"
    ins = [o for o in sb.ops if o["table"] == "review_tasks" and o["op"] == "upsert"][0]
    assert ins["payload"]["verdict"]["flagged"] is True
    assert posted["channel"] == "#cx-l1"
    # the button value is the task id, not the case id
    btns = posted["blocks"][1]["elements"]
    assert all(b["value"] == "task-1" for b in btns)


def test_clean_reply_no_sample_opens_nothing(monkeypatch):
    sb = _SB()
    monkeypatch.setattr(review, "_SAMPLE_RATE", 0.0)
    out = review.judge_human_reply(
        sb, run_row=_RUN,
        reply_text="Thanks for your patience — I've reproduced this and escalated it.",
        post=lambda *a, **k: {"sent": True})
    assert out is None
    assert not [o for o in sb.ops if o["table"] == "review_tasks"]


def test_clean_reply_sampled_opens_a_sample_task(monkeypatch):
    sb = _SB()
    monkeypatch.setattr("interpreter.routing.resolve_slack_route",
                        lambda *a, **k: {"channel": "#cx-l1", "usergroup": None})
    monkeypatch.setattr("interpreter.slack.usergroup_ref", lambda h, **k: None)
    task = review.judge_human_reply(
        sb, run_row=_RUN, sample_rate=1.0,
        reply_text="Thanks for your patience — I've reproduced this and escalated it.",
        post=lambda *a, **k: {"sent": True, "channel": "C1", "ts": "9.9"})
    assert task["kind"] == "sample" and task["trigger"] == "sample"


def test_resolve_rejects_a_bad_status():
    with pytest.raises(ValueError):
        review.resolve(_SB(), "task-1", status="banana")


def test_resolve_marks_the_task(monkeypatch):
    sb = _SB()
    row = review.resolve(sb, "task-1", status="correct", reviewer_id="U9")
    assert row["status"] == "correct"
    upd = [o for o in sb.ops if o["op"] == "update"][0]
    assert upd["payload"]["status"] == "correct" and upd["filter"]["status"] == "open"


def test_dispatch_action_review_button_resolves(monkeypatch):
    from interpreter import slack_socket
    sb = _SB()
    notes = []
    payload = {
        "type": "block_actions",
        "user": {"id": "U7"},
        "channel": {"id": "C1"},
        "container": {"thread_ts": "111.1"},
        "message": {"ts": "111.1"},
        "actions": [{"action_id": "review_dismiss", "value": "task-1"}],
    }
    out = slack_socket.dispatch_action(
        sb, payload, post=lambda c, t, txt: notes.append(txt))
    assert out["status"] == "dismissed" and out["task"] == "task-1"
    assert any("Dismissed" in n for n in notes)
