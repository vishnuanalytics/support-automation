"""
Slack connector (Phase 16) — post an Approve/Reject message for a pending
internal action and verify the interaction callback.

One platform-level Slack app (`SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` /
`SLACK_SIGNING_SECRET`); each tenant installs it and its bot token lands in
`tenant_integrations (tenant_id, kind='slack')`. Everything degrades to a
clear error when the app isn't configured (mirrors gdrive.py / salesforce.py).
`verify_signature` is pure and unit-tested.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

log = logging.getLogger("interpreter.slack")
from typing import Any

SCOPES = "chat:write,chat:write.public,usergroups:read,channels:read,groups:read,users:read"
_AUTH = "https://slack.com/oauth/v2/authorize"
_TOKEN = "https://slack.com/api/oauth.v2.access"
_API = "https://slack.com/api"


def available() -> bool:
    return bool(os.environ.get("SLACK_CLIENT_ID")
               and os.environ.get("SLACK_CLIENT_SECRET")
               and os.environ.get("SLACK_SIGNING_SECRET"))


def _need() -> tuple[str, str]:
    cid, secret = os.environ.get("SLACK_CLIENT_ID"), os.environ.get("SLACK_CLIENT_SECRET")
    if not (cid and secret and os.environ.get("SLACK_SIGNING_SECRET")):
        raise RuntimeError("Slack is not configured — set SLACK_CLIENT_ID / "
                           "SLACK_CLIENT_SECRET / SLACK_SIGNING_SECRET (docs/SLACK_SETUP.md)")
    return cid, secret


# ── OAuth ────────────────────────────────────────────────────────────
def authorize_url(redirect_uri: str, state: str) -> str:
    cid, _ = _need()
    from urllib.parse import urlencode

    return f"{_AUTH}?" + urlencode({
        "client_id": cid, "scope": SCOPES, "redirect_uri": redirect_uri, "state": state,
    })


def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    cid, secret = _need()
    import requests

    r = requests.post(_TOKEN, data={
        "client_id": cid, "client_secret": secret, "code": code, "redirect_uri": redirect_uri,
    }, timeout=15)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Slack OAuth failed: {body.get('error')}")
    return {"bot_token": body["access_token"], "team": body.get("team", {}),
            "bot_user_id": body.get("bot_user_id")}


# ── signature check (pure) ──────────────────────────────────────────
def verify_signature(signing_secret: str, timestamp: str, raw_body: bytes,
                     signature: str, *, max_skew: int = 300) -> bool:
    try:
        if abs(time.time() - int(timestamp)) > max_skew:
            return False
    except (TypeError, ValueError):
        return False
    base = b"v0:" + timestamp.encode() + b":" + raw_body
    mine = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, signature or "")


# ── messaging ──────────────────────────────────────────────────────
def _bot_token(tenant_id: str, sb) -> str:
    rows = (sb.table("tenant_integrations").select("secret")
            .eq("tenant_id", tenant_id).eq("kind", "slack").execute().data or [])
    if not rows or not rows[0]["secret"].get("bot_token"):
        raise RuntimeError(f"tenant {tenant_id} has not connected Slack")
    return rows[0]["secret"]["bot_token"]


def connected(tenant_id: str, sb) -> bool:
    try:
        return bool(_bot_token(tenant_id, sb))
    except Exception:  # noqa: BLE001
        return False


def _call(method: str, token: str, payload: dict) -> dict:
    import requests

    r = requests.post(f"{_API}/{method}", json=payload,
                      headers={"Authorization": f"Bearer {token}"}, timeout=15)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"slack {method}: {body.get('error')}")
    return body


def approval_blocks(action_id: str, summary: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "actions", "block_id": f"ar:{action_id}", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Approve"},
             "value": action_id, "action_id": "approve"},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Reject"},
             "value": action_id, "action_id": "reject"},
        ]},
    ]


def post_approval(tenant_id: str, channel: str, *, summary: str, action_id: str, sb) -> dict:
    token = _bot_token(tenant_id, sb)
    return _call("chat.postMessage", token, {
        "channel": channel, "text": summary,
        "blocks": approval_blocks(action_id, summary),
    })


def update_message(tenant_id: str, channel: str, ts: str, text: str, sb) -> dict:
    token = _bot_token(tenant_id, sb)
    return _call("chat.update", token, {
        "channel": channel, "ts": ts, "text": text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    })


def post_message(text: str, *, tenant_id: str | None = None, channel: str | None = None,
                 sb=None, webhook: str | None = None, thread_ts: str | None = None,
                 blocks: "list | None" = None) -> dict:
    """Send a message to Slack (Phase 23d — the `notify_human` node).

    Prefers the tenant's bot token (`chat.postMessage` to `channel`, so it can
    thread / react later); falls back to an incoming webhook
    (`webhook` arg or `SLACK_ALERT_WEBHOOK`). `thread_ts` posts as a reply in
    that thread (bot path only). `blocks` (Phase 27h — Block Kit, e.g. the
    handoff card with buttons) overrides the default single-section render;
    `text` is still sent as the notification fallback. Returns {sent, via, …};
    never raises."""
    webhook = webhook or os.environ.get("SLACK_ALERT_WEBHOOK")
    # bot token path
    if tenant_id and channel:
        try:
            sb = sb or _sb()
            payload = {"channel": channel, "text": text,
                       "blocks": blocks or
                       [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}
            if thread_ts:
                payload["thread_ts"] = thread_ts
            r = _call("chat.postMessage", _bot_token(tenant_id, sb), payload)
            return {"sent": True, "via": "bot", "channel": channel, "ts": r.get("ts")}
        except Exception as e:  # noqa: BLE001
            last = str(e)
    else:
        last = "no tenant bot token / channel"
    # webhook fallback
    if webhook:
        try:
            import requests
            resp = requests.post(webhook, json={"text": text}, timeout=10)
            return {"sent": resp.status_code // 100 == 2, "via": "webhook",
                    "status": resp.status_code}
        except Exception as e:  # noqa: BLE001
            return {"sent": False, "via": "webhook", "error": str(e)}
    return {"sent": False, "via": None, "error": last}


def lookup_user_by_email(email: str, *, tenant_id: str | None = None, sb=None) -> str | None:
    """Slack user id for an email (`users.lookupByEmail`), or None. Never raises."""
    if not email:
        return None
    try:
        sb = sb or _sb()
        r = _call("users.lookupByEmail", _bot_token(tenant_id, sb), {"email": email})
        return (r.get("user") or {}).get("id")
    except Exception as e:  # noqa: BLE001
        log.debug("users.lookupByEmail(%s) failed: %s", email, e)
        return None


# ── usergroup (@on-call) mentions ──────────────────────────────────
# `usergroups.list` is per-workspace and rarely changes — cache it briefly so
# an escalation storm doesn't spend a Slack API call per Case.
_UG_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_UG_TTL = 600.0  # seconds


def _usergroup_index(tenant_id: str | None, sb) -> dict[str, str]:
    """{handle -> usergroup id} for the tenant's workspace. Cached; {} on failure."""
    key = tenant_id or "_"
    hit = _UG_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _UG_TTL:
        return hit[1]
    try:
        r = _call("usergroups.list", _bot_token(tenant_id, sb), {})
        idx = {g["handle"]: g["id"] for g in r.get("usergroups", []) if g.get("handle")}
    except Exception as e:  # noqa: BLE001
        log.debug("usergroups.list failed: %s", e)
        idx = {}
    _UG_CACHE[key] = (time.time(), idx)
    return idx


