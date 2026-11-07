"""Aggregator SLAs: each kind of issue has its own clock. Pure.

The clock starts when the case is raised to the aggregator. Silence past the
escalate mark means escalate; past the resolve mark means the SLA is breached. An
issue with no rule uses the case's own target date, escalating at a set share of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import escalations, verdict
from .verdict import parse_dt

BREACHED, ESCALATE, ON_TRACK = "breached", "escalate", "on_track"
STANDARD = "Standard"
URGENT_HOURS = 48  # rules this tight go to the top of the day
# A rule is ready to tighten once most of a week's cases beat it, on enough cases to trust.
READY_SHARE = 80
READY_MIN_CASES = 5


def span(hours: float) -> str:
    hours = abs(hours)
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 24:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d {int(hours % 24)}h"


def rule_for(case: dict, rules: list[dict]) -> dict | None:
    subject = (case.get("subject") or "").lower()
    for r in rules:
        if (case.get("module") in (r.get("modules") or [])
                or case.get("sub_module") in (r.get("sub_modules") or [])
                or any(k.lower() in subject for k in r.get("keywords") or [])):
            return r
    return None


def _clock(case: dict, started: datetime, cfg: dict) -> tuple[str, float, float] | None:
    rule = rule_for(case, cfg.get("dsp_sla_rules") or [])
    if rule:
        return rule["name"], rule["escalate_hours"], rule["resolve_hours"]
    target = parse_dt(case.get("target_at"))
    if target is None or target <= started:
        return None
    resolve = (target - started).total_seconds() / 3600
    return STANDARD, resolve * cfg.get("dsp_escalate_share", 0.5), resolve


def row(case: dict, v: dict, evidence: list[dict], now: datetime, cfg: dict) -> dict | None:
    started = parse_dt(case.get("created_at"))
    clock = _clock(case, started, cfg) if started else None
    if clock is None:
        return None
    rule, escalate_hours, resolve_hours = clock
    escalate_at = started + timedelta(hours=escalate_hours)
    deadline = started + timedelta(hours=resolve_hours)
    update = escalations.last_update(case, v, evidence)
    waiting = verdict.stage(v) == verdict.THIRD_PARTY
    if now > deadline:
        state = BREACHED
    elif now >= escalate_at and update is None:
        state = ESCALATE
    else:
        state = ON_TRACK
    return {
        "case_id": case["id"], "case_number": case.get("case_number"), "subject": case.get("subject"),
        "owner": case.get("owner"), "partner": (case.get("third_parties") or ["Not set"])[0],
        "priority": case.get("priority"), "bu": case.get("bu"), "rule": rule, **escalations.business(case),
        "urgent": rule != STANDARD and resolve_hours <= URGENT_HOURS,
        "raised_at": started.isoformat(), "escalate_at": escalate_at.isoformat(),
        "deadline": deadline.isoformat(), "state": state, "waiting": waiting,
        "hours_left": (deadline - now).total_seconds() / 3600,
        "last_update_at": update["at_dt"].isoformat() if update else None,
        "url": case.get("url"),
    }


def _draft(partner: str, rows: list[dict]) -> str:
    lines = [f"Hi {partner} team,", "",
             f"These {len(rows)} issues are past the agreed time. Please share an update today:", ""]
    for r in rows:
        late = "past SLA by " + span(r["hours_left"]) if r["state"] == BREACHED else "no update yet"
        lines.append(f"- {r['case_number']}: {r['subject']} ({r['rule']}, {late})")
    return "\n".join(lines + ["", "Thanks"])


def build(items: list[tuple[dict, dict, list[dict]]], now: datetime, cfg: dict) -> dict:
    """items: (case, verdict, evidence) for open cases raised to an aggregator."""
    rows = [r for c, v, ev in items if (r := row(c, v, ev, now, cfg))]
    partners_info = {(c.get("third_parties") or ["Not set"])[0]: c["partner"]
                     for c, _, _ in items if c.get("partner")}
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["partner"], []).append(r)
    partners = []
    for name, members in groups.items():
        late = sorted((r for r in members if r["state"] != ON_TRACK and r["waiting"]),
                      key=lambda r: (not r["urgent"], r["hours_left"]))
        info = partners_info.get(name) or {}
        partners.append({
            "name": name, "open": len(members), "late": late,
            "breached": sum(r["state"] == BREACHED for r in members),
            "to": info.get("to_emails") or [], "cc": info.get("cc_emails") or [],
            "channel": info.get("channel"), "note": info.get("escalation_note"),
            "contacts": info.get("contacts") or [],
            "draft": _draft(name, late) if late else None,
        })
    partners.sort(key=lambda p: (-sum(r["urgent"] for r in p["late"]), -len(p["late"])))
    return {"total": len(rows), "breached": sum(r["state"] == BREACHED for r in rows),
            "escalate": sum(r["state"] == ESCALATE for r in rows),
            "on_track": sum(r["state"] == ON_TRACK for r in rows),
            "partners": partners, "rows": rows}


def _rule_stats(resolved: list[dict], cfg: dict) -> list[dict]:
    """How each rule did, so it can be tightened once cases are beating it."""
    out = []
    rules = cfg.get("dsp_sla_rules") or []
    for r in rules:
        spans = [(parse_dt(c["closed_at"]) - parse_dt(c["opened_at"])).total_seconds() / 3600
                 for c in resolved if rule_for(c, rules) is r and c.get("opened_at") and c.get("closed_at")]
        in_time = sum(h <= r["resolve_hours"] for h in spans)
        share = round(100 * in_time / len(spans)) if spans else 0
        out.append({**{k: r.get(k) for k in ("name", "escalate_hours", "resolve_hours", "goal_resolve_hours")},
                    "closed": len(spans), "in_time": in_time, "share": share,
                    "ready": len(spans) >= READY_MIN_CASES and share >= READY_SHARE
                    and r["resolve_hours"] > r.get("goal_resolve_hours", 0)})
    return out


def weekly_report(sla: dict, resolved: list[dict], now: datetime, cfg: dict) -> dict:
    """The week's numbers by aggregator: what closed in time, and what's still late."""
    since = now - timedelta(days=7)
    week = [c for c in resolved if (at := parse_dt(c.get("closed_at"))) and at >= since]
    by: dict[str, dict] = {}

    def entry(name: str) -> dict:
        return by.setdefault(name, {"name": name, "open": 0, "open_late": 0, "closed": 0, "closed_timed": 0, "closed_in_time": 0})

    for r in sla["rows"]:
        e = entry(r["partner"])
        e["open"] += 1
        e["open_late"] += r["state"] == BREACHED
    for c in week:
        closed, opened = parse_dt(c.get("closed_at")), parse_dt(c.get("opened_at"))
        names = c.get("third_parties") or []
        if not (closed and opened and names):
            continue
        e = entry(names[0])
        e["closed"] += 1
        rule = rule_for(c, cfg.get("dsp_sla_rules") or [])
        if rule:  # a closed case keeps no target date, so only ruled ones can be judged
            e["closed_timed"] += 1
            e["closed_in_time"] += (closed - opened).total_seconds() / 3600 <= rule["resolve_hours"]
    partners = sorted(by.values(), key=lambda e: -e["open_late"])
    return {"since": since.isoformat(), "partners": partners, "rules": _rule_stats(week, cfg)}
