"""Raises that didn't need an engineer, by agent. Pure.

Labels come from the engineers who closed the issue, so this needs no AI. A label
only says how one issue ended, so this is a prompt for a chat, not a score.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta

from .verdict import parse_dt

DAYS = 30
MIN_FOR_AGENT = 2  # one wrong raise is a mistake; two is a pattern worth a chat


def reporter(body: str | None, field: str) -> str | None:
    if not field:
        return None
    m = re.search(rf"\*\*{re.escape(field)}:\*\*\s*([^|\n*]+)", body or "")
    return m[1].strip() if m else None


def _message(agent: str, raised: int, wrong: list[dict]) -> str:
    lines = [f"Hi {agent}, {len(wrong)} of your {raised} GitHub issues this month didn't need an engineer. "
             "Let's look at these together, so next time you can solve them directly:"]
    lines += [f"- {w['title']} ({', '.join(w['reasons'])}): {w['url']}" for w in wrong[:5]]
    return "\n".join(lines)


def build(closed: list[dict], cfg: dict, now: datetime) -> dict:
    labels = set(cfg.get("wrong_raise_labels") or [])
    since = now - timedelta(days=DAYS)
    recent = [c for c in closed if (at := parse_dt(c.get("closed_at"))) and at >= since]
    raised = Counter(c.get("reporter") or "Unknown" for c in recent)
    wrong: dict[str, list[dict]] = {}
    reasons: Counter = Counter()
    for c in recent:
        hit = sorted(labels & set(c.get("labels") or []))
        if hit:
            reasons.update(hit)
            wrong.setdefault(c.get("reporter") or "Unknown", []).append(
                {"title": c.get("title"), "url": c.get("url"), "reasons": hit})
    agents = [{"agent": name, "raised": raised[name], "wrong": len(items), "issues": items,
               "message": _message(name, raised[name], items)}
              for name, items in wrong.items() if name != "Unknown" and len(items) >= MIN_FOR_AGENT]
    agents.sort(key=lambda a: -a["wrong"])
    total = sum(len(v) for v in wrong.values())
    return {"days": DAYS, "closed": len(recent), "wrong": total,
            "share": round(100 * total / len(recent)) if recent else 0,
            "reasons": reasons.most_common(), "agents": agents}
