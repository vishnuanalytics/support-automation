"""Response Quality Feedback Loop chunk B — interpreter/correction_review.py
(judge_correction) and interpreter/sweeps.py::correction_review_sweep."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import correction_review, sweeps


# ── judge_correction ────────────────────────────────────────────────────
def test_judge_correction_parses_a_clean_verdict(monkeypatch):
    monkeypatch.setattr(
        "interpreter.llm.complete",
        lambda **k: '{"category": "factual", "severity": 0.8, "summary": "fixed the wrong SLA figure"}',
    )
    out = correction_review.judge_correction("draft text", "human text", subject="SLA question")
    assert out == {"category": "factual", "severity": 0.8, "summary": "fixed the wrong SLA figure"}


def test_judge_correction_clamps_severity_out_of_range(monkeypatch):
    monkeypatch.setattr(
        "interpreter.llm.complete",
        lambda **k: '{"category": "tone", "severity": 4.2, "summary": "warmer phrasing"}',
    )
    out = correction_review.judge_correction("d", "h")
    assert out["severity"] == 1.0


def test_judge_correction_unknown_category_falls_back_to_other(monkeypatch):
    monkeypatch.setattr(
        "interpreter.llm.complete",
        lambda **k: '{"category": "sentiment", "severity": 0.5, "summary": "x"}',
    )
    out = correction_review.judge_correction("d", "h")
    assert out["category"] == "other"


def test_judge_correction_missing_input_is_unknown_without_calling_llm(monkeypatch):
    def _boom(**k):
        raise AssertionError("must not call the judge with empty input")
    monkeypatch.setattr("interpreter.llm.complete", _boom)
    assert correction_review.judge_correction("", "human reply")["category"] == "unknown"
    assert correction_review.judge_correction("draft", "")["category"] == "unknown"


def test_judge_correction_judge_failure_is_unknown_not_a_raise(monkeypatch):
    def _boom(**k):
        raise RuntimeError("groq is down")
    monkeypatch.setattr("interpreter.llm.complete", _boom)
    out = correction_review.judge_correction("d", "h")
    assert out == {"category": "unknown", "severity": None, "summary": ""}


def test_judge_correction_malformed_json_is_unknown(monkeypatch):
    monkeypatch.setattr("interpreter.llm.complete", lambda **k: "not json at all")
    out = correction_review.judge_correction("d", "h")
    assert out["category"] == "unknown"


def test_judge_correction_severity_non_numeric_becomes_none(monkeypatch):
    monkeypatch.setattr(
        "interpreter.llm.complete",
        lambda **k: '{"category": "brevity", "severity": "high", "summary": "trimmed it"}',
    )
    out = correction_review.judge_correction("d", "h")
    assert out["category"] == "brevity" and out["severity"] is None


def test_judge_correction_threads_tenant_id_for_byok(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "interpreter.llm.complete",
        lambda **k: captured.update(k) or '{"category": "none", "severity": 0.0, "summary": "typo fix"}',
    )
    correction_review.judge_correction("d", "h", tenant_id="t1")
    assert captured["tenant_id"] == "t1"


# ── correction_review_sweep ──────────────────────────────────────────────
class _RunsTable:
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
        assert name == "runs"
        return _RunsTable(self)


def test_correction_review_sweep_judges_the_backlog(monkeypatch):
    monkeypatch.setattr(
        correction_review, "judge_correction",
        lambda draft, reply, **k: {"category": "factual", "severity": 0.6, "summary": "x"},
    )
    row = {"run_id": "r1", "tenant_id": "t1", "subject": "s", "draft": "d",
           "human_reply": "h", "human_action": "edited", "correction_analysis": None}
    sb = _SB([row])
    out = sweeps.correction_review_sweep(sb, dry_run=False)
    assert out["judged"] == 1
    assert out["by_category"] == {"factual": 1}
    assert row["correction_analysis"] == {"category": "factual", "severity": 0.6, "summary": "x"}


def test_correction_review_sweep_skips_already_judged_rows():
    row = {"run_id": "r1", "tenant_id": "t1", "subject": "s", "draft": "d",
           "human_reply": "h", "human_action": "edited",
           "correction_analysis": {"category": "none", "severity": 0.0, "summary": "typo"}}
    sb = _SB([row])
    out = sweeps.correction_review_sweep(sb, dry_run=False)
    assert out["judged"] == 0


def test_correction_review_sweep_skips_rows_with_a_non_correction_action():
    row = {"run_id": "r1", "tenant_id": "t1", "subject": "s", "draft": "d",
           "human_reply": None, "human_action": "sent_as_is", "correction_analysis": None}
    sb = _SB([row])
    out = sweeps.correction_review_sweep(sb, dry_run=False)
    assert out["judged"] == 0


def test_correction_review_sweep_stores_unknown_verdict_when_reply_is_missing(monkeypatch):
    # human_action qualifies but human_reply is blank — the query itself
    # doesn't filter on this, so the row reaches judge_correction, which
    # falls back to category="unknown" (exercised for real, no mock).
    row = {"run_id": "r1", "tenant_id": "t1", "subject": "s", "draft": "d",
           "human_reply": None, "human_action": "edited", "correction_analysis": None}
    sb = _SB([row])
    out = sweeps.correction_review_sweep(sb, dry_run=False)
    assert out["judged"] == 1
    assert row["correction_analysis"]["category"] == "unknown"


def test_correction_review_sweep_dry_run_judges_nothing(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("dry run must not call the judge")
    monkeypatch.setattr(correction_review, "judge_correction", _boom)
    row = {"run_id": "r1", "tenant_id": "t1", "subject": "s", "draft": "d",
           "human_reply": "h", "human_action": "rewrote", "correction_analysis": None}
    sb = _SB([row])
    out = sweeps.correction_review_sweep(sb, dry_run=True)
    assert out["judged"] == 1 and out["dry_run"] is True
    assert row["correction_analysis"] is None   # unchanged


def test_correction_review_sweep_query_failure_is_a_clean_skip():
    class _Broken(_SB):
        def table(self, name):
            class _T(_RunsTable):
                def execute(self_):
                    raise RuntimeError("db down")
            return _T(self)
    out = sweeps.correction_review_sweep(_Broken([]), dry_run=False)
    assert "error" in out
