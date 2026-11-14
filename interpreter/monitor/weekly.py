"""The Wednesday report: what's pending from others, ready for the lead to post. Pure.

An engineering issue counts only while its case is open and tech owes the resolution,
so anything waiting on support, a merchant or a partner stays out. The lead posts the
message; nothing here sends or tags anyone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import dsp_sla, issue_status as st
from .verdict import parse_dt

_OWED = (st.NO_REPLY, st.INVESTIGATING, st.CAUSE_GIVEN)
MIN_QUIET_DAYS = 1  # an aggregator that replied today isn't "no response"


def week_start(now: datetime, cfg: dict) -> datetime:
    """The start of the lead's week, in their own timezone."""
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    back = (local.weekday() - cfg["week_start_weekday"]) % 7
    return (local - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)


def _left_out(r: dict) -> str | None:
    """Why an issue isn't tech's to resolve this week, or None if it is."""
    if not r.get("case_id"):
        return "No open case"
    if r["status"] == st.NOT_READ:
        return "Not read yet"
    if r["status"] == st.ANSWERED:
        return "Already answered"
    if r["status"] not in _OWED or r.get("waiting_on") != "tech":
        return {"support": "Waiting on support", "merchant": "Waiting on the merchant",
                "third_party": "Waiting on a partner"}.get(r.get("waiting_on"), "Nothing left for tech")
    return None


def _business(r: dict) -> str:
    return ", ".join(b.get("business") or str(b.get("biz_id")) for b in r.get("businesses") or []) or "No business"


def _issue_line(r: dict) -> str:
    return (f"- #{r['issue']} {r['title']} | Case {r['case_number']} | {_business(r)} | "
            f"{r['days_open']} days open | {r['tech_status']} | {r['url']}")


def _engineering_message(day: str, squads: list[dict], total: int) -> str:
    lines = [f"Hi team, as of {day}, {total} GitHub issue{'' if total == 1 else 's'} with an open case "
             "still need a resolution from engineering. Please share an update on these:"]
    for s in squads:
        lines += ["", f"{s['squad']} ({len(s['issues'])})", *map(_issue_line, s["issues"])]
    return "\n".join(lines)


def engineering(tech: dict, day: str) -> dict:
    groups: dict[str, list[dict]] = {}
    left_out: dict[str, int] = {}
    for squad in tech["squads"]:
        for r in squad["issues"]:
            reason = _left_out(r)
            if reason:
                left_out[reason] = left_out.get(reason, 0) + 1
            else:
                groups.setdefault(r["squad"], []).append(r)
    squads = [{"squad": k, "issues": sorted(v, key=lambda r: -r["days_open"])} for k, v in groups.items()]
    squads.sort(key=lambda s: -len(s["issues"]))
    total = sum(len(s["issues"]) for s in squads)
    for s in squads:
        s["message"] = _engineering_message(day, [s], len(s["issues"]))
    return {"total": total, "squads": squads, "left_out": left_out,
            "message": _engineering_message(day, squads, total) if squads else None}


def _quiet_days(r: dict, now: datetime) -> int:
    since = parse_dt(r.get("last_update_at")) or parse_dt(r["raised_at"])
    return int((now - since).total_seconds() // 86400)


def _partner_message(name: str, rows: list[dict]) -> str:
    lines = [f"Hi {name} team, these {len(rows)} cases are past SLA with no response. "
             "Please share an update:", ""]
    lines += [f"- {r['case_number']} | {r['subject']} | {r.get('business') or 'No business'} | "
              f"{r.get('biz_id') or 'No biz ID'} | {r['quiet_days']} days without an update" for r in rows]
    return "\n".join(lines)


def dsp(sla: dict, now: datetime) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in sla["rows"]:
        quiet = _quiet_days(r, now)
        if r["state"] == dsp_sla.BREACHED and r["waiting"] and quiet >= MIN_QUIET_DAYS:
            groups.setdefault(r["partner"], []).append({**r, "quiet_days": quiet})
    partners = [{"name": k, "rows": sorted(v, key=lambda r: -r["quiet_days"])} for k, v in groups.items()]
    partners.sort(key=lambda p: -len(p["rows"]))
    for p in partners:
        p["message"] = _partner_message(p["name"], p["rows"])
    return {"total": sum(len(p["rows"]) for p in partners), "partners": partners}


def build(tech: dict, sla: dict, now: datetime, cfg: dict) -> dict:
    start = week_start(now, cfg)
    day = now.astimezone(ZoneInfo(cfg["timezone"])).strftime("%a %d %b")
    return {"week_start": start.isoformat(), "week_label": start.strftime("%a %d %b"), "as_of": now.isoformat(),
            "engineering": engineering(tech, day), "dsp": dsp(sla, now)}
