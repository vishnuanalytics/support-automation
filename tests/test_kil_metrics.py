"""KIL-f — Knowledge Integrity Loop metrics."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import kil_metrics


def _iso(days_ago: float) -> str:
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

    def lt(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": list(self._rows)})


class _SB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self.tables.get(name, []))


def test_compute_review_and_kb_funnel():
    tasks = [
        {"trigger": "contradicts", "status": "correct", "created_at": _iso(10),
         "reviewed_at": _iso(9)},                                  # 24h to review
        {"trigger": "contradicts", "status": "dismissed", "created_at": _iso(8),
         "reviewed_at": _iso(7.5)},                                # false flag
        {"trigger": "novel", "status": "wrong", "created_at": _iso(6),
         "reviewed_at": _iso(5)},                                  # agent was off
        {"trigger": "sample", "status": "open", "created_at": _iso(1)},
    ]
    kb = [
        {"status": "provisional", "origin": "review_writeback", "created_at": _iso(2),
         "supersedes_entry_id": None},
        {"status": "active", "origin": "review_writeback", "created_at": _iso(20),
         "supersedes_entry_id": "x"},
        {"status": "superseded", "origin": "manual", "created_at": _iso(40),
         "supersedes_entry_id": None},
        {"status": "active", "origin": "manual", "created_at": _iso(30),
         "supersedes_entry_id": None},
    ]
    m = kil_metrics.compute(_SB({"review_tasks": tasks, "kb_entries": kb}),
                            "00000000-0000-0000-0000-000000000000", days=30)

    r = m["review"]
    assert r["total"] == 4 and r["open"] == 1 and r["resolved"] == 3
    assert r["by_trigger"]["contradicts"] == 2
    # correct / (correct + wrong) = 1/2
    assert r["flag_precision"] == 0.5
    # dismissed / resolved = 1/3
    assert r["false_flag_rate"] == 0.333
    assert r["agent_correction_rate"] == 0.333
    assert r["median_time_to_review_h"] is not None

    k = m["kb_writeback"]
    assert k["entries"] == 2 and k["provisional"] == 1 and k["active"] == 1
    assert k["superseded"] == 1
    assert k["promotion_rate"] == 0.5

    assert m["knowledge_freshness_days"] is not None   # median of the 2 active entries
    assert isinstance(m["weekly"], list)


def test_empty_tenant_is_all_zeros_not_a_crash():
    m = kil_metrics.compute(_SB({}), "t", days=7)
    assert m["review"]["total"] == 0
    assert m["review"]["flag_precision"] is None
    assert m["kb_writeback"]["entries"] == 0
    assert m["knowledge_freshness_days"] is None


# ── P8a: the weekly learning report ────────────────────────────────────
def test_compute_window_scores_only_resolved_flags():
    tasks = [
        {"trigger": "contradicts", "status": "correct", "created_at": _iso(10)},
        {"trigger": "contradicts", "status": "correct", "created_at": _iso(10)},
        {"trigger": "novel", "status": "wrong", "created_at": _iso(9)},
        {"trigger": "sample", "status": "dismissed", "created_at": _iso(9)},
        {"trigger": "contradicts", "status": "open", "created_at": _iso(8)},
    ]
    w = kil_metrics.compute_window(_SB({"review_tasks": tasks}), "t",
                                   start_days_ago=14, end_days_ago=7)
    # correct / (correct + wrong) = 2/3
    assert w["review"]["flag_precision"] == 0.667
    # dismissed / resolved = 1/4
    assert w["review"]["false_flag_rate"] == 0.25
    assert w["knowledge_freshness_days"] is None


def test_digest_surfaces_recurring_contradictions_and_kb_changes():
    tasks = [
        {"trigger": "contradicts", "status": "open", "created_at": _iso(3),
         "verdict": {"salient": ["Refunds take 5 business days"]}},
        {"trigger": "contradicts", "status": "correct", "created_at": _iso(4),
         "verdict": {"salient": ["refunds take 5 business days"]}},   # same claim, cased
        {"trigger": "novel", "status": "open", "created_at": _iso(5),
         "verdict": {"salient": ["EU data stays in Frankfurt"]}},     # seen once — dropped
    ]
    kb = [{"title": "Refund SLA", "status": "provisional", "created_at": _iso(1)}]
    d = kil_metrics.digest(_SB({"review_tasks": tasks, "kb_entries": kb}), "t", weeks=4)

    assert set(d) >= {"week_of", "this_week", "deltas", "top_contradictions",
                      "recent_kb_changes"}
    top = d["top_contradictions"]
    assert top and top[0]["claim"] == "refunds take 5 business days"
    assert top[0]["count"] == 2
    assert all(c["count"] >= 2 for c in top)          # the one-off is filtered
    assert d["recent_kb_changes"][0]["title"] == "Refund SLA"
    assert "flagged" in d["deltas"]


def test_render_digest_is_slack_markdown():
    tasks = [{"trigger": "contradicts", "status": "correct", "created_at": _iso(2),
              "reviewed_at": _iso(1), "verdict": {"salient": ["a claim", "a claim"]}}]
    d = kil_metrics.digest(_SB({"review_tasks": tasks, "kb_entries": []}), "t")
    text = kil_metrics.render_digest(d)
    assert isinstance(text, str)
    assert text.startswith("*Knowledge Integrity — week ")
    assert "Flag precision:" in text and "KB changes:" in text


def test_pct_formats_ratios_and_none():
    assert kil_metrics._pct(None) == "—"
    assert kil_metrics._pct(0.5) == "50%"
    assert kil_metrics._pct(1) == "100%"
