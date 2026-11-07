"""Rule-based verdict: whose turn is it on a case? Pure functions only.

The verdict is authoritative: an LLM may summarise or draft, never decide.

Precedence when signals disagree: any signal saying "us" wins, then a sub-status
saying the customer must confirm, then the party the case says it is waiting
on, then any other party, then unknown.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

US, ENGINEERING, THIRD_PARTY, AI, CUSTOMER, UNKNOWN = (
    "us", "engineering", "third_party", "ai", "customer", "unknown")
DEPENDENCY_PARTIES = (ENGINEERING, THIRD_PARTY)


# What a real third-party reply asks of us.
REPLY_ACTION = {
    "bug_raised": ("reply_bug_raised",
                   "{third} raised this as a bug: update the {customer} and track the fix."),
    "solution": ("reply_solution", "{third} shared a solution: verify it and update the {customer}."),
    "needs_info": ("reply_needs_info", "{third} needs more details: gather them and reply."),
    "update": ("reply_update", "Read the {third} update and act on it."),
    "no_answer": ("reply_no_answer", "{third} gave no real answer: push them for the reason."),
}


def parse_dt(value: Any) -> datetime | None:
    """Parse API datetimes, including Salesforce's "...+0000" offset."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    if len(s) > 5 and s[-5] in "+-" and s[-3] != ":":
        s = f"{s[:-2]}:{s[-2:]}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_text(hours: float) -> str:
    return f"{hours:g}h" if hours < 48 else f"{round(hours / 24)}d"


def _hours(later: datetime, earlier: datetime) -> float:
    return round((later - earlier).total_seconds() / 3600, 1)


def _last(messages: list[dict]) -> dict | None:
    dated = [m for m in messages if parse_dt(m.get("at"))]
    return max(dated, key=lambda m: parse_dt(m["at"])) if dated else None


def in_domains(domain: str, domains: list[str]) -> bool:
    """Exact or subdomain match, so external.example.com counts as example.com."""
    domain = domain.lower()
    return any(domain == d.lower() or domain.endswith(f".{d.lower()}") for d in domains)


def email_party(address: str | None, incoming: bool, case: dict, cfg: dict) -> str:
    """Who sent an email, by its domain: our team, the third party, or else the
    customer, who can be on any domain, such as gmail.com or their own company."""
    if not incoming:
        return US
    domain = (address or "").rsplit("@", 1)[-1].strip(" >").lower()
    if in_domains(domain, cfg.get("internal_email_domains") or []):
        return US
    partner = case.get("partner") or {}
    if in_domains(domain, partner.get("email_domains") or []):
        return THIRD_PARTY
    names = [partner.get("name"), *(partner.get("aliases") or []), *(case.get("third_parties") or [])]
    slugs = {re.sub(r"[^a-z0-9]", "", (n or "").lower()) for n in names}
    # Very short names like "NA" would match inside unrelated domains.
    if any(len(s) >= 4 and s in domain for s in slugs):
        return THIRD_PARTY
    return CUSTOMER


def threshold_hours(case: dict, cfg: dict) -> float | None:
    party = case.get("waiting_party")
    if party == ENGINEERING:
        return float(cfg["eng_nudge_hours"])
    if party != THIRD_PARTY:
        return None
    default = float(cfg["third_party_nudge_hours"])
    overrides = {k.lower(): float(v) for k, v in (cfg.get("third_party_overrides") or {}).items()}
    named = [overrides[n.lower()] for n in case.get("third_parties") or [] if n.lower() in overrides]
    directory = (case.get("partner") or {}).get("escalate_after_hours")
    if directory:
        named.append(float(directory))
    return min(named) if named else default


def _source(label: str, url: str | None) -> dict:
    return {"label": label, "url": url}


