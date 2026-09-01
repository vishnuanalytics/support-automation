"""Phase 24b — the Slack reasoning dialogue engine (offline, no LLM, no DB)."""

from __future__ import annotations

import pytest

from interpreter import reasoning


def _stub_llm(system: str, user: str, **_kw) -> str:
    if "JSON array" in system:
        return "[]"                       # no LLM top-up
    if "colleague" in system:
        return "my read on that point"
    if "customer-facing reply" in system:
        return "Hi there — here is our answer. Thanks!"
    return ""


CASE = {"sf_id": "500X", "subject": "Refund for a double charge",
        "body": "I was charged twice on the 3rd. Please refund one.",
        "case_number": "00001234"}


def _session(pointer_qs, **over):
    s = {
        "session_id": "s1", "case_id": "500X", "case_number": "00001234",
        "state": "awaiting_handoff", "cursor": 0, "transcript": [],
        "pointers": [{"q": q, "answered": False, "bot_take": None, "agent_note": None}
                     for q in pointer_qs],
    }
    s.update(over)
    return s


P4 = ["What is disputed?", "What does billing show?", "One-off or recurring?",
      "Do we need their data?"]


# ── handoff detection ───────────────────────────────────────────────
def test_is_handoff():
    for yes in ("take", "take it", "Take this one", "@bot take this", "go ahead",
                "your turn", "over to you"):
        assert reasoning.is_handoff(yes), yes
    for no in ("the customer says it's urgent", "I think this is billing",
              "can you check the logs", "what's the refund policy"):
        assert not reasoning.is_handoff(no), no


# ── the dialogue ───────────────────────────────────────────────────
def test_nudge_until_handoff():
    s = _session(P4)
    out = reasoning.advance(s, "this looks like a dupe charge", case=CASE, llm_fn=_stub_llm)
    assert out["action"] is None
    assert out["session"]["state"] == "awaiting_handoff"
    assert "take" in out["reply"].lower()


def test_handoff_opens_with_pointer_one():
    s = _session(P4)
    out = reasoning.advance(s, "take it", case=CASE, llm_fn=_stub_llm)
    assert out["session"]["state"] == "reasoning"
    assert out["session"]["cursor"] == 0
    assert out["session"]["pointers"][0]["bot_take"] == "my read on that point"
    assert "1/4" in out["reply"] and P4[0] in out["reply"]


def test_it_works_through_every_pointer_and_never_drafts_early():
    s = _session(P4, state="reasoning",
                 pointers=[{"q": q, "answered": False, "bot_take": "x",
                            "agent_note": None} for q in P4])
    # answer the first THREE — it must still be reasoning, not drafting
    for i, ans in enumerate(["an amount", "two charges on the 3rd", "one-off"]):
        out = reasoning.advance(s, ans, case=CASE, llm_fn=_stub_llm)
        s = out["session"]
        assert s["state"] == "reasoning", f"drafted early after {i + 1} answers"
        assert s["pointers"][i]["answered"] and s["pointers"][i]["agent_note"] == ans
    assert reasoning._n_answered(s["pointers"]) == 3
    assert f"4/4" in out["reply"]                       # asking the last one

    # answer the last -> now it drafts and asks for approval
    out = reasoning.advance(s, "yes we need their invoice", case=CASE, llm_fn=_stub_llm)
    s = out["session"]
    assert s["state"] == "awaiting_approval"
    assert s["draft"] == "Hi there — here is our answer. Thanks!"
    assert "————" in out["reply"] and "`send`" in out["reply"]


def test_approval_sends():
    s = _session(P4, state="awaiting_approval", draft="the draft text")
    out = reasoning.advance(s, "looks good, send it", case=CASE, llm_fn=_stub_llm)
    assert out["action"] == "send"
    assert out["session"]["state"] == "sent"


def test_edit_redrafts_and_stays_pending():
    s = _session(P4, state="awaiting_approval", draft="v1")
    out = reasoning.advance(s, "edit: make it shorter and warmer", case=CASE, llm_fn=_stub_llm)
    assert out["action"] is None
    assert out["session"]["state"] == "awaiting_approval"
    assert out["session"]["draft"] == "Hi there — here is our answer. Thanks!"
    assert "Updated draft" in out["reply"]


def test_no_abandons():
    s = _session(P4, state="awaiting_approval", draft="v1")
    out = reasoning.advance(s, "no", case=CASE, llm_fn=_stub_llm)
    assert out["action"] == "abandoned"
    assert out["session"]["state"] == "abandoned"


def test_terminal_states_are_inert():
    for st in ("sent", "abandoned"):
        out = reasoning.advance(_session(P4, state=st), "send", case=CASE, llm_fn=_stub_llm)
        assert out["action"] is None and st in out["reply"]


# ── pointer bank ───────────────────────────────────────────────────
class _FakeSB:
    def __init__(self, bank):
        self.bank = bank
        self.updates = []
        self.inserted = []

    def table(self, name):
        return _FakeQ(self, name)


class _FakeQ:
    def __init__(self, sb, name):
        self.sb, self.name, self.f = sb, name, {}

    def select(self, *_a, **_k): return self
    def eq(self, k, v): self.f[k] = v; return self
    def not_(self): return self
    def in_(self, *_a, **_k): return self
    def order(self, *_a, **_k): return self
    def limit(self, *_a, **_k): return self

    def insert(self, row):
        row = {**row, "session_id": "s-new"}
        self.sb.inserted.append(row); self._ins = row; return self

    def update(self, patch):
        self.sb.updates.append(patch); self._upd = True; return self

    def execute(self):
        if getattr(self, "_ins", None):
            return type("R", (), {"data": [self._ins]})
        if getattr(self, "_upd", None):
            return type("R", (), {"data": []})
        if self.name == "pointer_bank":
            ct = self.f.get("case_type")
            rows = [{"pointers": self.sb.bank[ct]}] if ct in self.sb.bank else []
            return type("R", (), {"data": rows})
        return type("R", (), {"data": []})


def test_build_pointers_uses_seed_bank_and_caps_at_six():
    sb = _FakeSB({"Billing": ["q1", "q2", "q3", "q4", "q5"], "Other": ["o1", "o2", "o3", "o4"]})
    ps = reasoning.build_pointers(sb, case_type="Billing", case=CASE, llm_fn=_stub_llm)
    assert [p["q"] for p in ps] == ["q1", "q2", "q3", "q4", "q5"]
    assert all(p["answered"] is False and p["bot_take"] is None for p in ps)


def test_build_pointers_falls_back_to_other_then_hardcoded():
    sb = _FakeSB({"Other": ["o1", "o2", "o3", "o4"]})
    ps = reasoning.build_pointers(sb, case_type="Nonsense", case=CASE, llm_fn=_stub_llm)
    assert [p["q"] for p in ps] == ["o1", "o2", "o3", "o4"]
    sb2 = _FakeSB({})
    ps2 = reasoning.build_pointers(sb2, case_type=None, case=CASE, llm_fn=_stub_llm)
    assert len(ps2) >= reasoning._MIN_POINTERS


def test_handle_agent_message_persists(monkeypatch):
    sb = _FakeSB({"Other": ["o1", "o2", "o3", "o4"]})
    monkeypatch.setattr(reasoning, "_case_for_session", lambda _sb, _s: CASE)
    row = _session(P4, session_id="s9")
    out = reasoning.handle_agent_message(sb, row, "take it", llm_fn=_stub_llm)
    assert out["session"]["state"] == "reasoning"
    assert sb.updates and sb.updates[-1]["state"] == "reasoning"
