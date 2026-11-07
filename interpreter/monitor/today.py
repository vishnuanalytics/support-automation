"""What needs the lead right now, with the next step ready. Pure.

Everything else stays out of sight. Marking a card done hides it until something
new happens, or for a day at most, so nothing can be lost by clearing it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import dsp_sla, issue_status as st
from . import verdict
from .verdict import parse_dt

DONE_FOR_HOURS = 24
HELP, ACT, ESCALATE_URGENT, LOGS, CHASE, CLOSE, ESCALATE, FOLLOW = range(8)
_WHO = {"merchant": "the merchant", "third_party": "the aggregator"}
_STEP_BY_PARTY = {verdict.CUSTOMER: "Reply to the merchant",
                  verdict.ENGINEERING: "Read the engineer's reply and update the merchant"}
_STEP_READ = "Read the {third} reply and act on it"
_STEP_BY_REPLY = {"no_answer": "Push {third} for a real answer", "needs_info": "Send {third} what they asked for",
                  "solution": "Check the {third} answer and update the merchant",
                  "bug_raised": "Track the {third} fix and update the merchant"}
REPLIES, SLA, AGENTS = "replies", "sla", "agents"
GROUPS = {"act": REPLIES, "reply": REPLIES, "escalate": SLA, "chase": SLA, "help": AGENTS, "logs": AGENTS,
          "close": AGENTS, "follow": AGENTS}


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


def _logs_card(p: dict) -> dict:
    rows = p["rows"]
    count = f"{len(rows)} {p['name']} case{'' if len(rows) == 1 else 's'}"
    gone = f" {p['gone']} older may already be gone." if p["gone"] else ""
    return {"key": f"logs:{p['name']}", "rank": LOGS, "kind": "logs", "title": f"Save logs for {count}",
            "why": f"{p['name']} keeps logs for {p['days']:g} days. First: {rows[0]['subject']}.{gone}",
            "chip": f"{dsp_sla.span(rows[0]['days_left'] * 24)} left", "tone": "exception" if rows[0]["days_left"] <= 1 else "warn",
            "draft": p["draft"], "draft_label": "Message for the agents",
            "activity_at": max(r["raised_at"] for r in rows), "case_ids": [r["case_id"] for r in rows]}


def _step(case: dict, latest: dict) -> str:
    """The next move in a few words, so an agent can read a list at a glance."""
    if latest.get("party") in _STEP_BY_PARTY:
        return _STEP_BY_PARTY[latest["party"]]
    third = (case.get("third_parties") or ["the aggregator"])[0]
    return _STEP_BY_REPLY.get((case.get("reply") or {}).get("kind"), _STEP_READ).format(third=third)


def _one_per_reply(rows: list[tuple], cfg: dict) -> list[list[tuple]]:
    """Linked cases share an email thread, so one reply can sit on two cases. It's one job,
    and it goes to whoever owns the case raised with the aggregator."""
    same: dict[tuple, list[tuple]] = {}
    for row in rows:
        latest = row[2]
        same.setdefault((latest.get("at"), latest.get("who") or row[1]["id"]), []).append(row)
    record_type = cfg.get("dependency_record_type")
    return [sorted(g, key=lambda r: r[1].get("record_type") != record_type) for g in same.values()]


def _reply_line(group: list[tuple], step: str) -> str:
    main = group[0][1]
    others = [f"{c.get('case_number')}" + (f" ({c.get('owner')})" if c.get("owner") != main.get("owner") else "")
              for _, c, _ in group[1:]]
    numbers = " + ".join([str(main.get("case_number")), *others])
    return f"- {numbers}: {step} ({dsp_sla.span(group[0][0])} ago)"


def reply_cards(items: list[tuple], cfg: dict, now: datetime) -> list[dict]:
    """One card per agent, for outside replies nobody has acted on.

    A card per case would rebuild the queue, and the fix is the same: the agent acts."""
    rows = []
    for case, v, _ in items:
        latest = v.get("latest") or {}
        waited = _hours_since(latest.get("at"), now)
        if verdict.stage(v) == "act" and latest.get("party") not in (None, verdict.US) \
                and waited >= cfg["needs_reply_hours"]:
            rows.append((waited, case, latest))
    owners: dict[str, list[list[tuple]]] = {}
    for group in _one_per_reply(rows, cfg):
        owners.setdefault(group[0][1].get("owner") or "Unassigned", []).append(group)
    cards = []
    for owner, groups in sorted(owners.items(), key=lambda o: -len(o[1])):
        groups.sort(key=lambda g: -g[0][0])
        steps = {c["id"]: _step(g[0][1], g[0][2]) for g in groups for _, c, _ in g}
        lines = [f"Hi {owner}, please do these today:", ""]
        lines += [_reply_line(g, steps[g[0][1]["id"]]) for g in groups]
        n = len(groups)
        oldest = groups[0][0]
        cards.append({"key": f"reply:{owner}", "rank": ACT, "kind": "reply",
                      "title": f"{owner}: {n} repl{'y' if n == 1 else 'ies'} not acted on",
                      "why": f"Oldest: {oldest[1].get('case_number')}, {oldest[1].get('subject')}",
                      "chip": f"{dsp_sla.span(oldest[0])} ago", "tone": "exception" if oldest[0] >= 72 else "warn",
                      "draft": "\n".join(lines), "draft_label": "Message for the agent",
                      "activity_at": max(r[2]["at"] for g in groups for r in g),
                      "case_ids": list(steps), "steps": steps})
    return cards


def _merged(cards: list[dict], kind: str, key: str, title: str, label: str) -> dict:
    """Several GitHub issues as one card, so the list stays a handful of decisions."""
    same = [c for c in cards if c["kind"] == kind]
    lines = [f"- {c.get('subject')}: {c['url']}" for c in same]
    return {"key": key, "rank": min(c["rank"] for c in same), "kind": kind, "group": GROUPS[kind],
            "title": title.format(n=len(same)), "why": same[0].get("why"), "chip": str(len(same)),
            "tone": "exception" if any(c["tone"] == "exception" for c in same) else same[0]["tone"],
            "draft": "\n".join(lines), "draft_label": label,
            "activity_at": max((c["activity_at"] or "" for c in same), default=None),
            "case_ids": [i for c in same for i in c["case_ids"]]}


def _group_github(cards: list[dict]) -> list[dict]:
    for kind, key, title, label in (
            ("chase", "gh:chase", "{n} GitHub issues gone quiet", "Issue list"),
            ("follow", "gh:follow", "{n} GitHub issues waiting on a merchant or partner", "Issue list"),
            ("close", "gh:close", "{n} GitHub issues look ready to close", "Issue list")):
        if sum(c["kind"] == kind for c in cards) > 1:
            cards = [c for c in cards if c["kind"] != kind] + [_merged(cards, kind, key, title, label)]
    return cards


def _hidden(card: dict, reviews: dict[str, dict], now: datetime) -> bool:
    done = parse_dt((reviews.get(card["key"]) or {}).get("reviewed_at"))
    if done is None or now - done >= timedelta(hours=DONE_FOR_HOURS):
        return False
    activity = parse_dt(card.get("activity_at"))
    return activity is None or activity <= done


def build(issues: dict, sla: dict, coaching: dict, follow_ups: dict, open_cases: int,
          reviews: dict[str, dict], cfg: dict, now: datetime, help_view: dict | None = None,
          logs: list[dict] | None = None, replies: list[dict] | None = None) -> dict:
    help_view = help_view or {"cards": [], "away": {"total": 0, "answered": 0}, "modules": []}
    rows = [r for s in issues["squads"] for r in s["issues"]]
    cards = [{**c, "rank": HELP} for c in help_view["cards"]]
    replies = replies or []
    answered_by_agent = {i for c in replies for i in c["case_ids"]}
    cards += replies
    cards += [c for r in rows if (c := _issue_card(r, cfg, now))
              and not (c["kind"] == "act" and c["case_id"] in answered_by_agent)]
    cards += [c for p in sla["partners"] if (c := _partner_card(p))]
    cards += [_logs_card(p) for p in logs or []]
    cards = [{**c, "group": GROUPS[c["kind"]]} for c in _group_github(cards)]
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