def _signals(case: dict, evidence: list[dict], cfg: dict) -> list[dict]:
    """Each signal is one observation naming the party whose turn it is."""
    out: list[dict] = []
    waiting = case.get("waiting_party")
    third = cfg.get("third_party_label", "Third party")
    customer = cfg.get("customer_label", "Customer")
    case_url = case.get("url")
    reply_kind = (case.get("reply") or {}).get("kind")

    ai = case.get("ai") or {}
    src = _source("AI agent run", ai.get("url"))
    if ai.get("state") == "handed_off":
        out.append({"ball": US, "code": "ai_handoff", "source": src,
                    "text": f"AI agent handed this to a human ({ai.get('outcome')})",
                    "action": "Take over the case from the AI agent's handoff."})
    elif ai.get("state") == "working":
        out.append({"ball": AI, "code": "ai_working", "source": src,
                    "text": "An AI agent is still working on this case",
                    "action": "No action needed yet: the AI agent has this case."})
    elif ai.get("state") == "awaiting_customer":
        out.append({"ball": CUSTOMER, "code": "ai_awaiting_customer", "source": src,
                    "text": "AI agent asked the customer for more information",
                    "action": f"Wait for the {customer.lower()}'s answer."})

    # On the raising record type, any incoming email sets this field, including
    # the third party's, so the email's sender decides there instead.
    raised_case = case.get("record_type") == cfg.get("dependency_record_type")
    customer_spoke = case.get("last_reply_from") == "customer" and not raised_case
    if customer_spoke:
        field = cfg["fields"].get("last_reply_from") or "last reply"
        out.append({"ball": US, "code": "customer_replied",
                    "source": _source(f"Salesforce: {field}", case_url),
                    "text": f"{customer} replied last",
                    "action": f"Read the {customer.lower()}'s reply and respond."})

    # The raising record type tracks only the waiting field, not a sub-status.
    raised = case.get("record_type") == cfg.get("dependency_record_type")
    sub = None if raised else case.get("sub_status")
    sub_source = _source(f"Salesforce: {cfg['fields'].get('waiting_sub_status') or 'sub-status'}",
                         case_url)
    awaiting_customer = sub in (cfg.get("customer_wait_sub_statuses") or [])
    if awaiting_customer:
        out.append({"ball": CUSTOMER, "code": "awaiting_customer_report", "source": sub_source,
                    "text": f"Solution given, marked {sub}",
                    "action": f"Follow up with the {customer.lower()} to confirm the fix."})
    if sub in (cfg.get("team_replied_sub_statuses") or []):
        out.append({"ball": US, "code": "team_replied", "source": sub_source,
                    "text": f"Marked {sub}",
                    "action": "Read the team's reply and act on it."})

    parent = case.get("parent") or {}
    if waiting == THIRD_PARTY and parent.get("closed") and \
            parent.get("record_type") == cfg.get("dependency_record_type") and \
            case.get("record_type") != parent.get("record_type"):
        out.append({"ball": US, "code": "dsp_resolved",
                    "source": _source(f"Salesforce: case {parent.get('case_number')}",
                                      parent.get("url")),
                    "text": f"Linked {parent['record_type']} case {parent.get('case_number')} "
                            f"is {parent.get('status')}",
                    "action": f"Update the {customer.lower()} and close this case."})

    modified = parse_dt(case.get("last_modified_at"))
    for ev in evidence:
        if not ev.get("ok"):
            continue
        last = _last(ev.get("messages") or [])
        # Once a case is marked as waiting for the customer, a reply is only
        # news if it arrived after the team last updated the case.
        at = parse_dt((last or {}).get("at"))
        if awaiting_customer and not (at and modified and at > modified):
            continue
        src = _source(ev.get("label") or ev["kind"], (last or {}).get("url") or ev.get("url"))
        if ev["kind"] == "github":
            if ev.get("state") == "closed":
                out.append({"ball": US, "code": "github_closed", "source": src,
                            "text": "GitHub issue is closed",
                            "action": f"Verify the fix with the {customer.lower()}, "
                                      "then update the case."})
            elif last and not last.get("ours"):
                out.append({"ball": US, "code": "engineer_replied", "source": src,
                            "text": f"{last.get('who')} replied on GitHub",
                            "action": "Read the engineer's reply and act on it."})
            else:
                out.append({"ball": ENGINEERING, "code": "waiting_engineer", "source": src,
                            "text": "Last GitHub comment is ours" if last
                                    else "GitHub issue has no comments yet",
                            "action": "Wait for Engineering; nudge if it goes quiet."})
        elif ev["kind"] in ("slack", "email") and waiting == THIRD_PARTY and last \
                and not ev.get("internal"):
            channel = "Slack" if ev["kind"] == "slack" else "email"
            if last.get("party") == CUSTOMER:
                out.append({"ball": US, "code": "customer_replied_email", "source": src,
                            "text": f"{customer} ({last.get('who')}) replied on the email thread",
                            "action": f"Read the {customer.lower()}'s email and respond."})
            elif not last.get("ours") and reply_kind in ("ack", "internal_tag"):
                out.append({"ball": THIRD_PARTY, "code": "third_party_ack", "source": src,
                            "text": f"{third} only acknowledged; no real update yet",
                            "action": f"Wait for a real update from {third}."})
            elif last.get("ours"):
                out.append({"ball": THIRD_PARTY, "code": "waiting_third_party", "source": src,
                            "text": f"Our {channel} message is the latest; waiting on {third}",
                            "action": f"Wait for {third}; follow up if it goes quiet."})
            else:
                code, action = REPLY_ACTION.get(reply_kind) or (
                    "third_party_replied", "Read the {third} reply and act on it.")
                out.append({"ball": US, "code": code, "source": src,
                            "text": (case.get("reply") or {}).get("summary")
                            or f"{last.get('who')} replied by {channel}",
                            "action": action.format(third=third, customer=customer.lower())})
    return out


