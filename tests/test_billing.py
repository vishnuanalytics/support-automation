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


_FREE_LIMITS = {"runs": 200, "tokens": 500_000}
_PRO_LIMITS = {"runs": None, "tokens": None}


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
    s = billing.usage_summary(rows, "free", _FREE_LIMITS, "2026-09-01T00:00:00+00:00",
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
    assert s["by_flow"] == []   # none of these rows carry a flow_id


def test_usage_summary_by_flow_groups_sorts_and_names_flows():
    rows = [
        {"flow_id": "f1", "tokens_total": 100, "tokens_by_model": {"openai/gpt-oss-120b": 100},
         "created_at": "2026-09-01T10:00:00+00:00"},
        {"flow_id": "f1", "tokens_total": 50, "tokens_by_model": {"claude-sonnet-5": 50},
         "created_at": "2026-09-01T11:00:00+00:00"},
        {"flow_id": "f2", "tokens_total": 400, "tokens_by_model": {"claude-sonnet-5": 400},
         "created_at": "2026-09-02T09:00:00+00:00"},
        {"flow_id": None, "tokens_total": 10, "tokens_by_model": {}, "created_at": None},
    ]
    s = billing.usage_summary(rows, "free", _FREE_LIMITS, "2026-09-01T00:00:00+00:00",
                              "2026-10-01T00:00:00+00:00", flow_names={"f1": "Support Autoreply"})

    assert s["runs_count"] == 4          # the flow_id=None row still counts toward the total
    # sorted by tokens desc: f2 (400) before f1 (150)
    assert [f["flow_id"] for f in s["by_flow"]] == ["f2", "f1"]
    f2, f1 = s["by_flow"]
    assert f2 == {"flow_id": "f2", "name": "f2", "runs": 1, "tokens": 400,
                  "estimated_cost_usd": billing.estimate_cost_usd({"claude-sonnet-5": 400})}
    assert f1["name"] == "Support Autoreply"   # named via flow_names, unlike f2 (falls back to id)
    assert f1["runs"] == 2 and f1["tokens"] == 150
    # a run with no flow_id is dropped from by_flow, not crashed on
    assert sum(f["runs"] for f in s["by_flow"]) == 3


# ── Billing chunk E (2026-09-10): BYOK-excluded quota/overage ──────────
def test_usage_summary_excludes_a_fully_byok_run_from_billable_counts():
    rows = [
        {"tokens_total": 100, "tokens_by_model": {"claude-sonnet-5": 100},
         "tokens_by_key_source": {"platform": 100}, "created_at": "2026-09-01T10:00:00+00:00"},
        {"tokens_total": 300, "tokens_by_model": {"claude-sonnet-5": 300},
         "tokens_by_key_source": {"byok": 300}, "created_at": "2026-09-02T10:00:00+00:00"},
    ]
    s = billing.usage_summary(rows, "free", _FREE_LIMITS, "2026-09-01T00:00:00+00:00",
                              "2026-10-01T00:00:00+00:00")
    assert s["runs_count"] == 2 and s["tokens_total"] == 400          # true totals, unaffected
    assert s["billable_runs_count"] == 1 and s["billable_tokens_total"] == 100  # the byok run excluded
    assert s["pct_runs_used"] == round(1 / 200 * 100, 1)
    assert s["pct_tokens_used"] == round(100 / 500_000 * 100, 1)


def test_usage_summary_a_run_with_any_platform_call_still_counts():
    """A run that mixed a platform-paid node with a BYOK node still cost
    the platform something — it counts, unlike a fully-BYOK run."""
    rows = [{"tokens_total": 150, "tokens_by_key_source": {"platform": 50, "byok": 100},
            "created_at": "2026-09-01T10:00:00+00:00"}]
    s = billing.usage_summary(rows, "free", _FREE_LIMITS, "2026-09-01T00:00:00+00:00",
                              "2026-10-01T00:00:00+00:00")
    assert s["billable_runs_count"] == 1
    assert s["billable_tokens_total"] == 50   # only the platform-paid share


def test_usage_summary_a_run_predating_key_source_tracking_is_fully_billable():
    """A row recorded before this column existed has no tokens_by_key_source
    at all — must count as fully platform-billable, not silently zeroed."""
    rows = [{"tokens_total": 200, "created_at": "2026-08-01T10:00:00+00:00"}]
    s = billing.usage_summary(rows, "free", _FREE_LIMITS, "2026-08-01T00:00:00+00:00",
                              "2026-09-01T00:00:00+00:00")
    assert s["billable_runs_count"] == 1
    assert s["billable_tokens_total"] == 200


def test_usage_summary_all_byok_never_trips_the_quota():
    rows = [{"tokens_total": 999_999, "tokens_by_key_source": {"byok": 999_999},
            "created_at": "2026-09-01T10:00:00+00:00"} for _ in range(500)]
    s = billing.usage_summary(rows, "free", _FREE_LIMITS, "2026-09-01T00:00:00+00:00",
                              "2026-10-01T00:00:00+00:00")
    assert s["runs_count"] == 500                # a lot of real activity
    assert s["billable_runs_count"] == 0          # none of it billable
    assert s["pct_runs_used"] == 0.0 and s["pct_tokens_used"] == 0.0


def test_usage_summary_pro_plan_has_no_pct_limits():
    s = billing.usage_summary([], "pro", _PRO_LIMITS, "2026-09-01T00:00:00+00:00",
                              "2026-10-01T00:00:00+00:00")
    assert s["limits"] == {"runs": None, "tokens": None}
    assert s["pct_runs_used"] is None
    assert s["pct_tokens_used"] is None
    assert s["runs_count"] == 0
    assert s["daily"] == []


# ── Billing chunk F (2026-09-10): resolve_checkout_plan (BYOK pricing) ──
_STARTER_PLAN = {
    "plan_id": "p-starter", "slug": "starter",
    "razorpay_plan_id": "plan_std", "razorpay_plan_id_byok": "plan_byok",
    "stripe_price_id": "price_std", "stripe_price_id_byok": "price_byok",
}


def test_resolve_checkout_plan_uses_byok_price_when_tenant_has_a_key(monkeypatch):
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_has_byok", lambda tid: True)
    plan, applied = billing.resolve_checkout_plan(_STARTER_PLAN, "razorpay", "t1")
    assert applied is True
    assert plan["razorpay_plan_id"] == "plan_byok"
    assert plan["stripe_price_id"] == "price_byok"   # both swapped, whichever provider is used


def test_resolve_checkout_plan_stripe_provider_swaps_stripe_id(monkeypatch):
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_has_byok", lambda tid: True)
    plan, applied = billing.resolve_checkout_plan(_STARTER_PLAN, "stripe", "t1")
    assert applied is True and plan["stripe_price_id"] == "price_byok"


def test_resolve_checkout_plan_no_byok_key_uses_standard_price(monkeypatch):
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_has_byok", lambda tid: False)
    plan, applied = billing.resolve_checkout_plan(_STARTER_PLAN, "razorpay", "t1")
    assert applied is False
    assert plan == _STARTER_PLAN   # unchanged


def test_resolve_checkout_plan_falls_back_when_no_byok_price_exists(monkeypatch):
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_has_byok", lambda tid: True)
    plan_no_byok = {"plan_id": "p-free", "slug": "free",
                    "razorpay_plan_id": None, "razorpay_plan_id_byok": None}
    plan, applied = billing.resolve_checkout_plan(plan_no_byok, "razorpay", "t1")
    assert applied is False
    assert plan == plan_no_byok


def test_plan_limits_reads_included_runs_and_tokens_off_a_plan_row():
    assert billing.plan_limits({"included_runs": 200, "included_tokens": 500_000}) == _FREE_LIMITS
    assert billing.plan_limits({"included_runs": None, "included_tokens": None}) == _PRO_LIMITS


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


def test_token_usage_sums_and_splits_by_model_and_node():
    trace = [
        {"type": "classify", "data": {"tokens": {"total": 120}, "model": "openai/gpt-oss-20b",
                                       "key_source": "platform"}},
        {"type": "draft", "data": {"tokens": {"total": 380}, "model": "claude-sonnet-5",
                                    "key_source": "byok"}},
        {"type": "draft", "data": {"tokens": {"total": 20}, "model": "claude-sonnet-5",
                                    "key_source": "byok"}},
        {"type": "sf_writeback", "data": {}},          # no tokens -> ignored
        {"type": "ai_prompt", "data": {"tokens": None, "model": "openai/gpt-oss-20b"}},
    ]
    total, by_model, by_node, by_key_source = _token_usage(trace)
    assert total == 520
    assert by_model == {"openai/gpt-oss-20b": 120, "claude-sonnet-5": 400}
    assert by_node == {"classify": 120, "draft": 400}
    assert by_key_source == {"platform": 120, "byok": 400}


def test_token_usage_missing_key_source_defaults_to_platform():
    """A trace entry from before chunk E (or a node type that doesn't set
    key_source, e.g. extract/clarify) must count as platform-billable, not
    silently vanish from the total or get miscounted as BYOK."""
    trace = [{"type": "classify", "data": {"tokens": {"total": 50}, "model": "openai/gpt-oss-20b"}}]
    _, _, _, by_key_source = _token_usage(trace)
    assert by_key_source == {"platform": 50}


def test_token_usage_empty_trace_is_zero_not_a_crash():
    total, by_model, by_node, by_key_source = _token_usage([])
    assert total == 0
    assert by_model == {}
    assert by_node == {}
    assert by_key_source == {}


def test_usage_summary_by_node_splits_cost_proportionally():
    rows = [
        {"tokens_total": 300, "tokens_by_model": {"claude-sonnet-5": 300},
         "tokens_by_node": {"draft": 200, "classify": 100},
         "created_at": "2026-09-01T10:00:00+00:00"},
        {"tokens_total": 100, "tokens_by_model": {"claude-sonnet-5": 100},
         "tokens_by_node": {"draft": 100},
         "created_at": "2026-09-02T10:00:00+00:00"},
    ]
    s = billing.usage_summary(rows, "pro", _PRO_LIMITS, "2026-09-01T00:00:00+00:00",
                              "2026-10-01T00:00:00+00:00")
    by_node = {n["node"]: n for n in s["by_node"]}
    assert by_node["draft"]["tokens"] == 300
    assert by_node["classify"]["tokens"] == 100
    # sorted tokens desc: draft first
    assert [n["node"] for n in s["by_node"]] == ["draft", "classify"]
    overall = s["estimated_cost_usd"]
    assert by_node["draft"]["estimated_cost_usd"] == round(overall * 300 / 400, 4)


def test_usage_summary_by_node_empty_when_no_rows_carry_it():
    s = billing.usage_summary(
        [{"tokens_total": 50, "tokens_by_model": {}, "created_at": None}],
        "pro", _PRO_LIMITS, "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
    assert s["by_node"] == []


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


# `get_plan_for_tenant` now does tenants.plan_id -> plans row; `_Q.eq()` is a
# no-op passthrough, so each fixture's `plans` bucket just needs the one row
# the test actually wants (the fake doesn't really filter by plan_id).
_FREE_PLAN = {"plan_id": "p-free", "slug": "free", "included_runs": 200, "included_tokens": 500_000}
_PRO_PLAN = {"plan_id": "p-pro", "slug": "pro", "included_runs": None, "included_tokens": None}


def test_check_and_warn_unlimited_plan_is_a_noop():
    sb = _SB({"tenants": [{"plan_id": "p-pro"}], "plans": [_PRO_PLAN]})
    level = billing.check_and_warn(sb, "t1")
    assert level is None
    assert sb.inserted == {}


def test_check_and_warn_under_threshold_is_a_noop():
    sb = _SB({"tenants": [{"plan_id": "p-free"}], "plans": [_FREE_PLAN], "runs": []})
    level = billing.check_and_warn(sb, "t1")
    assert level is None
    assert sb.inserted == {}


def test_check_and_warn_crosses_warning_threshold():
    runs = [{"tokens_total": 0, "tokens_by_model": {}, "created_at": _period_start()}
            for _ in range(160)]  # 160/200 = 80%
    sb = _SB({"tenants": [{"plan_id": "p-free"}], "plans": [_FREE_PLAN], "runs": runs,
              "audit_log": [], "tenant_integrations": []})
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
    sb = _SB({"tenants": [{"plan_id": "p-free"}], "plans": [_FREE_PLAN], "runs": runs,
              "audit_log": [], "tenant_integrations": []})
    level = billing.check_and_warn(sb, "t1")
    assert level == "exceeded"
    assert sb.inserted["audit_log"][0]["action"] == "billing.quota_exceeded"


def test_check_and_warn_dedups_within_the_same_period():
    period_label, _, _ = billing.month_bounds(None)
    runs = [{"tokens_total": 0, "tokens_by_model": {}, "created_at": _period_start()}
            for _ in range(160)]
    existing = [{"event_id": 1, "metadata": {"period": period_label}}]
    sb = _SB({"tenants": [{"plan_id": "p-free"}], "plans": [_FREE_PLAN], "runs": runs,
              "audit_log": existing, "tenant_integrations": []})
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
    sb = _SB({"tenants": [{"plan_id": "p-free"}], "plans": [_FREE_PLAN], "runs": runs,
              "audit_log": [],
              "tenant_integrations": [{"config": {"digest": {"channel": "#billing"}}}]})
    billing.check_and_warn(sb, "t1")
    assert len(calls) == 1
    text, kw = calls[0]
    assert "Billing" in text
    assert kw["channel"] == "#billing" and kw["tenant_id"] == "t1"


def test_check_and_warn_never_raises_on_a_broken_client():
    assert billing.check_and_warn(_BrokenSB(), "t1") is None


# ── Billing-foundation chunk (2026-09-10): plans as data, not a hardcoded dict ──
def test_get_plan_for_tenant_resolves_via_plan_id():
    sb = _SB({"tenants": [{"plan_id": "p-pro"}], "plans": [_PRO_PLAN]})
    assert billing.get_plan_for_tenant("t1", sb) == _PRO_PLAN


def test_get_plan_for_tenant_falls_back_to_free_plan_row_when_tenant_missing():
    sb = _SB({"tenants": [], "plans": [_FREE_PLAN]})
    assert billing.get_plan_for_tenant("ghost-tenant", sb)["slug"] == "free"


def test_get_plan_for_tenant_falls_back_to_hardcoded_limits_if_plans_table_is_empty():
    sb = _SB({"tenants": [{"plan_id": "p-free"}], "plans": []})
    plan = billing.get_plan_for_tenant("t1", sb)
    assert plan["slug"] == "free"
    assert billing.plan_limits(plan) == _FREE_LIMITS


# ── assert_not_locked (2026-09-11): the 7-day lock + a trial free-credit cap ──
# `openai/gpt-oss-120b` is a real MODELS entry (interpreter/llm.py) that
# maps to provider "groq" -- used below so the BYOK-provider-matching
# tests exercise the real `llm.provider()` mapping, not a stub.
def _runs_at_cap(model="openai/gpt-oss-120b"):
    return [{"tokens_total": 0, "tokens_by_model": {model: 0}, "tokens_by_key_source": {},
              "created_at": _period_start()} for _ in range(200)]  # 200/200 included_runs


def test_assert_not_locked_no_tenant_id_is_a_noop():
    billing.assert_not_locked(None, _SB())  # must not raise / must not touch the client


def test_assert_not_locked_active_tenant_ignores_usage(monkeypatch):
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_byok_providers", lambda tid: set())
    sb = _SB({"tenants": [{"billing_status": "active", "plan_id": "p-free"}],
              "plans": [_FREE_PLAN], "runs": _runs_at_cap()})
    billing.assert_not_locked("t1", sb)  # over the "cap" but not trialing -- not this gate's job


def test_assert_not_locked_locked_status_always_raises_even_with_byok(monkeypatch):
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_byok_providers", lambda tid: {"groq"})
    sb = _SB({"tenants": [{"billing_status": "locked", "plan_id": "p-free"}], "plans": [_FREE_PLAN]})
    try:
        billing.assert_not_locked("t1", sb)
        assert False, "expected BillingLockedError"
    except billing.BillingLockedError:
        pass  # the 7-day trial clock isn't extendable by BYOK


def test_assert_not_locked_trialing_under_cap_is_fine(monkeypatch):
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_byok_providers", lambda tid: set())
    sb = _SB({"tenants": [{"billing_status": "trialing", "plan_id": "p-free"}],
              "plans": [_FREE_PLAN], "runs": []})
    billing.assert_not_locked("t1", sb)


def test_assert_not_locked_trialing_over_cap_no_byok_raises(monkeypatch):
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_byok_providers", lambda tid: set())
    sb = _SB({"tenants": [{"billing_status": "trialing", "plan_id": "p-free"}],
              "plans": [_FREE_PLAN], "runs": _runs_at_cap()})
    try:
        billing.assert_not_locked("t1", sb)
        assert False, "expected BillingLockedError"
    except billing.BillingLockedError as e:
        assert "own LLM key" in str(e)


def test_assert_not_locked_trialing_over_cap_with_byok_for_the_used_provider_is_fine(monkeypatch):
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_byok_providers", lambda tid: {"groq"})
    sb = _SB({"tenants": [{"billing_status": "trialing", "plan_id": "p-free"}],
              "plans": [_FREE_PLAN], "runs": _runs_at_cap(model="openai/gpt-oss-120b")})
    billing.assert_not_locked("t1", sb)  # groq usage actually covered by the tenant's groq key


def test_assert_not_locked_trialing_byok_for_an_unused_provider_still_raises(monkeypatch):
    """The vulnerability this fix closes: a key for a provider the
    tenant's runs never actually called must NOT exempt it from the cap
    -- every real call still bills the platform's own key."""
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_byok_providers", lambda tid: {"openrouter"})
    sb = _SB({"tenants": [{"billing_status": "trialing", "plan_id": "p-free"}],
              "plans": [_FREE_PLAN], "runs": _runs_at_cap(model="openai/gpt-oss-120b")})  # groq usage
    try:
        billing.assert_not_locked("t1", sb)
        assert False, "expected BillingLockedError"
    except billing.BillingLockedError:
        pass


def test_assert_not_locked_trialing_unlimited_plan_never_capped(monkeypatch):
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_byok_providers", lambda tid: set())
    sb = _SB({"tenants": [{"billing_status": "trialing", "plan_id": "p-pro"}],
              "plans": [_PRO_PLAN], "runs": _runs_at_cap()})
    billing.assert_not_locked("t1", sb)
