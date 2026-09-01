"""Phase 24e — the reasoning dialogue engine (offline; no real LLM, no DB).

New model: LLM prunes the seed bank to what THIS case needs, asks it all in one
message, then ≤ max_rounds short follow-ups if a *critical* point is still open,
then draft + approve.
"""

from __future__ import annotations

import json

import pytest

from interpreter import reasoning


def make_stub(*, plan=None, ingest=None, ask="1. Q\n   _my read:_ x", draft="Hi — here is our answer."):
    def stub(system: str, user: str, **_kw) -> str:
        if "triaging a customer support case" in system:          # _PLAN_SYS
            return json.dumps(plan if plan is not None
                              else [{"q": "Q1", "critical": True}, {"q": "Q2", "critical": True}])
        if "briefing a colleague" in system:                      # _ASK_SYS
            return ask
        if "replied (free-form)" in system:                       # _INGEST_SYS
            return json.dumps(ingest if ingest is not None
                              else [{"answered": True, "note": "a"}, {"answered": True, "note": "b"}])
        if "customer-facing reply" in system:                     # _DRAFT_SYS
            return draft
        return ""
    return stub


CASE = {"sf_id": "500X", "subject": "Refund for a double charge",
        "body": "I was charged twice on the 3rd. Please refund one.",
        "case_number": "00001234"}


def _session(qs, **over):
    s = {
        "session_id": "s1", "case_id": "500X", "case_number": "00001234",
        "state": "awaiting_handoff", "cursor": 0, "transcript": [], "max_rounds": 3,
        "pointers": [{"q": q, "critical": crit, "answered": False, "agent_note": None}
                     for q, crit in qs],
    }
    s.update(over)
    return s


P2 = [("What is disputed?", True), ("Do we need their billing data?", True)]


# ── handoff detection ──────────────────────────────────────────────
def test_is_handoff():
    for yes in ("take", "take it", "Take this one", "assist", "discuss",
                "walk me through it", "your turn"):
        assert reasoning.is_handoff(yes), yes
    for no in ("the customer says it's urgent", "I think this is billing",
              "what's the refund policy"):
        assert not reasoning.is_handoff(no), no


def test_at_mention_counts_as_handoff():
    s = _session(P2)
    out = reasoning.advance(s, "sort this please", case=CASE, llm_fn=make_stub(), handoff=True)
    assert out["session"]["state"] == "clarifying"


# ── question planning ─────────────────────────────────────────────
class _FakeSB:
    def __init__(self, bank):
        self.bank = bank
        self.rows = []

    def table(self, name):
        return _FakeQ(self, name)


class _FakeQ:
    def __init__(self, sb, name):
        self.sb, self.name, self.f = sb, name, {}

    def select(self, *_a, **_k): return self
    def eq(self, k, v): self.f[k] = v; return self
    @property
    def not_(self): return self
    def in_(self, *_a, **_k): return self
    def limit(self, *_a, **_k): return self
    def order(self, *_a, **_k): return self

    def insert(self, row):
        row = {**row, "session_id": "s-new"}; self.sb.rows.append(row); self._ins = row; return self

    def update(self, patch): self.sb.rows.append(("update", patch)); self._upd = True; return self

    def execute(self):
        if getattr(self, "_ins", None):
            return type("R", (), {"data": [self._ins]})
        if getattr(self, "_upd", None):
            return type("R", (), {"data": []})
        if self.name == "pointer_bank":
            ct = self.f.get("case_type")
            return type("R", (), {"data": [{"pointers": self.sb.bank[ct]}] if ct in self.sb.bank else []})
        return type("R", (), {"data": []})


def test_plan_questions_prunes_to_the_llm_subset():
    sb = _FakeSB({"Billing": ["a", "b", "c", "d", "e"], "Other": ["o1", "o2", "o3"]})
    ps = reasoning.plan_questions(
        sb, case_type="Billing", case=CASE,
        llm_fn=make_stub(plan=[{"q": "Just this one", "critical": True}]))
    assert [p["q"] for p in ps] == ["Just this one"]
    assert ps[0]["critical"] is True and ps[0]["answered"] is False


def test_plan_questions_falls_back_when_llm_gives_nothing():
    sb = _FakeSB({"Other": ["o1", "o2", "o3"]})
    ps = reasoning.plan_questions(sb, case_type="Zzz", case=CASE,
                                  llm_fn=make_stub(plan=[]))
    assert 1 <= len(ps) <= reasoning._MAX_QUESTIONS