def _how_to_raise(partner: dict | None, third: str) -> str:
    """One line on where this third party takes new issues, from the directory."""
    if not partner:
        return f"Raise it with {third} (DSP case plus email, Slack or Jira) and record the link."
    name, channel = partner["name"], partner.get("channel")
    if channel == "email":
        return f"Raise it with {name} by email from the DSP case."
    if channel == "slack":
        return f"Raise it with {name} on Slack and add the thread link."
    if channel == "jira":
        return f"Raise it with {name} on their Jira portal and add the ticket link."
    if channel == "portal":
        return f"Raise it with {name} on their portal and add the ticket link."
    return f"Raise it with {name} on {channel}."


def _pick_ball(signals: list[dict], waiting: str | None) -> dict | None:
    def rank(s: dict) -> int:
        if s["ball"] == US:
            return 0
        if s["code"] == "awaiting_customer_report":
            return 1
        return 2 if s["ball"] == waiting else 3
    return min(signals, key=rank) if signals else None


def latest_message(evidence: list[dict]) -> dict | None:
    """The newest message across every readable source, for the queue row."""
    found = [(m, ev) for ev in evidence if ev.get("ok") for m in ev.get("messages") or []
             if parse_dt(m.get("at"))]
    if not found:
        return None
    msg, ev = max(found, key=lambda pair: parse_dt(pair[0]["at"]))
    return {"who": msg.get("who"), "ours": bool(msg.get("ours")), "at": msg.get("at"),
            "text": msg.get("text"), "url": msg.get("url") or ev.get("url"),
            "source": ev.get("label") or ev.get("kind"),
            "party": msg.get("party") or (US if msg.get("ours") else
                                          ENGINEERING if ev.get("kind") == "github"
                                          else THIRD_PARTY)}


def stage(verdict: dict) -> str:
    """The one bucket a case sits in, so the queue's tabs always add up."""
    ball = verdict["ball"]
    if ball == US:
        return "act"
    if verdict.get("not_raised") or verdict.get("missing_link"):
        return "raise"
    if ball in (ENGINEERING, THIRD_PARTY, CUSTOMER, AI):
        return ball
    return "check"


def score_level(score: int) -> str:
    if score >= 60:
        return "critical"
    if score >= 40:
        return "high"
    return "medium" if score >= 20 else "low"


