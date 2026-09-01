"""
Phase 24c — the `slackbot` service: a Slack **Socket Mode** client that turns
an agent's in-thread replies into `reasoning.handle_agent_message` turns.

One persistent WebSocket (no public URL, no polling). `notify_human` posts the
root message of a per-Case thread and stores its `ts` on the
`reasoning_sessions` row; the agent replies in that thread; each reply drives
the dialogue, and on approval the drafted reply is sent to the customer.

    python -m interpreter.slack_socket        # run forever
    SLACK_APP_TOKEN=xapp-…  (app-level token, scope connections:write)

`dispatch()` is pure given `post` / `deliver` callables — see tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from dotenv import load_dotenv

load_dotenv()

import requests  # noqa: E402
import websockets  # noqa: E402

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import reasoning, slack  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("interpreter.slack_socket")

_OPEN_URL = "https://slack.com/api/apps.connections.open"
_DEAD_STATES = ("sent", "abandoned")
_MENTION_RE = re.compile(r"^\s*<@[A-Z0-9]+>\s*")
_ROUTE_RE = re.compile(r"^\s*route:\s*(support|tier2|csm|sales|offboarding|billing)\b",
                       re.IGNORECASE)
# single-tenant deployment — the Slack bot token lives on this tenant's row
_TENANT = os.environ.get("DEFAULT_TENANT_ID", "00000000-0000-0000-0000-000000000000")


# ── session lookup ─────────────────────────────────────────────────
def _find_session(sb, thread_ts: str):
    try:
        rows = (sb.table("reasoning_sessions").select("*")
                .eq("slack_thread_ts", thread_ts)
                .not_.in_("state", _DEAD_STATES).execute().data)
        return rows[0] if rows else None
    except Exception as e:  # noqa: BLE001
        log.warning("session lookup failed for thread %s: %s", thread_ts, e)
        return None


# ── deliver the approved draft ─────────────────────────────────────
def _deliver(sb, session: dict) -> dict:
    """Send `session['draft']` to the customer via the same path `auto_reply`
    used, and mark the escalated run resolved. Never raises."""
    from interpreter import agent_reply, mailbox

    case_id = session["case_id"]            # the SF record id (500…)
    tenant_id = session.get("tenant_id")
    _SEL = ("run_id, case_payload, draft, tenant_id, flow_id, team, "
            "human_action, subject, source, created_at")
    runs: list = []
    # runs.case_id holds the CaseNumber (get_case maps it), so match on the
    # record id inside case_payload first, then fall back.
    for q in (lambda: sb.table("runs").select(_SEL).eq("case_payload->>sf_id", case_id),
              lambda: sb.table("runs").select(_SEL).eq("case_id", case_id)):
        try:
            runs = q().order("created_at", desc=True).limit(5).execute().data or []
        except Exception as e:  # noqa: BLE001
            log.warning("run lookup for %s failed: %s", case_id, e)
            runs = []
        if runs:
            break
    run = next((r for r in runs if r.get("source") != "agent_resume"),
              runs[0] if runs else None)
    case = (run or {}).get("case_payload") or {}
    case.setdefault("sf_id", case_id)
    if not case.get("channel"):
        case["channel"] = "salesforce"

    cfg = mailbox.load_channel(tenant_id or session.get("tenant_id"), sb)
    out = agent_reply.resume_from_guidance(
        case, "send it", cfg=cfg, tenant_id=tenant_id, draft=session.get("draft"))

    if run:
        try:
            sb.table("runs").insert({
                "flow_id": run.get("flow_id"), "tenant_id": tenant_id,
                "team": run.get("team"), "source": "slack_reasoning",
                "case_id": str(case_id)[:200],
                "subject": (str(run.get("subject") or "")[:500] or None),
                "outcome": "auto_reply" if out.get("auto_sent") else "draft",
                "draft": out.get("reply"), "case_payload": case,
            }).execute()
            sb.table("runs").update({
                "human_action": "guided_resume",
                "human_reply": "[slack reasoning: approved]",
                "feedback_checked_at": "now()",
            }).eq("run_id", run["run_id"]).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("could not record slack_reasoning run for %s: %s", case_id, e)

    # Phase 27 — the reply landed: move the Case to Resolved + audit the send.
    if out.get("auto_sent"):
        try:
            from interpreter import case_events, salesforce

            salesforce.update_case_fields(case_id, {
                "Status": "Resolved", "Next_Action__c": "resolved via Slack reasoning",
            }, tenant_id=tenant_id)
            case_events.record(
                sb, tenant_id=tenant_id, case_sf_id=str(case_id),
                case_number=session.get("case_number"),
                actor=f"agent:{session.get('agent_slack_id') or '?'}",
                action="send", to_status="Resolved", slack_ts=session.get("slack_thread_ts"),
                reason="approved in the Slack reasoning dialogue")
        except Exception as e:  # noqa: BLE001
            log.warning("could not mark %s Resolved after send: %s", case_id, e)

    return {"sent": bool(out.get("auto_sent")), "via": out.get("via"),
            "error": out.get("error")}


# ── the core (pure given post / deliver) ───────────────────────────
def dispatch(sb, event: dict, *, post, deliver=None, bot_user_id: str | None = None) -> dict:
    """Handle one Slack message event. `post(channel, thread_ts, text)` sends a
    reply; `deliver(sb, session)` ships the approved draft. Returns a summary."""
    # only plain channel/DM messages — an `app_mention` event is a duplicate of
    # the `message.*` event that already carries the same text.
    if event.get("type") != "message":
        return {"skip": f"type={event.get('type')}"}
    if event.get("bot_id") or event.get("subtype"):
        return {"skip": "bot or subtype"}
    if bot_user_id and event.get("user") == bot_user_id:
        return {"skip": "self"}

    raw = (event.get("text") or "").strip()
    mentioned = bool(bot_user_id and f"<@{bot_user_id}>" in raw)
    text = _MENTION_RE.sub("", raw).strip()
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    if not (channel and thread_ts) or not (text or mentioned):
        return {"skip": "incomplete event"}

    session = _find_session(sb, thread_ts)
    if not session:
        return {"skip": "no open session for this thread"}

    # Phase 27h — `route: <team>` from the Reassign / Not-my-team buttons
    m = _ROUTE_RE.match(text)
    if m:
        team = m.group(1).lower()
        r = _reassign(sb, session, team)
        post(channel, thread_ts,
             f":arrows_counterclockwise: Re-routed to *{team}* — Omni will push it to that team."
             if r.get("ok") else f":warning: Couldn't re-route ({r.get('error')}).")
        return {"session_id": session["session_id"], "action": "reassign",
                "routed_team": team, "ok": r.get("ok")}

    out = reasoning.handle_agent_message(sb, session, text, handoff=mentioned or None)
    post(channel, thread_ts, out["reply"])
    res = {"session_id": session["session_id"],
           "state": out["session"]["state"], "action": out.get("action")}

    if out.get("action") == "send":
        d = (deliver or _deliver)(sb, out["session"])
        post(channel, thread_ts,
             ":white_check_mark: Sent to the customer."
             if d.get("sent") else
             f":warning: I couldn't send it ({d.get('error') or d.get('via')}). "
             f"The draft is on the Case.")
        res["delivery"] = d
    return res


_TEAM_QUEUE = {"support": "Team_Support", "tier2": "Support_Tier2", "csm": "Team_CSM",
               "sales": "Team_Sales", "offboarding": "Team_Offboarding",
               "billing": "Billing_Escalations"}


def _reassign(sb, session: dict, team: str) -> dict:
    """`route: <team>` — set Routed_Team__c + owner queue on the Case; Omni
    then re-routes. Also records a routing-correction case_events row."""
    sf_id = session.get("case_id")
    if not sf_id:
        return {"ok": False, "error": "no case id on the session"}
    try:
        from interpreter import case_events, salesforce

        salesforce.update_case_fields(sf_id, {"Routed_Team__c": team, "Status": "Escalated"})
        salesforce.assign_case(sf_id, queue=_TEAM_QUEUE.get(team, "Team_Support"))
        case_events.record(sb, tenant_id=session.get("tenant_id"), case_sf_id=str(sf_id),
                           case_number=session.get("case_number"),
                           actor=f"agent:{session.get('agent_slack_id') or '?'}",
                           action="reassign", to_status="Escalated", routed_team=team,
                           reason="agent reassigned in Slack")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        log.warning("_reassign(%s -> %s): %s", sf_id, team, e)
        return {"ok": False, "error": str(e)[:120]}


_REDRIVE_MSG = (":arrows_counterclockwise: (reconnected) I'm still here — reply to "
                "continue, or @mention me.")


def _redrive_open_sessions(sb, post) -> None:
    """Phase 27 — after a WSS reconnect, re-post a short nudge into every
    reasoning thread that's still open, so a dialogue that stalled while the
    socket was down comes back to life. Best-effort, once per connect."""
    try:
        rows = (sb.table("reasoning_sessions")
                .select("session_id,slack_channel,slack_thread_ts,state")
                .not_.in_("state", _DEAD_STATES).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("redrive: session query failed: %s", e)
        return
    n = 0
    for s in rows:
        ch, ts = s.get("slack_channel"), s.get("slack_thread_ts")
        if ch and ts:
            try:
                post(ch, ts, _REDRIVE_MSG)
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("redrive post failed for %s: %s", s.get("session_id"), e)
    if n:
        log.info("redrive: nudged %d open reasoning thread(s)", n)


def dispatch_action(sb, payload: dict, *, post, deliver=None) -> dict:
    """Phase 27h — one Block Kit button click (Socket Mode `interactive`
    envelope). Pure given `post` / `deliver`."""
    if payload.get("type") != "block_actions":
        return {"skip": f"payload type={payload.get('type')}"}
    actions = payload.get("actions") or []
    if not actions:
        return {"skip": "no actions"}
    action_id = actions[0].get("action_id")
    channel = (payload.get("channel") or {}).get("id")
    cont = payload.get("container") or {}
    msg = payload.get("message") or {}
    thread_ts = cont.get("thread_ts") or msg.get("thread_ts") or msg.get("ts")
    if not (channel and thread_ts):
        return {"skip": "no channel/thread on the interaction"}

    session = _find_session(sb, thread_ts)
    if not session:
        return {"skip": "no open session for this thread"}

    if action_id == "cx_send":
        out = reasoning.handle_agent_message(sb, session, "send it", handoff=True)
        if out.get("action") == "send":
            d = (deliver or _deliver)(sb, out["session"])
            post(channel, thread_ts,
                 ":white_check_mark: Sent to the customer." if d.get("sent")
                 else f":warning: couldn't send ({d.get('error') or d.get('via')}).")
            return {"action": "send", "delivery": d}
        post(channel, thread_ts, out.get("reply") or "…")
        return {"action": "send", "state": out["session"]["state"]}
    if action_id == "cx_edit":
        post(channel, thread_ts,
             ":pencil2: Reply in this thread with the edited reply text and I'll send *that*.")
        return {"action": "edit"}
    if action_id in ("cx_reassign", "cx_not_my_team"):
        post(channel, thread_ts,
             "Which team? Reply `route: <team>` — `support` / `tier2` / `csm` / "
             "`sales` / `offboarding` / `billing`.")
        return {"action": action_id}
    return {"skip": f"unknown action {action_id}"}


# ── transport ─────────────────────────────────────────────────────
def _connection_url(app_token: str) -> str:
    r = requests.post(_OPEN_URL, headers={"Authorization": f"Bearer {app_token}"}, timeout=15)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"apps.connections.open: {body.get('error')}")
    return body["url"]


def _make_poster(sb):
    def post(channel: str, thread_ts: str, text: str) -> None:
        try:
            slack._call("chat.postMessage", slack._bot_token(_TENANT, sb),
                        {"channel": channel, "thread_ts": thread_ts, "text": text})
        except Exception as e:  # noqa: BLE001
            log.warning("chat.postMessage failed: %s", e)
    return post


def _bot_user_id(sb) -> str | None:
    try:
        r = slack._call("auth.test", slack._bot_token(_TENANT, sb), {})
        return r.get("user_id")
    except Exception as e:  # noqa: BLE001
        log.warning("auth.test failed: %s", e)
        return None


async def _run_async() -> None:
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        log.warning("SLACK_APP_TOKEN (xapp-…) not set — slackbot idle. "
                    "See docs/SLACK_SETUP.md / .env.example.")
        while True:                       # idle, don't crash-loop the container
            await asyncio.sleep(3600)
    sb = get_supabase()
    post = _make_poster(sb)
    bot_uid = _bot_user_id(sb)
    log.info("slackbot starting (bot user %s)", bot_uid)

    async def _heartbeat() -> None:
        from interpreter.health import beat
        while True:
            try:
                beat("slackbot", {"status": "connected"}, sb=sb, force=True)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(60)

    while True:
        hb = None
        try:
            url = _connection_url(app_token)
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          open_timeout=20) as ws:
                log.info("socket connected")
                hb = asyncio.create_task(_heartbeat())
                _redrive_open_sessions(sb, post)   # Phase 27 — a dropped socket
                #   can strand a dialogue mid-turn; nudge the threads back to life.
                async for raw in ws:
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if msg.get("envelope_id"):
                        await ws.send(json.dumps({"envelope_id": msg["envelope_id"]}))
                    if kind == "hello":
                        continue
                    if kind == "disconnect":
                        log.info("server asked us to reconnect (%s)", msg.get("reason"))
                        break
                    if kind == "events_api":
                        ev = ((msg.get("payload") or {}).get("event")) or {}
                        try:
                            log.info("dispatch: %s", dispatch(sb, ev, post=post, bot_user_id=bot_uid))
                        except Exception as e:  # noqa: BLE001
                            log.exception("dispatch failed: %s", e)
                    if kind == "interactive":                 # Phase 27h — button clicks
                        try:
                            log.info("action: %s",
                                     dispatch_action(sb, msg.get("payload") or {}, post=post))
                        except Exception as e:  # noqa: BLE001
                            log.exception("dispatch_action failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("socket error (%s); reconnecting in 5s", e)
            await asyncio.sleep(5)
        finally:
            if hb is not None:
                hb.cancel()


def run() -> None:
    asyncio.run(_run_async())


if __name__ == "__main__":
    run()