# ── the dialogue ──────────────────────────────────────────────────
def test_nudge_until_handoff():
    out = reasoning.advance(_session(P2), "looks like a dupe", case=CASE, llm_fn=make_stub())
    assert out["action"] is None and out["session"]["state"] == "awaiting_handoff"
    assert "take" in out["reply"].lower()


def test_handoff_asks_every_question_in_one_message():
    s = _session(P2)
    out = reasoning.advance(s, "take it", case=CASE, llm_fn=make_stub(ask="1. What is disputed?\n2. Do we need data?"))
    s = out["session"]
    assert s["state"] == "clarifying" and s["cursor"] == 1
    # one message contains both questions — not a per-question walk
    assert "1." in out["reply"] and "2." in out["reply"]


def test_answering_everything_goes_straight_to_draft():
    s = _session(P2, state="clarifying", cursor=1)
    out = reasoning.advance(
        s, "It's the amount; yes we need their invoice.", case=CASE,
        llm_fn=make_stub(ingest=[{"answered": True, "note": "amount"},
                                 {"answered": True, "note": "yes, invoice"}]))
    s = out["session"]
    assert s["state"] == "awaiting_approval"
    assert s["draft"] == "Hi — here is our answer."
    assert "————" in out["reply"] and "`send`" in out["reply"]


def test_open_critical_point_triggers_one_short_followup():
    s = _session(P2, state="clarifying", cursor=1)
    out = reasoning.advance(
        s, "It's about the amount.", case=CASE,
        llm_fn=make_stub(ingest=[{"answered": True, "note": "amount"},
                                 {"answered": False, "note": ""}]))
    s = out["session"]
    assert s["state"] == "clarifying" and s["cursor"] == 2
    assert "Do we need their billing data?" in out["reply"] and "round 2/3" in out["reply"]


def test_it_drafts_anyway_after_max_rounds():
    s = _session(P2, state="clarifying", cursor=3, max_rounds=3)   # already on the last round
    out = reasoning.advance(
        s, "not sure honestly", case=CASE,
        llm_fn=make_stub(ingest=[{"answered": False, "note": ""},
                                 {"answered": False, "note": ""}]))
    s = out["session"]
    assert s["state"] == "awaiting_approval"          # used our rounds -> draft anyway
    assert "still unconfirmed" in out["reply"]


def test_approval_sends():
    s = _session(P2, state="awaiting_approval", draft="the draft")
    out = reasoning.advance(s, "looks good, send it", case=CASE, llm_fn=make_stub())
    assert out["action"] == "send" and out["session"]["state"] == "sent"


def test_edit_redrafts_and_stays_pending():
    s = _session(P2, state="awaiting_approval", draft="v1")
    out = reasoning.advance(s, "edit: shorter", case=CASE,
                            llm_fn=make_stub(draft="v2 shorter"))
    assert out["action"] is None and out["session"]["state"] == "awaiting_approval"
    assert out["session"]["draft"] == "v2 shorter"


def test_no_abandons():
    out = reasoning.advance(_session(P2, state="awaiting_approval", draft="v1"),
                            "no", case=CASE, llm_fn=make_stub())
    assert out["action"] == "abandoned" and out["session"]["state"] == "abandoned"


def test_terminal_states_are_inert():
    for st in ("sent", "abandoned"):
        out = reasoning.advance(_session(P2, state=st), "send", case=CASE, llm_fn=make_stub())
        assert out["action"] is None and st in out["reply"]


def test_open_session_records_max_rounds(monkeypatch):
    sb = _FakeSB({"Billing": ["a", "b", "c"]})
    s = reasoning.open_session(sb, case=CASE, tenant_id="t", case_type="Billing",
                               max_rounds=2, llm_fn=make_stub(plan=[{"q": "x", "critical": True}]))
    assert s["max_rounds"] == 2 and s["state"] == "awaiting_handoff"


def test_handle_agent_message_persists(monkeypatch):
    sb = _FakeSB({"Other": ["o1", "o2"]})
    monkeypatch.setattr(reasoning, "_case_for_session", lambda _sb, _s: CASE)
    out = reasoning.handle_agent_message(sb, _session(P2, session_id="s9"),
                                         "take it", llm_fn=make_stub())
    assert out["session"]["state"] == "clarifying"
    assert any(isinstance(r, tuple) and r[0] == "update" and r[1]["state"] == "clarifying"
               for r in sb.rows)
