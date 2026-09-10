"""
P9 — usage & billing dashboard. Billing-foundation chunk (2026-09-10)
moved plan quotas off a hardcoded dict onto the `plans` table.

`usage_summary(rows, plan_name, limits, period_start, period_end)` turns a
tenant's `runs` rows for one calendar-month window into the numbers the
Billing tab shows: run count, tokens (total + by model), an *illustrative*
estimated cost, a daily series for the usage chart, and plan-quota
percentages. Stays pure/no-DB — `get_plan_for_tenant()` below does the one
Supabase round-trip (tenants.plan_id -> plans row) callers need first.

`estimated_cost_usd` is a notional number for illustration, not a real
invoice: Groq (this project's default LLM provider) runs on its free tier,
so Groq/OpenRouter model ids price at $0; only opt-in Anthropic models
carry a rate, taken from published list pricing at time of writing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("interpreter.billing")

# $ per 1,000,000 tokens, blended prompt+completion for simplicity — an
# illustrative estimate, not a real invoice. Unlisted / Groq / OpenRouter
# free-tier model ids fall back to DEFAULT_RATE_USD (0.0).
RATE_PER_1M_USD: dict[str, float] = {
    "claude-opus-5": 20.0,
    "claude-sonnet-5": 4.5,
    "claude-haiku-4-5": 1.1,
}
DEFAULT_RATE_USD = 0.0

# Fallback only for a tenant somehow missing a plan_id (shouldn't happen —
# `default_tenant_plan()` trigger backstops every insert) or an empty
# `plans` table. Never the primary source of quotas. Same field names as a
# real plan row so `plan_limits()` reads it identically either way.
_FREE_PLAN_FALLBACK: dict[str, Any] = {
    "slug": "free", "name": "Free", "included_runs": 200, "included_tokens": 500_000,
}


class BillingLockedError(RuntimeError):
    """Raised by `assert_not_locked` when a tenant's trial (or grace
    period) has lapsed with no payment on file. Callers turn this into a
    clear, actionable failure — a 402 for the interactive "run" endpoint,
    a job that fails (and retries a few times, harmlessly, before settling
    as an error) for the worker's queued path — never a generic crash."""


def assert_not_locked(tenant_id: "str | None", sb) -> None:
    """The billing-enforcement gate (chunk D): called before a flow
    actually runs, so a locked tenant never spends an LLM call. A missing/
    unresolvable tenant_id is let through — this is a billing gate, not a
    tenant-existence check; other code already handles that."""
    if not tenant_id:
        return
    rows = (sb.table("tenants").select("billing_status").eq("tenant_id", tenant_id)
            .execute().data or [])
    if rows and rows[0].get("billing_status") == "locked":
        raise BillingLockedError(
            "This workspace's trial has ended and no payment method is on file — "
            "flows are paused until billing is reactivated.")


def get_plan_for_tenant(tenant_id: str, sb) -> dict[str, Any]:
    """The tenant's plan row from `plans` — the numbers `usage_summary` and
    `check_and_warn` need, sourced from data instead of a hardcoded dict."""
    trows = (sb.table("tenants").select("plan_id").eq("tenant_id", tenant_id)
             .execute().data or [])
    plan_id = trows[0].get("plan_id") if trows else None
    if plan_id:
        prows = (sb.table("plans").select("*").eq("plan_id", plan_id).execute().data or [])
        if prows:
            return prows[0]
    prows = sb.table("plans").select("*").eq("slug", "free").execute().data or []
    if prows:
        return prows[0]
    return dict(_FREE_PLAN_FALLBACK)


def plan_limits(plan: dict[str, Any]) -> dict[str, int | None]:
    return {"runs": plan.get("included_runs"), "tokens": plan.get("included_tokens")}


def resolve_checkout_plan(plan: dict[str, Any], provider_slug: str,
                          tenant_id: str) -> tuple[dict[str, Any], bool]:
    """Billing chunk F: a tenant who already brings their own LLM key costs
    the platform nothing on tokens, so they check out at the plan's BYOK
    price instead of the standard one — if the plan has one set yet.
    `create_subscription` only ever reads razorpay_plan_id/stripe_price_id,
    so swapping those two keys on a copy of the row is the whole mechanism;
    `interpreter/payments.py` stays entirely unaware BYOK pricing exists.

    Returns (plan_to_use, discount_applied)."""
    from interpreter import llm

    if not llm.tenant_has_byok(tenant_id):
        return plan, False
    byok_id = (plan.get("razorpay_plan_id_byok") if provider_slug == "razorpay"
              else plan.get("stripe_price_id_byok"))
    if not byok_id:
        return plan, False
    return {**plan, "razorpay_plan_id": plan.get("razorpay_plan_id_byok"),
            "stripe_price_id": plan.get("stripe_price_id_byok")}, True


