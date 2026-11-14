"""Every open support GitHub issue, by squad, with where it stands. Pure."""

from __future__ import annotations

from datetime import datetime

from . import escalations, issue_status
from .verdict import parse_dt

NO_SQUAD = "No squad"
_PRIORITY = {"urgent": 0, "high": 1, "medium": 2, "low": 3}


def sla_status(target_at: str | None, now: datetime) -> str | None:
    target = parse_dt(target_at)
    if target is None:
        return None
    minutes = int(abs((target - now).total_seconds()) // 60)
    span = f"{minutes // 1440}d {minutes % 1440 // 60}h"
    return f"Breached by {span}" if target < now else f"Due in {span}"


def _main_case(cases: list[dict], support_type: str) -> dict:
    """The case whose SLA matters: a support case first, then the most urgent, then the soonest."""
    return min(cases, key=lambda c: (c.get("record_type") == support_type,
                                     _PRIORITY.get((c.get("priority") or "").lower(), 9),
                                     c.get("target_at") or "9999"))


def _businesses(cases: list[dict]) -> list[dict]:
    """Each business with an open case on this issue, once."""
    seen: dict[tuple, dict] = {}
    for c in cases:
        found = escalations.business(c)
        if found["business"] or found["biz_id"]:
            seen.setdefault((found["business"], found["biz_id"]), found)
    return list(seen.values())


def _row(ev: dict, read: dict, cases: list[dict], days: float, now: datetime, cfg: dict) -> dict:
    opened = parse_dt(ev.get("opened_at"))
    age = (now - opened).total_seconds() if opened else 0
    messages = sorted((m for m in ev.get("messages") or [] if parse_dt(m.get("at"))),
                      key=lambda m: parse_dt(m["at"]))
    case = _main_case(cases, cfg.get("dependency_record_type")) if cases else {}
    suffix = cfg.get("github_squad_suffix") or ""
    squads = [x for x in ev.get("labels") or [] if suffix and x.endswith(suffix)]
    status = read.get("status") or issue_status.NOT_READ
    return {
        "squad": squads[0] if squads else NO_SQUAD,
        "issue": ev["label"].rsplit("#", 1)[-1], "url": ev["url"], "title": ev.get("title"),
        "last_reply_by": messages[-1]["who"] if messages else None,
        "days_open": int(age // 86400), "over_days": age > days * 86400,
        "opened_at": ev.get("opened_at"),
        "last_activity_at": messages[-1]["at"] if messages else ev.get("opened_at"),
        "case_id": case.get("id"), "case_number": case.get("case_number"),
        "other_cases": [c.get("case_number") for c in cases if c is not case],
        "businesses": _businesses(cases),
        "priority": case.get("priority"), "sla_due": case.get("target_at"),
        "sla_status": sla_status(case.get("target_at"), now),
        "status": status, "tech_status": issue_status.LABELS[status],
        "waiting_on": read.get("waiting_on"), "reason": read.get("reason") or "",
    }


def build(issues: list[dict], cases: list[dict], evidence: dict[str, dict], days: float,
          now: datetime, cfg: dict) -> dict:
    by_issue: dict[str, list[dict]] = {}
    for c in cases:
        for link in c.get("links") or []:
            if link["kind"] == "github":
                by_issue.setdefault(issue_status.issue_key(link), []).append(c)

    rows, unread = [], 0
    for read in issues:
        ev = evidence.get(read["url"])
        if ev is None or not ev.get("ok"):
            unread += 1
        elif ev.get("state") == "open":
            rows.append(_row(ev, read, by_issue.get(ev["label"], []), days, now, cfg))

    groups: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["days_open"]):
        groups.setdefault(r["squad"], []).append(r)
    squads = [{"squad": k, "issues": v} for k, v in groups.items()]
    squads.sort(key=lambda s: -len(s["issues"]))
    return {"days": days, "total": len(rows), "unread": unread, "squads": squads,
            "statuses": issue_status.LABELS}
