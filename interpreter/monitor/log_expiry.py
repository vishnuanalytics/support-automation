"""Cases whose aggregator logs are about to be lost. Pure.

Some aggregators keep logs for only a few days. A case doesn't record its order
date, so the clock runs from when the case was raised. The order is older than
that, so the logs can go sooner than shown, never later.
"""

from __future__ import annotations

from datetime import datetime

from .dsp_sla import span
from .verdict import parse_dt


def _days(case: dict, cfg: dict) -> tuple[str, float] | None:
    retention = {k.lower(): v for k, v in (cfg.get("log_retention_days") or {}).items()}
    for name in case.get("third_parties") or []:
        if name.lower() in retention:
            return name, retention[name.lower()]
    return None


def _needs_logs(case: dict, cfg: dict) -> bool:
    topics = cfg.get("log_topics") or []
    return case.get("module") in topics or case.get("sub_module") in topics


def _thread(case: dict) -> str:
    """A support case and its aggregator case share one set of logs."""
    return (case.get("parent") or {}).get("id") or case["id"]


def _draft(partner: str, days: float, rows: list[dict]) -> str:
    lines = [f"Hi team, {partner} keeps logs for only {days:g} days. "
             "Please save the logs for these cases today:", ""]
    lines += [f"- {r['case_number']} ({r['owner']}): {r['subject']}, {span(r['days_left'] * 24)} left at most" for r in rows]
    return "\n".join(lines)


def build(cases: list[dict], cfg: dict, now: datetime) -> list[dict]:
    """One entry per aggregator with logs at risk, most urgent first."""
    warn = cfg.get("log_warn_days", 3)
    groups: dict[str, dict] = {}
    seen: set[str] = set()
    # Newest first: an older linked case shouldn't write off logs that can still be saved.
    for c in sorted(cases, key=lambda c: c.get("created_at") or "", reverse=True):
        found, raised = _days(c, cfg), parse_dt(c.get("created_at"))
        if not (found and raised and _needs_logs(c, cfg)) or _thread(c) in seen:
            continue
        seen.add(_thread(c))
        name, days = found
        left = days - (now - raised).total_seconds() / 86400
        g = groups.setdefault(name, {"name": name, "days": days, "rows": [], "gone": 0})
        if left <= 0:
            g["gone"] += 1
        elif left <= warn:
            g["rows"].append({"case_id": c["id"], "case_number": c.get("case_number"), "subject": c.get("subject"),
                              "owner": c.get("owner"), "days_left": round(left, 1),
                              "raised_at": raised.isoformat()})
    out = [{**g, "draft": _draft(g["name"], g["days"], g["rows"])} for g in groups.values() if g["rows"]]
    for g in out:
        g["rows"].sort(key=lambda r: r["days_left"])
    out.sort(key=lambda g: g["rows"][0]["days_left"])
    return out
