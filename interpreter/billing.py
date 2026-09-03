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

from datetime import datetime, timezone
from typing import Any

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
                   period_start: str, period_end: str) -> dict:
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])

    runs_count = len(rows)
    tokens_total = 0
    tokens_by_model: dict[str, int] = {}
    daily: dict[str, dict[str, int]] = {}

    for r in rows:
        tokens_total += int(r.get("tokens_total") or 0)
        for model, n in (r.get("tokens_by_model") or {}).items():
            tokens_by_model[model] = tokens_by_model.get(model, 0) + int(n)

        created = r.get("created_at")
        day = str(created)[:10] if created else None
        if day:
            bucket = daily.setdefault(day, {"runs": 0, "tokens": 0})
            bucket["runs"] += 1
            bucket["tokens"] += int(r.get("tokens_total") or 0)

    return {
        "period": {"start": period_start, "end": period_end},
        "plan": plan,
        "limits": limits,
        "runs_count": runs_count,
        "tokens_total": tokens_total,
        "tokens_by_model": tokens_by_model,
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