def estimate_cost_usd(tokens_by_model: dict[str, int]) -> float:
    total = 0.0
    for model, n in (tokens_by_model or {}).items():
        rate = RATE_PER_1M_USD.get(model, DEFAULT_RATE_USD)
        total += (n / 1_000_000) * rate
    return round(total, 4)


def _pct(used: int, limit: int | None) -> float | None:
    if limit is None:
        return None
    if limit <= 0:
        return 100.0 if used > 0 else 0.0
    return round(used / limit * 100, 1)


def usage_summary(rows: list[dict[str, Any]], plan_name: str, limits: dict[str, int | None],
                   period_start: str, period_end: str,
                   flow_names: dict[str, str] | None = None) -> dict:
    flow_names = flow_names or {}

    runs_count = len(rows)
    tokens_total = 0
    billable_runs_count = 0
    billable_tokens_total = 0
    tokens_by_model: dict[str, int] = {}
    tokens_by_node: dict[str, int] = {}
    daily: dict[str, dict[str, int]] = {}
    by_flow: dict[str, dict[str, Any]] = {}

    for r in rows:
        row_tokens = int(r.get("tokens_total") or 0)
        tokens_total += row_tokens
        # Billing chunk E — a run's own LLM key costs the platform nothing,
        # so it shouldn't count toward the plan's included runs/tokens. A
        # run only counts as fully-BYOK (excluded) when EVERY LLM call in
        # it used the tenant's own key; a run with any platform-paid call
        # (or none at all — an old/legacy row, or a flow with no LLM node)
        # still counts, same as before chunk E existed. A row with no
        # tokens_by_key_source at all (recorded before this column existed)
        # is the "none at all" case too — its whole tokens_total is
        # platform-billable, not silently zeroed.
        row_key_source = r.get("tokens_by_key_source") or {}
        if row_key_source:
            platform_tokens = int(row_key_source.get("platform") or 0)
            byok_tokens = int(row_key_source.get("byok") or 0)
        else:
            platform_tokens, byok_tokens = row_tokens, 0
        billable_tokens_total += platform_tokens
        if not (byok_tokens > 0 and platform_tokens == 0):
            billable_runs_count += 1
        row_by_model = r.get("tokens_by_model") or {}
        for model, n in row_by_model.items():
            tokens_by_model[model] = tokens_by_model.get(model, 0) + int(n)
        for node, n in (r.get("tokens_by_node") or {}).items():
            tokens_by_node[node] = tokens_by_node.get(node, 0) + int(n)

        created = r.get("created_at")
        day = str(created)[:10] if created else None
        if day:
            bucket = daily.setdefault(day, {"runs": 0, "tokens": 0})
            bucket["runs"] += 1
            bucket["tokens"] += row_tokens

        flow_id = r.get("flow_id")
        if flow_id:
            fb = by_flow.setdefault(flow_id, {
                "flow_id": flow_id, "name": flow_names.get(flow_id, flow_id),
                "runs": 0, "tokens": 0, "tokens_by_model": {},
            })
            fb["runs"] += 1
            fb["tokens"] += row_tokens
            for model, n in row_by_model.items():
                fb["tokens_by_model"][model] = fb["tokens_by_model"].get(model, 0) + int(n)

    by_flow_list = [
        {"flow_id": fb["flow_id"], "name": fb["name"], "runs": fb["runs"],
         "tokens": fb["tokens"], "estimated_cost_usd": estimate_cost_usd(fb["tokens_by_model"])}
        for fb in by_flow.values()
    ]
    by_flow_list.sort(key=lambda f: f["tokens"], reverse=True)

    overall_cost = estimate_cost_usd(tokens_by_model)
    # per-node cost is proportional — the trace doesn't carry a per-node model,
    # so split the run-level $ by each node's share of the tokens.
    by_node_list = sorted(
        ({"node": node, "tokens": n,
          "estimated_cost_usd": round(overall_cost * n / tokens_total, 4) if tokens_total else 0.0}
         for node, n in tokens_by_node.items()),
        key=lambda x: x["tokens"], reverse=True,
    )

    return {
        "period": {"start": period_start, "end": period_end},
        "plan": plan_name,
        "limits": limits,
        "runs_count": runs_count,
        "tokens_total": tokens_total,
        # Billing chunk E — the true totals above are for display
        # ("you ran 400 automations this month"); these two are what
        # actually counts against the plan, with fully-BYOK runs excluded.
        "billable_runs_count": billable_runs_count,
        "billable_tokens_total": billable_tokens_total,
        "tokens_by_model": tokens_by_model,
        "by_node": by_node_list,
        "by_flow": by_flow_list,
        "estimated_cost_usd": overall_cost,
        "daily": [{"date": d, **daily[d]} for d in sorted(daily)],
        "pct_runs_used": _pct(billable_runs_count, limits["runs"]),
        "pct_tokens_used": _pct(billable_tokens_total, limits["tokens"]),
    }


