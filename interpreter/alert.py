"""
Phase 23d — `alert_human`: ping a named person about an escalated Case, on
Slack and/or Salesforce Chatter. Backs the `notify_human` node so the
workflow (not hard-coded config) decides who gets tagged and where.

Person resolution, in order:
  slack:   mention.slack_user_id  ->  mention.slack_user_by_team[routed_team]
  chatter: mention.sf_user_id     ->  mention.sf_team (a queue member, via
           routing.queue_member)  ->  mention.mention_id
Channel resolution:
  slack:   config.slack_channel  ->  config.slack_channel_by_team[routed_team]
Channels used: config.channel = "both" | "slack" | "salesforce_chatter"
(default: whatever is configured — Slack if a channel/webhook resolves,
Chatter if there's an sf_id).
"""

from __future__ import annotations

import logging
from typing import Any

from interpreter import salesforce, slack

log = logging.getLogger("interpreter.alert")


def _sf_link(sf_id: str) -> str:
    try:
        inst = getattr(salesforce.client_for(None), "sf_instance", "")
        base = f"https://{inst}" if inst else ""
    except Exception:  # noqa: BLE001
        base = ""
    return f"{base}/lightning/r/Case/{sf_id}/view" if base else f"Case {sf_id}"


def _pick(d: dict | None, team: str, default_key: str) -> str | None:
    d = d or {}
    return d.get(team) or d.get(default_key) or None


def alert_human(state: dict, config: dict) -> dict[str, Any]:
    """Post the escalation to the configured channel(s), tagging the person.
    Returns {slack, chatter, mention} — every leg best-effort."""
    case = state.get("case") or {}
    sf_id = case.get("sf_id") or case.get("id")
    team = state.get("routed_team") or ""
    outcome = (state.get("outcome") or {}).get("action") or "escalation"
    draft = state.get("draft") or ""
    conf = state.get("confidence")
    subject = case.get("subject") or "(no subject)"
    cn = (state.get("sf_case") or {}).get("case_number") or case.get("case_number") or sf_id or "?"

    mention = config.get("mention") or {}
    slack_uid = mention.get("slack_user_id") or _pick(mention.get("slack_user_by_team"), team, "default")
    sf_uid = mention.get("sf_user_id")
    if not sf_uid and (mention.get("sf_team") or team):
        from interpreter import routing
        qref = mention.get("sf_queue") or f"Team_{(mention.get('sf_team') or team).capitalize()}"
        sf_uid = routing.queue_member(qref, state.get("tenant_id"))[0]
    sf_uid = sf_uid or mention.get("mention_id")

    want = config.get("channel") or "both"
    slack_ch = _pick(config.get("slack_channel_by_team"), team, "default") or config.get("slack_channel")
    out: dict[str, Any] = {"mention": {"slack": slack_uid, "sf": sf_uid}, "channel": want}

    tmpl = config.get("note_tmpl") or (
        "*Support bot needs a human* — Case *{cn}* ({outcome}, confidence {conf})\n"
        "> {subject}\n{who}\nSuggested draft:\n```{draft}```\n{link}"
    )

    if want in ("both", "slack") and (slack_ch or config.get("slack_webhook")
                                      or _has_alert_webhook()):
        who = f"<@{slack_uid}> please take a look" if slack_uid else "please pick this up"
        text = tmpl.format(cn=cn, outcome=outcome, conf=conf, subject=subject, who=who,
                           draft=(draft or "(none)")[:2500], link=_sf_link(sf_id) if sf_id else "")
        out["slack"] = slack.post_message(
            text, tenant_id=state.get("tenant_id"), channel=slack_ch,
            webhook=config.get("slack_webhook"),
        )

    if want in ("both", "salesforce_chatter") and sf_id:
        who = "please review the suggested reply before it goes to the customer."
        body = tmpl.format(cn=cn, outcome=outcome, conf=conf, subject=subject, who=who,
                           draft=(draft or "(none)")[:3000], link="").replace("```", "")
        mid = sf_uid if salesforce and _looks_id(sf_uid) else None
        out["chatter"] = salesforce.post_chatter(sf_id, body, mention_id=mid,
                                                 tenant_id=state.get("tenant_id"))

    # When the upstream ask_human/handover node is routing-only (post_note:false)
    # the reviewable draft would otherwise be lost — drop it as one private
    # CaseComment so the agent can copy it into the Email quick action.
    if config.get("draft_comment") and sf_id and draft.strip():
        out["draft_comment"] = salesforce.add_case_comment(
            sf_id, f"[bot draft — review before sending]\n\n{draft}",
            published=False, tenant_id=state.get("tenant_id"),
        )

    return out


def _looks_id(v: Any) -> bool:
    return isinstance(v, str) and len(v) in (15, 18) and v.isalnum()


def _has_alert_webhook() -> bool:
    import os
    return bool(os.environ.get("SLACK_ALERT_WEBHOOK"))
