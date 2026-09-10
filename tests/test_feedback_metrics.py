"""Response Quality Feedback Loop chunk F — interpreter/feedback_metrics.py."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import feedback_metrics


def _iso(days_ago: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    @property
    def not_(self):
        return self

    def is_(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": list(self._rows)})()


class _SB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self.tables.get(name, []))


def test_compute_is_all_zeros_for_an_empty_tenant():
    out = feedback_metrics.compute(_SB({}), "t1")
    assert out["window_days"] == 30
    assert out["corrections"] == {"total_judged": 0, "by_category": {}, "avg_severity": None, "recent": []}
    assert out["sessions"] == {"total_judged": 0, "by_category": {}, "avg_severity": None, "recent": []}
    assert out["exemplars"] == {"draft_runs_sampled": 0, "draft_runs_with_exemplars": 0, "usage_rate": None}
    assert out["gate_tuning"]["pending"] == 0 and out["gate_tuning"]["recent"] == []


def test_compute_every_table_read_degrades_to_zero_on_failure():
    class _Broken:
        def table(self, name):
            class _T:
                def select(self, *a, **k): return self
                def eq(self, *a, **k): return self
                def gte(self, *a, **k): return self
                def order(self, *a, **k): return self
                def limit(self, *a, **k): return self
                @property
                def not_(self): return self
                def is_(self, *a, **k): return self
                def execute(self): raise RuntimeError("db down")
            return _T()
    out = feedback_metrics.compute(_Broken(), "t1")
    assert out["corrections"]["total_judged"] == 0
    assert out["sessions"]["total_judged"] == 0
    assert out["exemplars"]["draft_runs_sampled"] == 0
    assert out["gate_tuning"]["pending"] == 0


def test_compute_corrections_tallies_category_and_avg_severity():
    runs = [
        {"run_id": "r1", "subject": "s1", "created_at": _iso(),
         "correction_analysis": {"category": "policy", "severity": 0.9, "summary": "x"}},
        {"run_id": "r2", "subject": "s2", "created_at": _iso(),
         "correction_analysis": {"category": "policy", "severity": 0.5, "summary": "y"}},
        {"run_id": "r3", "subject": "s3", "created_at": _iso(),
         "correction_analysis": {"category": "none", "severity": 0.0, "summary": "z"}},
        {"run_id": "r4", "subject": "s4", "created_at": _iso(),
         "correction_analysis": {"category": "unknown", "severity": None, "summary": ""}},
    ]
    out = feedback_metrics.compute(_SB({"runs": runs}), "t1")
    corr = out["corrections"]
    assert corr["total_judged"] == 4
    assert corr["by_category"] == {"policy": 2, "none": 1, "unknown": 1}
    assert corr["avg_severity"] == round((0.9 + 0.5 + 0.0) / 3, 3)
    assert len(corr["recent"]) == 4
    assert corr["recent"][0]["run_id"] == "r1"


def test_compute_sessions_tallies_category_and_avg_severity():
    sessions = [
        {"session_id": "s1", "case_number": "C-1", "updated_at": _iso(),
         "session_analysis": {"category": "redundant", "severity": 0.4, "summary": "x"}},
        {"session_id": "s2", "case_number": "C-2", "updated_at": _iso(),
         "session_analysis": {"category": "sound", "severity": 0.1, "summary": "y"}},
    ]
    out = feedback_metrics.compute(_SB({"reasoning_sessions": sessions}), "t1")
    sess = out["sessions"]
    assert sess["total_judged"] == 2
    assert sess["by_category"] == {"redundant": 1, "sound": 1}
    assert sess["avg_severity"] == round((0.4 + 0.1) / 2, 3)


def test_compute_exemplar_usage_rate():
    # the fake's `.table("runs")` can't distinguish the corrections query
    # from the exemplars query (both hit "runs"), so each row also carries
    # harmless corrections-shaped fields for the first query to read.
    def _trace(used):
        return [{"node_id": "d", "type": "draft", "data": {"exemplars_used": used}}]
    def _row(i, used):
        return {"run_id": f"r{i}", "subject": None, "created_at": _iso(),
                "correction_analysis": None, "trace": _trace(used)}
    runs = [_row(1, 1), _row(2, 2), _row(3, 0), {**_row(4, 0), "trace": []}]
    out = feedback_metrics.compute(_SB({"runs": runs}), "t1")
    ex = out["exemplars"]
    assert ex["draft_runs_sampled"] == 4
    assert ex["draft_runs_with_exemplars"] == 2
    assert ex["usage_rate"] == 0.5


def test_compute_exemplar_usage_rate_none_when_no_draft_runs():
    out = feedback_metrics.compute(_SB({"runs": []}), "t1")
    assert out["exemplars"]["usage_rate"] is None


def test_compute_gate_tuning_counts_by_status():
    ars = [
        {"id": "a1", "status": "pending", "created_at": _iso(),
         "payload": {"title": "Lower basic", "tier": "basic",
                     "current_threshold": 0.5, "suggested_threshold": 0.45}},
        {"id": "a2", "status": "approved", "created_at": _iso()},
        {"id": "a3", "status": "approved", "created_at": _iso()},
        {"id": "a4", "status": "rejected", "created_at": _iso()},
    ]
    out = feedback_metrics.compute(_SB({"action_requests": ars}), "t1")
    gt = out["gate_tuning"]
    assert gt["pending"] == 1 and gt["approved"] == 2 and gt["rejected"] == 1
    assert gt["recent"][0]["id"] == "a1" and gt["recent"][0]["tier"] == "basic"


def test_compute_respects_custom_days_window():
    out = feedback_metrics.compute(_SB({}), "t1", days=7)
    assert out["window_days"] == 7
