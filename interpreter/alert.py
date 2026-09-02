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

    tenant_id = state.get("tenant_id")
    mention = config.get("mention") or {}
    slack_uid = mention.get("slack_user_id") or _pick(mention.get("slack_user_by_team"), team, "default")
    sf_uid = mention.get("sf_user_id")
    if not sf_uid and (mention.get("sf_team") or team):
        from interpreter import routing
        qref = mention.get("sf_queue") or f"Team_{(mention.get('sf_team') or team).capitalize()}"
        sf_uid = routing.queue_member(qref, tenant_id)[0]
    sf_uid = sf_uid or mention.get("mention_id")
    # map the SF agent -> their Slack account by email, so the bot can DM/@them
    if not slack_uid and sf_uid and _looks_id(sf_uid):
        slack_uid = slack.lookup_user_by_email(
            salesforce.user_email(sf_uid, tenant_id=tenant_id) or "", tenant_id=tenant_id)

    want = config.get("channel") or "both"
    # Phase 27e — category -> #cx-* channel + @cx-*-oncall usergroup from
    # notify_targets. Flow config still wins if it names a channel explicitly.
    cls = state.get("classification") or {}
    route = {}
    try:
        from interpreter import routing
        route = routing.resolve_slack_route(
            tenant_id, routed_team=team or None,
            case_type=cls.get("case_type"), module=(state.get("sf_writeback") or {}).get("written", {}).get("Module__c"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("slack route lookup failed: %s", e)
    slack_ch = (_pick(config.get("slack_channel_by_team"), team, "default")
                or config.get("slack_channel") or route.get("channel"))
    # Phase 27e — resolve the on-call handle to `<!subteam^ID>` so Slack
    # actually pages the group (a bare `@handle` in text is inert).
    usergroup = slack.usergroup_ref(route.get("usergroup"), tenant_id=tenant_id)
    out: dict[str, Any] = {"mention": {"slack": slack_uid, "sf": sf_uid},
                           "channel": want, "route": route}

    # Phase 24 — the bot has NOT answered the customer. The Slack post is the
    # root of the reasoning thread; the agent replies `take` in it to start.
    # Phase 27e — @mention the on-call usergroup and carry the context an
    # agent needs before they claim the case in Omni.
    who = (f"<@{slack_uid}>" if slack_uid
           else f"{usergroup} " if usergroup else "the responsible agent")
    tier = state.get("tier") or "basic"
    ctype = cls.get("case_type") or "?"
    ctx_bits = [f"tier `{tier}`", f"type `{ctype}`", f"team `{team or 'support'}`"]
    if conf is not None:
        ctx_bits.append(f"confidence `{conf}`")
    prior = state.get("prior_resolutions") or []
    prior_line = ""
    if prior:
        prior_line = "\n_Nearest past resolutions:_ " + "; ".join(
            f"{(p.get('case_number') or p.get('case_sf_id') or '?')}" for p in prior[:3])
    root = (
        f":thread: {who} — Case *{cn}*  ·  _{subject}_\n"
        f"{' · '.join(ctx_bits)}\n"
        f"Triaged (type/priority written to the Case). *I have not replied to the "
        f"customer.* Claim it in Omni, then reply here — **@mention me** or type "
        f"`take` — to reason through the response.{prior_line}\n"
        f"{_sf_link(sf_id) if sf_id else ''}"
    )

    if want in ("both", "slack") and (slack_ch or config.get("slack_webhook")
                                      or _has_alert_webhook()):
        out["slack"] = slack.post_message(
            root, tenant_id=tenant_id, channel=slack_ch,
            webhook=config.get("slack_webhook"),
            blocks=_handoff_card(root, has_draft=bool((state.get("draft") or "").strip())),
        )
        sl = out["slack"]
        if sl.get("sent") and sl.get("channel") and sl.get("ts"):
            out["reasoning_session"] = _open_session(
                state, config, sf_uid=sf_uid, slack_uid=slack_uid,
                slack_channel=sl["channel"], slack_thread_ts=sl["ts"])

    if want in ("both", "salesforce_chatter") and sf_id:
        mid = sf_uid if _looks_id(sf_uid) else None
        body = (f"Support bot has triaged this Case and flagged it for you"
                f"{' in Slack' if out.get('reasoning_session') else ''}. "
                f"It has **not** replied to the customer — we'll reason through "
                f"the response together before anything is sent.")
        out["chatter"] = salesforce.post_chatter(sf_id, body, mention_id=mid, tenant_id=tenant_id)

    return out


def _open_session(state: dict, config: dict, *, sf_uid, slack_uid,
                  slack_channel, slack_thread_ts) -> str | None:
    """Create the reasoning session and stamp the Slack thread on it."""
    try:
        from ingestion.scraper import get_supabase
        from interpreter import reasoning

        sb = get_supabase()
        case = state.get("case") or {}
        cls = state.get("classification") or {}
        # `case_lookup` writes state.investigation_hints (list[str]); retrieval
        # is a list[dict] of chunks. The old code did `(list).get("docs")` and
        # threw whenever retrieval was non-empty — which killed the session on
        # every escalation that had KB matches.
        kb_hits = (
            list(state.get("investigation_hints") or [])
            or [(r.get("heading_path") or r.get("doc_url") or "")
                for r in (state.get("retrieval") or []) if isinstance(r, dict)][:6]
            or None
        )
        sess = reasoning.open_session(
            sb, case=case, tenant_id=state.get("tenant_id"),
            run_id=state.get("run_id"),
            case_type=cls.get("case_type") or state.get("case_type"),
            case_number=(state.get("sf_case") or {}).get("case_number") or case.get("case_number"),
            agent_sf_id=sf_uid, agent_slack_id=slack_uid,
            kb_hits=kb_hits,
            max_rounds=config.get("max_rounds"),
        )
        sb.table("reasoning_sessions").update({
            "slack_channel": slack_channel, "slack_thread_ts": slack_thread_ts,
            "agent_slack_id": slack_uid, "updated_at": "now()",
        }).eq("session_id", sess["session_id"]).execute()
        return sess["session_id"]
    except Exception as e:  # noqa: BLE001
        log.warning("could not open reasoning session: %s", e)
        return None


def _handoff_card(body: str, *, has_draft: bool) -> list[dict]:
    """Phase 27h — the reasoning-thread root as a Block Kit card with buttons.
    Socket Mode delivers the clicks as `interactive` envelopes; slack_socket
    routes them via `dispatch_action`."""
    els = [
        {"type": "button", "action_id": "cx_edit",
         "text": {"type": "plain_text", "text": "Edit in thread"}},
        {"type": "button", "action_id": "cx_reassign",
         "text": {"type": "plain_text", "text": "Reassign…"}},
        {"type": "button", "action_id": "cx_not_my_team",
         "text": {"type": "plain_text", "text": "Not my team"}},
    ]
    if has_draft:
        els.insert(0, {"type": "button", "action_id": "cx_send", "style": "primary",
                       "text": {"type": "plain_text", "text": "Send as-is"}})
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "actions", "block_id": "cx_handoff", "elements": els},
    ]


def _looks_id(v: Any) -> bool:
    return isinstance(v, str) and len(v) in (15, 18) and v.isalnum()


def _has_alert_webhook() -> bool:
    import os
    return bool(os.environ.get("SLACK_ALERT_WEBHOOK"))
