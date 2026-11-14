"""Copy-ready drafts from templates. Pure, and never invents a fact.

A draft is only offered when the facts it needs exist, and each one lists the
facts it used with their sources.
"""

from __future__ import annotations

from typing import Any

from .redact import clean
from .verdict import ENGINEERING, THIRD_PARTY, US, age_text


def _fact(label: str, value: Any, source: str, url: str | None) -> dict:
    return {"label": label, "value": str(value), "source": {"label": source, "url": url}}


def _case_fact(case: dict) -> dict:
    return _fact("Case", case.get("case_number") or case.get("id"), "Salesforce: CaseNumber",
                 case.get("url"))


def _latest_external(evidence: list[dict]) -> tuple[dict, dict] | None:
    pairs = [(ev, m) for ev in evidence if ev.get("ok") for m in ev.get("messages") or []
             if not m.get("ours") and m.get("at")]
    return max(pairs, key=lambda p: p[1]["at"]) if pairs else None


def _hours_text(verdict: dict) -> str:
    hours = verdict.get("stale_hours")
    return f"No update for {age_text(hours)}. " if hours is not None else ""


def build(case: dict, verdict: dict, evidence: list[dict], cfg: dict) -> list[dict]:
    number = case.get("case_number") or case.get("id")
    subject = case.get("subject") or "(no subject)"
    third = cfg.get("third_party_label", "Third party")
    drafts: list[dict] = []

    partner = case.get("partner") or {}
    channel_email = partner.get("channel") == "email"
    if channel_email and verdict.get("not_raised"):
        template = partner.get("raise_template") or f"Title: {subject}\nIssue Description:"
        drafts.append({
            "id": "raise_email", "title": f"Raise to {partner['name']}", "review_required": True,
            "to": partner.get("to_emails") or [], "cc": partner.get("cc_emails") or [],
            "text": template.replace("{title}", subject),
            "facts": [_case_fact(case), _fact("Recipients", partner["name"],
                                              "Aggregator directory", None)],
        })

    gh = next((e for e in evidence if e["kind"] == "github" and e.get("ok")), None)
    if gh:
        drafts.append({
            "id": "github_nudge", "title": "GitHub nudge",
            "text": f"Hi team, any update on this? It's blocking case {number} ({subject}). "
                    f"{_hours_text(verdict)}Please share the current status and an ETA.",
            "facts": [_case_fact(case), _fact("Issue", gh.get("label") or gh["url"], "GitHub",
                                              gh["url"])],
        })

    slack = next((e for e in evidence if e["kind"] == "slack" and e.get("ok")), None)
    if case.get("waiting_party") == THIRD_PARTY:
        names = ", ".join(case.get("third_parties") or []) or third
        facts = [_case_fact(case)]
        if slack:
            facts.append(_fact("Thread", slack["url"], "Slack", slack["url"]))
        drafts.append({
            "id": "third_party_followup", "title": f"{third} follow-up",
            **({"to": partner.get("to_emails") or [], "cc": partner.get("cc_emails") or []}
               if channel_email else {}),
            "text": f"Hi {names} team, following up on {subject} (our ref {number}). "
                    f"{_hours_text(verdict)}Could you share an update?",
            "facts": facts,
        })

    latest = _latest_external(evidence)
    if latest:
        ev, msg = latest
        drafts.append({
            "id": "customer_update", "title": f"{cfg.get('customer_label', 'Customer')} update",
            "review_required": True,
            "text": f"Hi, here's the latest on {subject} (case {number}):\n\n"
                    f"{clean(msg.get('text'))}\n\n"
                    "We'll keep you posted on next steps.",
            "facts": [_case_fact(case),
                      _fact("Latest reply", msg.get("who"), ev["kind"].title(),
                            msg.get("url") or ev.get("url"))],
        })

    if case.get("owner"):
        drafts.append({
            "id": "agent_nudge", "title": "Message to the agent",
            "text": f"Hi {case['owner']}, please check case {number} ({subject}). "
                    f"Next action: {verdict['next_action']}",
            "facts": [_case_fact(case),
                      _fact("Owner", case["owner"], "Salesforce: Owner", case.get("url"))],
        })

    drafts.append({
        "id": "handoff_note", "title": "Internal handoff note",
        "text": f"Case {number}: {subject}\n"
                f"Status: {case.get('waiting_raw') or 'not waiting on a dependency'}\n"
                f"Next action: {verdict['next_action']}",
        "facts": [_case_fact(case)],
    })

    first = suggested_id(case, verdict, drafts)
    return sorted(drafts, key=lambda d: d["id"] != first)


def suggested_id(case: dict, verdict: dict, drafts: list[dict]) -> str | None:
    """The draft the `c` shortcut copies: the one that fits the next action."""
    ids = {d["id"] for d in drafts}
    ball = verdict["ball"]
    if "raise_email" in ids:
        return "raise_email"
    if ball == US and "customer_update" in ids:
        return "customer_update"
    if ball == ENGINEERING and "github_nudge" in ids:
        return "github_nudge"
    if ball == THIRD_PARTY and "third_party_followup" in ids:
        return "third_party_followup"
    return "handoff_note"
