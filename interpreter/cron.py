"""
A tiny 5-field cron matcher — enough for `flow_triggers` schedule triggers
(P6b). No dependency.

Fields: minute hour day-of-month month day-of-week
Each field: `*`, `N`, `A-B`, `A-B/S`, `*/S`, or a comma list of those.
day-of-week: 0-6 (Sun=0); `7` also means Sun.

    matches("*/15 * * * *", dt)         -> bool at that minute
    due("0 9 * * 1-5", last_fired, now) -> was there a matching minute in
                                           (last_fired, now]?  (60-min lookback)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]   # dow 7 == Sun, normalised in parse()


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        for v in range(a, b + 1, step):
            if lo <= v <= hi:
                out.add(v)
    return out


def parse(expr: str) -> list[set[int]]:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(parts)}: {expr!r}")
    fields = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, _BOUNDS)]
    # normalise dow: 7 -> 0
    if 7 in fields[4]:
        fields[4] = (fields[4] - {7}) | {0}
    return fields


def matches(expr: str, dt: datetime) -> bool:
    mi, hr, dom, mon, dow = parse(expr)
    if not (dt.minute in mi and dt.hour in hr and dt.month in mon):
        return False
    d_in_dom = dt.day in dom
    d_in_dow = (dt.weekday() + 1) % 7 in dow        # weekday(): Mon=0 -> cron: Sun=0
    dom_wild, dow_wild = len(dom) == 31, len(dow) == 7
    if dom_wild and dow_wild:
        return True
    if dom_wild:
        return d_in_dow
    if dow_wild:
        return d_in_dom
    return d_in_dom or d_in_dow                     # both restricted -> OR (standard cron)


def due(expr: str, last_fired: datetime | None, now: datetime | None = None,
        *, lookback_min: int = 60) -> bool:
    """Was there a cron-matching minute in `(last_fired, now]`? A capped
    lookback means a worker that was down for hours fires once, not a flood."""
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=lookback_min - 1)
    if last_fired is not None:
        lf = last_fired.replace(second=0, microsecond=0)
        start = max(start, lf + timedelta(minutes=1))
    t = start
    while t <= now:
        if matches(expr, t):
            return True
        t += timedelta(minutes=1)
    return False
