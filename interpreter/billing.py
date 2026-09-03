"""
P9 — usage & billing dashboard.

`usage_summary(rows, plan, period_start, period_end)` turns a tenant's
`runs` rows for one calendar-month window into the numbers the Billing tab
shows: run count, tokens (total + by model), an *illustrative* estimated
cost, a daily series for the usage chart, and plan-quota percentages.

Scope is usage metering only — there is no payment processing behind this
(`tenants.plan` is a static admin-set label, not billing-system-driven).
`estimated_cost_usd` is a notional number for illustration, not a real
invoice: Groq (this project's default LLM provider) runs on its free tier,
so Groq/OpenRouter model ids price at $0; only opt-in Anthropic models
carry a rate, taken from published list pricing at time of writing.

Read-only / pure — no Supabase calls here (the route in api/main.py does
the fetch, same split as kil_metrics.py's compute() vs. its caller).
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

# Static per-tenant plan quotas. None = unlimited. Not payment-processor
# driven — an admin sets `tenants.plan`, this table just holds the numbers.
PLAN_LIMITS: dict[str, dict[str, int | None]] = {
    "free": {"runs": 200, "tokens": 500_000},
    "pro": {"runs": None, "tokens": None},
}


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


def usage_summary(rows: list[dict[str, Any]], plan: str,
                   period_start: str, period_end: str,
                   flow_names: dict[str, str] | None = None) -> dict:
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    flow_names = flow_names or {}

    runs_count = len(rows)
    tokens_total = 0
    tokens_by_model: dict[str, int] = {}
    daily: dict[str, dict[str, int]] = {}
    by_flow: dict[str, dict[str, Any]] = {}

    for r in rows:
        row_tokens = int(r.get("tokens_total") or 0)
        tokens_total += row_tokens
        row_by_model = r.get("tokens_by_model") or {}
        for model, n in row_by_model.items():
            tokens_by_model[model] = tokens_by_model.get(model, 0) + int(n)

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

    return {
        "period": {"start": period_start, "end": period_end},
        "plan": plan,
        "limits": limits,
        "runs_count": runs_count,
        "tokens_total": tokens_total,
        "tokens_by_model": tokens_by_model,
        "by_flow": by_flow_list,
        "estimated_cost_usd": estimate_cost_usd(tokens_by_model),
        "daily": [{"date": d, **daily[d]} for d in sorted(daily)],
        "pct_runs_used": _pct(runs_count, limits["runs"]),
        "pct_tokens_used": _pct(tokens_total, limits["tokens"]),
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

        trows = (sb.table("tenants").select("plan").eq("tenant_id", tenant_id)
                 .execute().data or [])
        plan = (trows[0].get("plan") if trows else None) or "free"
        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
        if limits["runs"] is None and limits["tokens"] is None:
            return None  # unlimited plan — nothing to warn about

        rows = (sb.table("runs").select("tokens_total, tokens_by_model, created_at")
                .eq("tenant_id", tenant_id)
                .gte("created_at", period_start).lt("created_at", period_end)
                .limit(5000).execute().data or [])
        s = usage_summary(rows, plan, period_start, period_end)
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
