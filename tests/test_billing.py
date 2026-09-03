"""P9 — usage & billing dashboard: interpreter/billing.py + runs.py's token roll-up."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import billing
from interpreter.runs import _token_usage


def test_estimate_cost_groq_only_is_free():
    assert billing.estimate_cost_usd({"openai/gpt-oss-120b": 500_000}) == 0.0


def test_estimate_cost_mixed_model_blends_rates():
    cost = billing.estimate_cost_usd({
        "openai/gpt-oss-120b": 1_000_000,      # $0
        "claude-sonnet-5": 1_000_000,          # $4.5 / 1M
    })
    assert cost == 4.5


def test_estimate_cost_unlisted_model_defaults_to_zero():
    assert billing.estimate_cost_usd({"some-new-model": 999_999}) == 0.0


def test_usage_summary_aggregates_runs_tokens_and_daily_buckets():
    rows = [
        {"tokens_total": 100, "tokens_by_model": {"openai/gpt-oss-120b": 100},
         "created_at": "2026-09-01T10:00:00+00:00"},
        {"tokens_total": 200, "tokens_by_model": {"openai/gpt-oss-120b": 150,
                                                   "claude-sonnet-5": 50},
         "created_at": "2026-09-01T18:00:00+00:00"},
        {"tokens_total": 50, "tokens_by_model": {"claude-haiku-4-5": 50},
         "created_at": "2026-09-02T09:00:00+00:00"},
    ]
    s = billing.usage_summary(rows, "free", "2026-09-01T00:00:00+00:00",
                              "2026-10-01T00:00:00+00:00")

    assert s["runs_count"] == 3
    assert s["tokens_total"] == 350
    assert s["tokens_by_model"] == {
        "openai/gpt-oss-120b": 250, "claude-sonnet-5": 50, "claude-haiku-4-5": 50,
    }
    assert s["daily"] == [
        {"date": "2026-09-01", "runs": 2, "tokens": 300},
        {"date": "2026-09-02", "runs": 1, "tokens": 50},
    ]
    # free plan: 200 runs / 500_000 tokens
    assert s["pct_runs_used"] == round(3 / 200 * 100, 1)
    assert s["pct_tokens_used"] == round(350 / 500_000 * 100, 1)
    assert s["estimated_cost_usd"] > 0   # sonnet + haiku tokens aren't free


def test_usage_summary_pro_plan_has_no_pct_limits():
    s = billing.usage_summary([], "pro", "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
    assert s["limits"] == {"runs": None, "tokens": None}
    assert s["pct_runs_used"] is None
    assert s["pct_tokens_used"] is None
    assert s["runs_count"] == 0
    assert s["daily"] == []


def test_usage_summary_unknown_plan_falls_back_to_free_limits():
    s = billing.usage_summary([], "not-a-real-plan", "2026-09-01T00:00:00+00:00",
                              "2026-10-01T00:00:00+00:00")
    assert s["limits"] == billing.PLAN_LIMITS["free"]


def test_month_bounds_explicit_period():
    label, start, end = billing.month_bounds("2026-09")
    assert label == "2026-09"
    assert start.startswith("2026-09-01")
    assert end.startswith("2026-10-01")


def test_month_bounds_december_rolls_into_next_year():
    label, start, end = billing.month_bounds("2026-12")
    assert label == "2026-12"
    assert start.startswith("2026-12-01")
    assert end.startswith("2027-01-01")


def test_token_usage_sums_and_splits_by_model():
    trace = [
        {"type": "classify", "data": {"tokens": {"total": 120}, "model": "openai/gpt-oss-20b"}},
        {"type": "draft", "data": {"tokens": {"total": 380}, "model": "claude-sonnet-5"}},
        {"type": "sf_writeback", "data": {}},          # no tokens -> ignored
        {"type": "ai_prompt", "data": {"tokens": None, "model": "openai/gpt-oss-20b"}},
    ]
    total, by_model = _token_usage(trace)
    assert total == 500
    assert by_model == {"openai/gpt-oss-20b": 120, "claude-sonnet-5": 380}


def test_token_usage_empty_trace_is_zero_not_a_crash():
    total, by_model = _token_usage([])
    assert total == 0
    assert by_model == {}


# ── Phase 28 step 3: check_and_warn (warn-only quota enforcement) ──────
class _Q:
    def __init__(self, rows, sink, table_name):
        self._rows = rows
        self._sink = sink
        self._table_name = table_name

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def lt(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def insert(self, row):
        self._sink.setdefault(self._table_name, []).append(row)
        return self

    def execute(self):
        return type("R", (), {"data": list(self._rows)})


class _SB:
    def __init__(self, tables=None):
        self.tables = tables or {}
        self.inserted: dict[str, list[dict]] = {}

    def table(self, name):
        return _Q(self.tables.get(name, []), self.inserted, name)


class _BrokenSB:
    def table(self, name):
        raise RuntimeError("db is down")


def _period_start():
    _, start, _ = billing.month_bounds(None)
    return start


def test_check_and_warn_unlimited_plan_is_a_noop():
    sb = _SB({"tenants": [{"plan": "pro"}]})
    level = billing.check_and_warn(sb, "t1")
    assert level is None
    assert sb.inserted == {}


def test_check_and_warn_under_threshold_is_a_noop():
    sb = _SB({"tenants": [{"plan": "free"}], "runs": []})
    level = billing.check_and_warn(sb, "t1")
    assert level is None
    assert sb.inserted == {}


def test_check_and_warn_crosses_warning_threshold():
    runs = [{"tokens_total": 0, "tokens_by_model": {}, "created_at": _period_start()}
            for _ in range(160)]  # 160/200 = 80%
    sb = _SB({"tenants": [{"plan": "free"}], "runs": runs, "audit_log": [],
              "tenant_integrations": []})
    level = billing.check_and_warn(sb, "t1")
    assert level == "warning"
    assert len(sb.inserted["audit_log"]) == 1
    row = sb.inserted["audit_log"][0]
    assert row["action"] == "billing.quota_warning"
    assert row["tenant_id"] == "t1"
    assert row["metadata"]["pct"] == 80.0


def test_check_and_warn_crosses_exceeded_threshold():
    runs = [{"tokens_total": 0, "tokens_by_model": {}, "created_at": _period_start()}
            for _ in range(200)]  # 200/200 = 100%
    sb = _SB({"tenants": [{"plan": "free"}], "runs": runs, "audit_log": [],
              "tenant_integrations": []})
    level = billing.check_and_warn(sb, "t1")
    assert level == "exceeded"
    assert sb.inserted["audit_log"][0]["action"] == "billing.quota_exceeded"


def test_check_and_warn_dedups_within_the_same_period():
    period_label, _, _ = billing.month_bounds(None)
    runs = [{"tokens_total": 0, "tokens_by_model": {}, "created_at": _period_start()}
            for _ in range(160)]
    existing = [{"event_id": 1, "metadata": {"period": period_label}}]
    sb = _SB({"tenants": [{"plan": "free"}], "runs": runs, "audit_log": existing,
              "tenant_integrations": []})
    level = billing.check_and_warn(sb, "t1")
    assert level == "warning"
    assert sb.inserted == {}   # already warned this period -- no duplicate


def test_check_and_warn_posts_to_slack_when_a_digest_channel_is_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "interpreter.slack.post_message",
        lambda text, **kw: calls.append((text, kw)),
    )
    runs = [{"tokens_total": 0, "tokens_by_model": {}, "created_at": _period_start()}
            for _ in range(200)]
    sb = _SB({"tenants": [{"plan": "free"}], "runs": runs, "audit_log": [],
              "tenant_integrations": [{"config": {"digest": {"channel": "#billing"}}}]})
    billing.check_and_warn(sb, "t1")
    assert len(calls) == 1
    text, kw = calls[0]
    assert "Billing" in text
    assert kw["channel"] == "#billing" and kw["tenant_id"] == "t1"


def test_check_and_warn_never_raises_on_a_broken_client():
    assert billing.check_and_warn(_BrokenSB(), "t1") is None