def usergroup_ref(handle: str | None, *, tenant_id: str | None = None, sb=None) -> str | None:
    """Render an on-call usergroup as a *real* Slack mention.

    Slack only notifies a usergroup when the message carries `<!subteam^ID>`; a
    bare `@handle` in text is inert. Resolves `handle` (with or without a
    leading `@`) to `<!subteam^ID>` via `usergroups.list`, falling back to the
    literal `@handle` when the group can't be resolved (no scope / unknown /
    Slack down). Returns None for a blank handle. Never raises."""
    if not handle:
        return None
    name = handle.lstrip("@").strip()
    if not name:
        return None
    try:
        sb = sb or _sb()
        gid = _usergroup_index(tenant_id, sb).get(name)
    except Exception:  # noqa: BLE001
        gid = None
    return f"<!subteam^{gid}>" if gid else f"@{name}"


def _sb():
    from ingestion.scraper import get_supabase

    return get_supabase()


# ── workspace introspection (flow editor pickers) ───────────────────
def list_channels(tenant_id: str | None, *, sb=None, limit: int = 200) -> list[dict[str, Any]]:
    """Public + private channels the bot can see (`conversations.list`), for
    the flow editor's channel picker (`notify_human.slack_channel`). []
    on any error (not connected / missing scope / Slack down) — never
    raises, same degrade-gracefully rule as `salesforce.list_queues`."""
    try:
        sb = sb or _sb()
        r = _call("conversations.list", _bot_token(tenant_id, sb),
                  {"types": "public_channel,private_channel", "limit": limit})
        return [{"id": c["id"], "name": c["name"], "is_member": bool(c.get("is_member"))}
                for c in r.get("channels", [])]
    except Exception as e:  # noqa: BLE001
        log.debug("list_channels failed: %s", e)
        return []


def list_users(tenant_id: str | None, *, sb=None, limit: int = 200) -> list[dict[str, Any]]:
    """Real human members of the workspace (bots, Slackbot and deleted users
    filtered out), for the flow editor's @mention picker. [] on any error."""
    try:
        sb = sb or _sb()
        r = _call("users.list", _bot_token(tenant_id, sb), {"limit": limit})
        return [
            {"id": u["id"], "name": u.get("real_name") or u.get("name") or u["id"],
             "email": (u.get("profile") or {}).get("email")}
            for u in r.get("members", [])
            if not u.get("is_bot") and not u.get("deleted") and u.get("id") != "USLACKBOT"
        ]
    except Exception as e:  # noqa: BLE001
        log.debug("list_users failed: %s", e)
        return []


def list_usergroups(tenant_id: str | None, *, sb=None) -> list[dict[str, Any]]:
    """Usergroups (@on-call handles) with display metadata — a public,
    richer sibling of `_usergroup_index`'s handle->id cache, for the flow
    editor's picker. [] on failure."""
    try:
        sb = sb or _sb()
        r = _call("usergroups.list", _bot_token(tenant_id, sb), {})
        return [{"id": g["id"], "handle": g.get("handle"), "name": g.get("name")}
                for g in r.get("usergroups", []) if g.get("handle")]
    except Exception as e:  # noqa: BLE001
        log.debug("list_usergroups failed: %s", e)
        return []


def workspace_meta(tenant_id: str | None, *, sb=None) -> dict[str, Any]:
    """Channels + users + usergroups for the flow editor's Slack pickers —
    degrades independently per section, same shape as
    `salesforce.introspect_org`. `available=False` when the tenant hasn't
    connected Slack at all."""
    sb = sb or _sb()
    if not connected(tenant_id, sb):
        return {"available": False, "channels": [], "users": [], "usergroups": [],
                "errors": ["Slack not connected for this tenant"]}
    return {
        "available": True,
        "channels": list_channels(tenant_id, sb=sb),
        "users": list_users(tenant_id, sb=sb),
        "usergroups": list_usergroups(tenant_id, sb=sb),
        "errors": [],
    }
