"""Agents waiting on a senior's help. Pure.

A request counts as answered once someone other than the agent replies. An
automatic reply doesn't count, because it can be wrong and nobody has checked it.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import dsp_sla
from .verdict import parse_dt

SHOW_DAYS = 7
SLOW_HOURS = 1
AWAY_HOURS = 24
TOP_MODULES = 5
STATS_DAYS = 30


def in_hours(at: datetime, hours: dict) -> bool:
    local = at.astimezone(ZoneInfo(hours.get("tz") or "UTC"))
    return hours["start"] <= local.hour < hours["end"]


def _card(r: dict, at: datetime, hours: dict, cases: dict[str, str], now: datetime) -> dict:
    wait = (now - at).total_seconds() / 3600
    away = bool(hours) and not in_hours(at, hours)
    meta = " · ".join(x for x in (r.get("module"), r.get("sub_module"), r.get("priority"),
                                  "Raised outside your hours" if away else None) if x)
    return {"key": f"help:{r['ts']}", "kind": "help", "title": f"{r.get('agent') or 'An agent'} needs help on {r.get('ticket')}",
            "why": r.get("question"), "meta": meta, "chip": dsp_sla.span(wait),
            "tone": "exception" if wait >= SLOW_HOURS else "warn", "url": r.get("url"), "url_label": "Open thread",
            "case_id": cases.get(r.get("ticket") or ""), "activity_at": r.get("raised_at"),
            "case_ids": [cases[r["ticket"]]] if r.get("ticket") in cases else []}


def build(rows: list[dict], hours: dict, cases: dict[str, str], now: datetime) -> dict:
    """cases maps an open case number to its id."""
    cards, away_total, away_answered = [], 0, 0
    modules: Counter = Counter()
    for r in rows:
        at = parse_dt(r.get("raised_at"))
        if at is None:
            continue
        if now - at <= timedelta(days=STATS_DAYS) and r.get("module"):
            modules[r["module"]] += 1
        if now - at > timedelta(days=SHOW_DAYS):
            continue
        if not r.get("answered_at"):
            cards.append(_card(r, at, hours, cases, now))
        if hours and now - at <= timedelta(hours=AWAY_HOURS) and not in_hours(at, hours):
            away_total += 1
            away_answered += bool(r.get("answered_at"))
    return {"cards": cards, "away": {"total": away_total, "answered": away_answered},
            "modules": modules.most_common(TOP_MODULES)}
