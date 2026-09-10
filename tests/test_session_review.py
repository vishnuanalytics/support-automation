"""Response Quality Feedback Loop chunk C — interpreter/session_review.py
(judge_session) and interpreter/sweeps.py::session_review_sweep."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import session_review, sweeps


# ── judge_session ────────────────────────────────────────────────────────
_TRANSCRIPT = [
    {"role": "agent", "text": "take", "at": "t0"},
    {"role": "bot", "text": "What exactly is the customer disputing?", "at": "t1"},
    {"role": "agent", "text": "A duplicate charge on their last invoice.", "at": "t2"},
    {"role": "bot", "text": "Here's my draft...", "at": "t3"},
    {"role": "agent", "text": "send", "at": "t4"},
]


def test_judge_session_parses_a_clean_verdict(monkeypatch):
    monkeypatch.setattr(
        "interpreter.llm.complete",
        lambda **k: '{"category": "sound", "severity": 0.1, "summary": "converged in one round"}',
    )
    out = session_review.judge_session(_TRANSCRIPT, pointers=[{"q": "x", "agent_note": "y"}],
                                       draft="d", state="sent", subject="Billing dispute")
    assert out == {"category": "sound", "severity": 0.1, "summary": "converged in one round"}


def test_judge_session_clamps_severity_out_of_range(monkeypatch):
    monkeypatch.setattr(
        "interpreter.llm.complete",
        lambda **k: '{"category": "redundant", "severity": -3, "summary": "looped twice"}',
    )
    out = session_review.judge_session(_TRANSCRIPT, state="sent")
    assert out["severity"] == 0.0


def test_judge_session_unknown_category_falls_back_to_other(monkeypatch):
    monkeypatch.setattr(
        "interpreter.llm.complete",
        lambda **k: '{"category": "flawless", "severity": 0.0, "summary": "x"}',
    )
    out = session_review.judge_session(_TRANSCRIPT, state="sent")
    assert out["category"] == "other"


def test_judge_session_empty_transcript_is_unknown_without_calling_llm(monkeypatch):
    def _boom(**k):
        raise AssertionError("must not call the judge with an empty transcript")
    monkeypatch.setattr("interpreter.llm.complete", _boom)
    assert session_review.judge_session([], state="abandoned")["category"] == "unknown"
    assert session_review.judge_session([{"role": "bot", "text": "  "}])["category"] == "unknown"


def test_judge_session_judge_failure_is_unknown_not_a_raise(monkeypatch):
    def _boom(**k):
        raise RuntimeError("groq is down")
    monkeypatch.setattr("interpreter.llm.complete", _boom)
    out = session_review.judge_session(_TRANSCRIPT, state="sent")
    assert out == {"category": "unknown", "severity": None, "summary": ""}


def test_judge_session_malformed_json_is_unknown(monkeypatch):
    monkeypatch.setattr("interpreter.llm.complete", lambda **k: "not json at all")
    out = session_review.judge_session(_TRANSCRIPT, state="sent")
    assert out["category"] == "unknown"


def test_judge_session_threads_tenant_id_for_byok(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "interpreter.llm.complete",
        lambda **k: captured.update(k) or '{"category": "sound", "severity": 0.0, "summary": "x"}',
    )
    session_review.judge_session(_TRANSCRIPT, state="sent", tenant_id="t1")
    assert captured["tenant_id"] == "t1"


def test_format_transcript_skips_blank_turns():
    out = session_review._format_transcript(
        [{"role": "agent", "text": "  "}, {"role": "bot", "text": "hi"}])
    assert out == "BOT: hi"


def test_format_pointers_marks_unanswered():
    out = session_review._format_pointers([{"q": "why?"}, {"q": "what?", "agent_note": "because"}])
    assert "why? -> (unanswered)" in out
    assert "what? -> because" in out


# ── session_review_sweep ─────────────────────────────────────────────────
class _SessionsTable:
    def __init__(self, sb):
        self.sb = sb
        self.filters = []
        self._update = None

    def select(self, *_a):
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def in_(self, field, values):
        self.filters.append(("in", field, values))
        return self

    def is_(self, field, value):
        self.filters.append(("is", field, value))
        return self

    def order(self, *_a):
        return self

    def limit(self, *_a):
        return self

    def update(self, fields):
        self._update = fields
        return self

    def _matches(self, row):
        for op, field, value in self.filters:
            v = row.get(field)
            if op == "eq" and v != value:
                return False
            if op == "in" and v not in value:
                return False
            if op == "is" and value == "null" and v is not None:
                return False
        return True

    def execute(self):
        if self._update is not None:
            for row in self.sb.rows:
                if self._matches(row):
                    row.update(self._update)
            return type("R", (), {"data": None})()
        return type("R", (), {"data": [r for r in self.sb.rows if self._matches(r)]})()


class _SB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "reasoning_sessions"
        return _SessionsTable(self)


def test_session_review_sweep_judges_the_backlog(monkeypatch):
    monkeypatch.setattr(
        session_review, "judge_session",
        lambda transcript, **k: {"category": "sound", "severity": 0.1, "summary": "x"},
    )
    row = {"session_id": "s1", "tenant_id": "t1", "case_number": "C-1",
           "transcript": _TRANSCRIPT, "pointers": [], "draft": "d",
           "state": "sent", "session_analysis": None}
    sb = _SB([row])
    out = sweeps.session_review_sweep(sb, dry_run=False)
    assert out["judged"] == 1
    assert out["by_category"] == {"sound": 1}
    assert row["session_analysis"] == {"category": "sound", "severity": 0.1, "summary": "x"}


def test_session_review_sweep_skips_already_judged_rows():
    row = {"session_id": "s1", "tenant_id": "t1", "case_number": "C-1",
           "transcript": _TRANSCRIPT, "pointers": [], "draft": "d", "state": "sent",
           "session_analysis": {"category": "sound", "severity": 0.0, "summary": "x"}}
    sb = _SB([row])
    out = sweeps.session_review_sweep(sb, dry_run=False)
    assert out["judged"] == 0


def test_session_review_sweep_skips_non_terminal_sessions():
    row = {"session_id": "s1", "tenant_id": "t1", "case_number": "C-1",
           "transcript": _TRANSCRIPT, "pointers": [], "draft": None,
           "state": "clarifying", "session_analysis": None}
    sb = _SB([row])
    out = sweeps.session_review_sweep(sb, dry_run=False)
    assert out["judged"] == 0


def test_session_review_sweep_dry_run_judges_nothing(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("dry run must not call the judge")
    monkeypatch.setattr(session_review, "judge_session", _boom)
    row = {"session_id": "s1", "tenant_id": "t1", "case_number": "C-1",
           "transcript": _TRANSCRIPT, "pointers": [], "draft": "d",
           "state": "abandoned", "session_analysis": None}
    sb = _SB([row])
    out = sweeps.session_review_sweep(sb, dry_run=True)
    assert out["judged"] == 1 and out["dry_run"] is True
    assert row["session_analysis"] is None   # unchanged


def test_session_review_sweep_query_failure_is_a_clean_skip():
    class _Broken(_SB):
        def table(self, name):
            class _T(_SessionsTable):
                def execute(self_):
                    raise RuntimeError("db down")
            return _T(self)
    out = sweeps.session_review_sweep(_Broken([]), dry_run=False)
    assert "error" in out
