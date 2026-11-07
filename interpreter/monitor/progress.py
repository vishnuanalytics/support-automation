"""Whether the lead's workload is shrinking: one row of numbers per day. Pure."""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from .verdict import parse_dt

WINDOW_DAYS = 7  # a day is noisy, so the start and the latest are each a week's average
DONE_PREFIXES = ("gh:", "dsp:", "team:", "help:")
# (key, label, whether a lower number is better; None when either way can be good)
MEASURES = (
    ("cards", "Things on Today, busiest point of the day", True),
    ("done", "Things marked done in a day", None),
    ("github_open", "Open GitHub issues", True),
    ("github_close_hours", "Hours to close a GitHub issue (median, last 7 days)", True),
    ("wrong_raise_share", "GitHub issues wrongly raised, last 30 days (%)", True),
    ("dsp_new_late", "New aggregator cases past SLA", True),
    ("dsp_old_late", "Old aggregator cases past SLA", True),
)


def local_day(now: datetime, cfg: dict) -> str:
    tz = (cfg.get("assist_hours") or {}).get("tz")
    return (now.astimezone(ZoneInfo(tz)) if tz else now).date().isoformat()


def _close_hours(closed: list[dict], now: datetime) -> float | None:
    since = now - timedelta(days=WINDOW_DAYS)
    hours = [(done - opened).total_seconds() / 3600 for c in closed
             if (done := parse_dt(c.get("closed_at"))) and done >= since
             and (opened := parse_dt(c.get("opened_at")))]
    return round(median(hours), 1) if hours else None


def _done_on(reviews: list[dict], day: str, cfg: dict) -> int:
    return sum(1 for r in reviews if str(r.get("case_id")).startswith(DONE_PREFIXES)
               and (at := parse_dt(r.get("reviewed_at"))) and local_day(at, cfg) == day)


def snapshot(today: dict, sla: dict, closed: list[dict], reviews: list[dict], previous: dict | None,
             now: datetime, cfg: dict) -> dict:
    """Today's numbers. The sync runs all day, so `cards` keeps the day's highest."""
    day = local_day(now, cfg)
    rows = sla["rows"]
    return {
        "cards": max(len(today["cards"]) + today["done"], (previous or {}).get("cards", 0)),
        "done": _done_on(reviews, day, cfg),
        "open_cases": today["open_cases"],
        "github_open": today["github"]["open"],
        "github_close_hours": _close_hours(closed, now),
        "wrong_raise_share": today["coaching"]["share"],
        "dsp_new_late": sum(r["new"] and r["state"] == "breached" for r in rows),
        "dsp_old_late": sum(not r["new"] and r["state"] == "breached" for r in rows),
    }


def _average(rows: list[dict], key: str) -> float | None:
    values = [r["stats"][key] for r in rows if r["stats"].get(key) is not None]
    return round(sum(values) / len(values), 1) if values else None


def _change(start: float | None, latest: float | None, lower_is_better: bool | None, days: int) -> str:
    if days <= WINDOW_DAYS or start is None or latest is None:
        return "too_early"
    if lower_is_better is None:
        return "not_judged"
    if start == latest:
        return "same"
    return "better" if (latest < start) == lower_is_better else "worse"


def summary(rows: list[dict]) -> dict:
    """rows: oldest first. The first week against the latest week."""
    if not rows:
        return {"since": None, "days": 0, "measures": []}
    first, latest = rows[:WINDOW_DAYS], rows[-WINDOW_DAYS:]
    measures = []
    for key, label, lower_is_better in MEASURES:
        start, now_value = _average(first, key), _average(latest, key)
        measures.append({"key": key, "label": label, "start": start, "now": now_value,
                         "change": _change(start, now_value, lower_is_better, len(rows))})
    return {"since": rows[0]["day"], "days": len(rows), "measures": measures}
