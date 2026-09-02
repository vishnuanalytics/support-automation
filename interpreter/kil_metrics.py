"""
KIL-f — Knowledge Integrity Loop metrics.

`compute(sb, tenant_id, days=30)` reads `review_tasks` + `kb_entries` for one
tenant and returns the health numbers the loop is judged by:

  flag_precision        of resolved review tasks, the share marked `correct`
                        (a real knowledge gap) vs `dismissed` (a false flag)
  false_flag_rate       `dismissed` / resolved  -- the alert-fatigue number
  agent_correction_rate `wrong` / resolved      -- how often the human was off
  median_time_to_review time from a flag to its resolution
  kb writeback funnel   entries created / provisional / active / superseded
  promotion_rate        active / (active + provisional) among writeback entries
  knowledge_freshness   median age of the tenant's active KB entries
  weekly                contradictions flagged per ISO week

Read-only; every table read degrades to zero.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

log = logging.getLogger("interpreter.kil_metrics")

_RESOLVED = ("correct", "wrong", "dismissed")


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_days(s: str | None, now: datetime) -> float | None:
    d = _dt(s)
    return None if d is None else max(0.0, (now - d).total_seconds() / 86400)


def compute(sb, tenant_id: str, *, days: int = 30) -> dict:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    tid = str(tenant_id)

    try:
        tasks = (sb.table("review_tasks").select("*")
                 .eq("tenant_id", tid).gte("created_at", since)
                 .limit(2000).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("kil_metrics review_tasks: %s", e)
        tasks = []

    by_trigger = Counter(t.get("trigger") or "?" for t in tasks)
    by_status = Counter(t.get("status") or "open" for t in tasks)
    resolved = [t for t in tasks if t.get("status") in _RESOLVED]
    n_res = len(resolved)
    n_correct = sum(1 for t in resolved if t["status"] == "correct")
    n_wrong = sum(1 for t in resolved if t["status"] == "wrong")
    n_dismissed = sum(1 for t in resolved if t["status"] == "dismissed")
    real = n_correct + n_wrong                       # a genuine issue either way

    ttr = [(_dt(t.get("reviewed_at")) - _dt(t["created_at"])).total_seconds() / 3600
           for t in resolved if _dt(t.get("reviewed_at")) and _dt(t.get("created_at"))]

    try:
        kb = (sb.table("kb_entries")
              .select("status, origin, created_at, supersedes_entry_id")
              .eq("tenant_id", tid).limit(5000).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("kil_metrics kb_entries: %s", e)
        kb = []
    wb = [e for e in kb if e.get("origin") == "review_writeback"]
    wb_prov = sum(1 for e in wb if e["status"] == "provisional")
    wb_active = sum(1 for e in wb if e["status"] == "active")
    wb_superseded = sum(1 for e in kb if e["status"] == "superseded")
    fresh = [a for a in (_age_days(e.get("created_at"), now)
                         for e in kb if e["status"] == "active") if a is not None]

    weeks = min(max(days // 7, 1), 12)
    wk: Counter = Counter()
    for t in tasks:
        d = _dt(t.get("created_at"))
        if d and t.get("trigger") in ("contradicts", "novel"):
            iso = d.isocalendar()
            wk[f"{iso.year}-W{iso.week:02d}"] += 1
    weekly = [{"week": w, "flagged": wk.get(w, 0)}
              for w in sorted(wk)[-weeks:]]

    return {
        "window_days": days,
        "review": {
            "total": len(tasks),
            "open": by_status.get("open", 0),
            "by_trigger": dict(by_trigger),
            "by_status": dict(by_status),
            "resolved": n_res,
            "flag_precision": round(n_correct / real, 3) if real else None,
            "false_flag_rate": round(n_dismissed / n_res, 3) if n_res else None,
            "agent_correction_rate": round(n_wrong / n_res, 3) if n_res else None,
            "median_time_to_review_h": round(statistics.median(ttr), 1) if ttr else None,
        },
        "kb_writeback": {
            "entries": len(wb),
            "provisional": wb_prov,
            "active": wb_active,
            "superseded": wb_superseded,
            "promotion_rate": round(wb_active / (wb_active + wb_prov), 3)
                              if (wb_active + wb_prov) else None,
        },
        "knowledge_freshness_days": round(statistics.median(fresh), 1) if fresh else None,
        "weekly": weekly,
    }


# ── P8a: the weekly learning report ─────────────────────────────────────
def digest(sb, tenant_id: str, *, weeks: int = 4) -> dict:
    """A period-over-period view for the weekly report: this week's metrics,
    last week's for the deltas, the recurring contradictions, and the KB
    changes the loop produced."""
    this = compute(sb, tenant_id, days=7)
    prior = compute_window(sb, tenant_id, start_days_ago=14, end_days_ago=7)

    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=7 * max(weeks, 1))).isoformat()
    try:
        tasks = (sb.table("review_tasks")
                 .select("verdict, trigger, created_at, case_number")
                 .eq("tenant_id", str(tenant_id)).gte("created_at", since)
                 .limit(3000).execute().data or [])
    except Exception:  # noqa: BLE001
        tasks = []
    claims: Counter = Counter()
    for t in tasks:
        if t.get("trigger") not in ("contradicts", "novel"):
            continue
        for cl in ((t.get("verdict") or {}).get("salient") or [])[:1]:
            claims[cl.strip()[:160].lower()] += 1
    top = [{"claim": c, "count": n} for c, n in claims.most_common(5) if n >= 2]

    try:
        recent_kb = (sb.table("kb_entries")
                     .select("title, status, created_at")
                     .eq("tenant_id", str(tenant_id)).eq("origin", "review_writeback")
                     .order("created_at", desc=True).limit(8).execute().data or [])
    except Exception:  # noqa: BLE001
        recent_kb = []

    def _d(a, b):
        return None if (a is None or b is None) else round(a - b, 3)

    r, pr = this["review"], prior["review"]
    return {
        "week_of": now.strftime("%Y-W%V"),
        "this_week": this,
        "deltas": {
            "flagged": (sum(this["review"]["by_trigger"].get(k, 0) for k in ("contradicts", "novel"))
                        - sum(prior["review"]["by_trigger"].get(k, 0) for k in ("contradicts", "novel"))),
            "flag_precision": _d(r["flag_precision"], pr["flag_precision"]),
            "false_flag_rate": _d(r["false_flag_rate"], pr["false_flag_rate"]),
            "knowledge_freshness_days": _d(this["knowledge_freshness_days"],
                                           prior["knowledge_freshness_days"]),
        },
        "top_contradictions": top,
        "recent_kb_changes": recent_kb,
    }


def compute_window(sb, tenant_id: str, *, start_days_ago: int, end_days_ago: int) -> dict:
    """`compute` over an arbitrary [start, end] window (used for last week)."""
    now = datetime.now(timezone.utc)
    lo = (now - timedelta(days=start_days_ago)).isoformat()
    hi = (now - timedelta(days=end_days_ago)).isoformat()
    tid = str(tenant_id)
    try:
        tasks = (sb.table("review_tasks").select("*")
                 .eq("tenant_id", tid).gte("created_at", lo).lt("created_at", hi)
                 .limit(2000).execute().data or [])
    except Exception:  # noqa: BLE001
        tasks = []
    by_trigger = Counter(t.get("trigger") or "?" for t in tasks)
    resolved = [t for t in tasks if t.get("status") in _RESOLVED]
    n_res = len(resolved)
    n_correct = sum(1 for t in resolved if t["status"] == "correct")
    n_wrong = sum(1 for t in resolved if t["status"] == "wrong")
    n_dismissed = sum(1 for t in resolved if t["status"] == "dismissed")
    real = n_correct + n_wrong
    return {
        "review": {
            "by_trigger": dict(by_trigger),
            "flag_precision": round(n_correct / real, 3) if real else None,
            "false_flag_rate": round(n_dismissed / n_res, 3) if n_res else None,
        },
        "knowledge_freshness_days": None,     # a point-in-time metric; not windowed
    }


def render_digest(d: dict) -> str:
    """Slack-flavoured markdown for the weekly digest."""
    r = d["this_week"]["review"]
    kb = d["this_week"]["kb_writeback"]
    dl = d["deltas"]

    def arrow(v):
        if v is None or v == 0:
            return ""
        return f" ({'▲' if v > 0 else '▼'}{abs(v)})"

    lines = [
        f"*Knowledge Integrity — week {d['week_of']}*",
        f"• Contradictions flagged: *{sum(r['by_trigger'].get(k, 0) for k in ('contradicts', 'novel'))}*"
        f"{arrow(dl['flagged'])}",
        f"• Flag precision: *{_pct(r['flag_precision'])}*{arrow(dl['flag_precision'])}   "
        f"False-flag rate: *{_pct(r['false_flag_rate'])}*{arrow(dl['false_flag_rate'])}",
        f"• Open reviews: *{r['open']}*   Median time to review: "
        f"*{r['median_time_to_review_h']}h*" if r['median_time_to_review_h'] is not None
        else f"• Open reviews: *{r['open']}*",
        f"• KB changes: *{kb['entries']}* ({kb['provisional']} provisional, "
        f"{kb['active']} confirmed, {kb['superseded']} retired)",
        f"• Knowledge freshness: *{d['this_week']['knowledge_freshness_days']}d*"
        f"{arrow(dl['knowledge_freshness_days'])}",
    ]
    if d["top_contradictions"]:
        lines.append("*Recurring contradictions:*")
        lines += [f"  {c['count']}× {c['claim']}" for c in d["top_contradictions"]]
    if d["recent_kb_changes"]:
        lines.append("*Latest KB updates:*")
        lines += [f"  • {e['title']} — {e['status']}" for e in d["recent_kb_changes"][:5]]
    return "\n".join(lines)


def _pct(v):
    return "—" if v is None else f"{round(v * 100)}%"
