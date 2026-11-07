"""What needs the lead right now, with the next step ready. Pure.

Everything else stays out of sight. Marking a card done hides it until something
new happens, or for a day at most, so nothing can be lost by clearing it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import dsp_sla, issue_status as st
from .verdict import parse_dt

DONE_FOR_HOURS = 24
HELP, ACT, ESCALATE_URGENT, CHASE, CLOSE, ESCALATE, FOLLOW, TEAM = range(8)
_WHO = {"merchant": "the merchant", "third_party": "the aggregator"}


def _hours_since(at: str | None, now: datetime) -> float:
    when = parse_dt(at)
    return (now - when).total_seconds() / 3600 if when else 0


def _case_of(r: dict) -> str:
    return f"Case {r['case_number']}" + (f" ({r['priority']})" if r.get("priority") else "") if r.get("case_number") else "No open case"


def _issue_card(r: dict, cfg: dict, now: datetime) -> dict | None:
    quiet = _hours_since(r["last_activity_at"], now)
    age = _hours_since(r["opened_at"], now)
    chase_after, quiet_after = cfg["issue_chase_hours"], cfg["issue_quiet_hours"]
    who, status = r.get("waiting_on"), r["status"]
    tag = f"#{r['issue']}"
    base = {"key": f"gh:{r['issue']}", "activity_at": r["last_activity_at"], "url": r["url"],
            "url_label": "Open issue", "case_ids": [r["case_id"]] if r.get("case_id") else [],
            "case_id": r.get("case_id"), "subject": r.get("title"), "meta": f"{r['squad']} · {_case_of(r)}"}
    if who == "support":
        return {**base, "rank": ACT, "kind": "act", "title": f"{tag}: engineers answered, your side is next",
                "why": r["reason"], "chip": f"{dsp_sla.span(quiet)} ago", "tone": "warn"}
    if status == st.NO_REPLY and age >= chase_after:
        draft = (f"Hi {r['squad']}, {tag} has had no reply for {dsp_sla.span(age)}. {_case_of(r)}. "
                 f"Can someone pick it up today? {r['url']}")
        return {**base, "rank": CHASE, "kind": "chase", "title": f"{tag}: no engineer has replied",
                "why": r.get("title"), "chip": dsp_sla.span(age), "tone": "exception",
                "draft": draft, "draft_label": "Chase message"}
    if who == "tech" and status != st.NO_REPLY and quiet >= quiet_after:
        draft = f"Hi {r['squad']}, any update on {tag}? {r['reason']} {r['url']}"
        return {**base, "rank": CHASE, "kind": "chase", "title": f"{tag}: engineers have gone quiet",
                "why": r["reason"], "chip": dsp_sla.span(quiet), "tone": "warn",
                "draft": draft, "draft_label": "Chase message"}
    if who == "nobody" and status in (st.ANSWERED, st.CAUSE_GIVEN):
        return {**base, "rank": CLOSE, "kind": "close", "title": f"{tag}: looks ready to close",
                "why": r["reason"], "chip": dsp_sla.span(age), "tone": "neutral"}
    if who in _WHO and quiet >= quiet_after:
        draft = f"Please follow up with {_WHO[who]} on {tag}: {r['reason']} {r['url']}"
        return {**base, "rank": FOLLOW, "kind": "follow", "title": f"{tag}: waiting on {_WHO[who]}",
                "why": r["reason"], "chip": dsp_sla.span(quiet), "tone": "neutral",
                "draft": draft, "draft_label": "Message for the agent"}
    return None


def _partner_card(p: dict) -> dict | None:
    late = p["late"]
    if not late:
        return None
    urgent = sum(r["urgent"] for r in late)
    worst = late[0]
    parts = [f"{urgent} urgent"] if urgent else []
    parts.append(f"{sum(r['state'] == dsp_sla.BREACHED for r in late)} past SLA")
    return {"key": f"dsp:{p['name']}", "rank": ESCALATE_URGENT if urgent else ESCALATE, "kind": "escalate",
            "title": f"Escalate {len(late)} {p['name']} case{'' if len(late) == 1 else 's'}",
            "why": ", ".join(parts) + f". First: {worst['subject']}", "meta": p.get("note") or "",
            "chip": "Urgent" if urgent else f"{len(late)} late", "tone": "exception" if urgent else "warn",
            "draft": p["draft"], "draft_label": "Escalation email", "to": p["to"], "cc": p["cc"],
            "channel": p["channel"], "contacts": p["contacts"],
            "activity_at": max(r["escalate_at"] for r in late),
            "case_ids": [r["case_id"] for r in late]}


def _hidden(card: dict, reviews: dict[str, dict], now: datetime) -> bool:
    done = parse_dt((reviews.get(card["key"]) or {}).get("reviewed_at"))
    if done is None or now - done >= timedelta(hours=DONE_FOR_HOURS):
        return False
    activity = parse_dt(card.get("activity_at"))
    return activity is None or activity <= done


def build(issues: dict, sla: dict, coaching: dict, follow_ups: dict, open_cases: int,
          reviews: dict[str, dict], cfg: dict, now: datetime, help_view: dict | None = None) -> dict:
    help_view = help_view or {"cards": [], "away": {"total": 0, "answered": 0}, "modules": []}
    rows = [r for s in issues["squads"] for r in s["issues"]]
    cards = [{**c, "rank": HELP} for c in help_view["cards"]]
    cards += [c for r in rows if (c := _issue_card(r, cfg, now))]
    cards += [c for p in sla["partners"] if (c := _partner_card(p))]
    if follow_ups["total"]:
        cards.append({"key": "team:follow-ups", "rank": TEAM, "kind": "team", "tab": "followups",
                      "title": f"Send {len(follow_ups['owners'])} agents their follow-ups",
                      "why": f"{follow_ups['total']} cases, with a message ready for each agent.",
                      "chip": str(follow_ups["total"]), "tone": "neutral", "case_ids": [],
                      "activity_at": None})
    shown = [c for c in cards if not _hidden(c, reviews, now)]
    shown.sort(key=lambda c: c["rank"])
    covered = {i for c in cards for i in c["case_ids"]}
    return {
        "cards": shown, "done": len(cards) - len(shown),
        "clear": max(open_cases - len(covered), 0), "open_cases": open_cases,
        "github": {"open": len(rows), "need_you": sum(c["key"].startswith("gh:") for c in shown),
                   "with_tech": sum(r.get("waiting_on") == "tech" for r in rows)},
        "dsp": {k: sla[k] for k in ("total", "breached", "escalate", "on_track")},
        "coaching": coaching, "help": {"away": help_view["away"], "modules": help_view["modules"]},
    }