def compute(case: dict, evidence: list[dict], cfg: dict, now: datetime) -> dict[str, Any]:
    waiting = case.get("waiting_party")
    case_url = case.get("url")
    sf = lambda key: _source(f"Salesforce: {cfg['fields'].get(key) or key}", case_url)  # noqa: E731
    signals = _signals(case, evidence, cfg)
    reasons: list[dict] = []
    score = 0

    def add(points: int, code: str, text: str, source: dict) -> None:
        nonlocal score
        score += points
        reasons.append({"code": code, "text": text, "points": points, "source": source})

    pick = _pick_ball(signals, waiting)
    ball = pick["ball"] if pick else UNKNOWN
    action = pick["action"] if pick else None
    third = cfg.get("third_party_label", "Third party")

    target = parse_dt(case.get("target_at"))
    overdue_hours = _hours(now, target) if target and target < now else 0.0
    if overdue_hours:
        days = int(overdue_hours // 24)
        add(30 + min(30, 2 * days), "overdue", f"Overdue by {days}d", sf("target_at"))

    mismatch = ball == US and waiting in DEPENDENCY_PARTIES
    if mismatch:
        add(25, "mismatch", f"Case says waiting on {case.get('waiting_raw')}, but it is our turn",
            pick["source"])

    modified = parse_dt(case.get("last_modified_at"))
    theirs = [parse_dt(m.get("at")) for ev in evidence if ev.get("ok")
              for m in ev.get("messages") or [] if not m.get("ours")]
    theirs = [t for t in theirs if t]
    newest_theirs = max(theirs, default=None)
    if newest_theirs and modified and newest_theirs > modified:
        add(15, "new_reply", "External reply is newer than the case's last update",
            pick["source"] if pick else sf("last_modified_at"))

    raised_by_email = any(e["kind"] == "email" and e.get("messages") for e in evidence)
    links_present = bool(case.get("links")) or raised_by_email
    partner = case.get("partner")
    channel = (partner or {}).get("channel")
    directory = _source("Aggregator directory", None)
    missing_link = not_raised = False
    if waiting in DEPENDENCY_PARTIES and not links_present:
        if waiting == THIRD_PARTY and case.get("has_dsp") is False:
            not_raised = True
            add(20, "no_dsp_case", "No DSP case raised", sf("link"))
            how = "Raise a DSP case first. " + _how_to_raise(partner, third)
        elif channel == "whatsapp":
            how = f"Check the {partner['name']} WhatsApp group by hand: it can't be read here."
            add(0, "whatsapp_channel", "Raised on WhatsApp, which can't be read", directory)
        elif channel == "email":
            not_raised = True
            add(20, "no_email_raised", f"No email to {partner['name']} on the DSP case", directory)
            how = _how_to_raise(partner, third)
        else:
            missing_link = True
            add(20, "missing_link", "Missing external reference link", sf("link"))
            how = ("Raise it with Engineering via the on-call channel and record the link."
                   if waiting == ENGINEERING else _how_to_raise(partner, third))
        if not pick or pick["ball"] != US:
            ball = UNKNOWN
            action = how

    times = [parse_dt(m.get("at")) for ev in evidence if ev.get("ok")
             for m in ev.get("messages") or []]
    latest = max([t for t in times if t] + ([modified] if modified else []), default=None)
    stale_hours = _hours(now, latest) if latest else None
    limit = threshold_hours(case, cfg)
    stale = bool(limit is not None and stale_hours is not None and stale_hours > limit)
    if stale:
        add(20, "stale", f"Quiet for {age_text(stale_hours)} (nudge after {age_text(limit)})",
            pick["source"] if pick else sf("last_modified_at"))
        if ball in DEPENDENCY_PARTIES:
            who = "Engineering" if ball == ENGINEERING else third
            action = f"Nudge {who}: no update in {age_text(stale_hours)}."
            note = (partner or {}).get("escalation_note") or cfg.get("third_party_escalation")
            if ball == THIRD_PARTY and note:
                action = f"{action} {note}"

    if case.get("last_reply_from") == "customer" and \
            case.get("record_type") != cfg.get("dependency_record_type"):
        add(15, "customer_replied", f"{cfg.get('customer_label', 'Customer')} replied last",
            sf("last_reply_from"))

    points = {"urgent": 10, "high": 5}.get((case.get("priority") or "").lower())
    if points:
        add(points, "priority", f"{case.get('priority')} priority", sf("priority"))

    unreadable = [e for e in evidence if e.get("error")]
    for ev in unreadable:
        add(5, "unreadable_link", f"Link not readable: {ev['error']}",
            _source(ev.get("label") or ev["kind"], ev.get("url")))
    if ball == UNKNOWN and not action:
        action = ("Fix link access, or check the linked thread by hand."
                  if unreadable else "Check the linked item by hand: its status isn't read.")

    # Signals that didn't move the score still explain the ball.
    shown = {r["code"] for r in reasons}
    for s in signals:
        if s["code"] not in shown:
            reasons.append({"code": s["code"], "text": s["text"], "points": 0,
                            "source": s["source"]})

    return {"ball": ball, "mismatch": mismatch, "score": score, "level": score_level(score),
            "reasons": reasons, "next_action": action or "No action needed right now.",
            "stale_hours": stale_hours, "stale": stale, "overdue_hours": overdue_hours,
            "overdue": overdue_hours > 0, "missing_link": missing_link, "not_raised": not_raised,
            "signal": {k: pick[k] for k in ("code", "text", "source")} if pick else None,
            "latest": latest_message(evidence),
            "latest_activity_at": latest.isoformat() if latest else None,
            "latest_reply_at": newest_theirs.isoformat() if newest_theirs else None}