def month_bounds(period: str | None) -> tuple[str, str, str]:
    """`period` ("YYYY-MM") -> (period, period_start, period_end), UTC. Defaults
    to the current calendar month."""
    now = datetime.now(timezone.utc)
    if period:
        year, month = (int(p) for p in period.split("-", 1))
    else:
        year, month = now.year, now.month
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12 \
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return f"{year:04d}-{month:02d}", start.isoformat(), end.isoformat()


# ── Phase 28 step 3: quota warnings (warn-only — never blocks a run) ────
_WARN_PCT = 80
_EXCEEDED_PCT = 100


def check_and_warn(sb, tenant_id: str) -> str | None:
    """Best-effort, fire-and-forget — called right after a run is recorded.
    Checks whether this tenant just crossed 80%/100% of its plan's monthly
    quota and, at most once per (tenant, period, level), logs an
    `audit_log` entry and — if the tenant has a Slack digest channel
    configured — posts a heads-up there. NEVER blocks the run that
    triggered it; a failure here must not propagate.

    Dedup is against `audit_log` itself (no new table): if a
    `billing.quota_<level>` entry already exists for this tenant with
    `metadata.period == this period`, this is a no-op.

    Returns the level reached ("warning" | "exceeded"), or None — purely
    informational for callers/tests; nothing acts on it.
    """
    try:
        from interpreter import audit

        period_label, period_start, period_end = month_bounds(None)

        plan_row = get_plan_for_tenant(tenant_id, sb)
        plan, limits = plan_row["slug"], plan_limits(plan_row)
        if limits["runs"] is None and limits["tokens"] is None:
            return None  # unlimited plan — nothing to warn about

        rows = (sb.table("runs").select("tokens_total, tokens_by_model, tokens_by_key_source, created_at")
                .eq("tenant_id", tenant_id)
                .gte("created_at", period_start).lt("created_at", period_end)
                .limit(5000).execute().data or [])
        s = usage_summary(rows, plan, limits, period_start, period_end)
        pcts = [p for p in (s["pct_runs_used"], s["pct_tokens_used"]) if p is not None]
        if not pcts:
            return None
        pct = max(pcts)
        level = ("exceeded" if pct >= _EXCEEDED_PCT
                 else "warning" if pct >= _WARN_PCT else None)
        if level is None:
            return None

        action = f"billing.quota_{level}"
        already = (sb.table("audit_log").select("event_id, metadata")
                   .eq("tenant_id", tenant_id).eq("action", action)
                   .gte("created_at", period_start)
                   .limit(50).execute().data or [])
        if any((e.get("metadata") or {}).get("period") == period_label for e in already):
            return level  # already warned this period at this level

        summary = (f"{plan} plan at {pct:.0f}% of quota this period "
                   f"({s['runs_count']}/{limits['runs']} runs, "
                   f"{s['tokens_total']}/{limits['tokens']} tokens)")
        audit.record(sb, tenant_id=tenant_id, action=action,
                     target_type="tenant", target_id=tenant_id, summary=summary,
                     metadata={"period": period_label, "pct": pct, "plan": plan})

        try:
            slack_rows = (sb.table("tenant_integrations").select("config")
                          .eq("tenant_id", tenant_id).eq("kind", "slack")
                          .execute().data or [])
            channel = (((slack_rows[0].get("config") or {}).get("digest") or {}).get("channel")
                       if slack_rows else None)
            if channel:
                from interpreter import slack as slackmod
                slackmod.post_message(f":warning: *Billing* — {summary}",
                                      tenant_id=tenant_id, channel=channel, sb=sb)
        except Exception as e:  # noqa: BLE001
            log.warning("billing quota Slack notify failed: %s", e)

        return level
    except Exception as e:  # noqa: BLE001 -- never break the run this fires after
        log.warning("billing.check_and_warn(%s) failed: %s", tenant_id, e)
        return None
