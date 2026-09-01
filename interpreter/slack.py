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
import os
import time
from typing import Any

SCOPES = "chat:write,chat:write.public"
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
                 sb=None, webhook: str | None = None) -> dict:
    """Send a plain message to Slack (Phase 23d — the `notify_human` node).

    Prefers the tenant's bot token (`chat.postMessage` to `channel`, so it can
    thread / react later); falls back to an incoming webhook
    (`webhook` arg or `SLACK_ALERT_WEBHOOK`). Returns {sent, via, ...}; never
    raises."""
    webhook = webhook or os.environ.get("SLACK_ALERT_WEBHOOK")
    # bot token path
    if tenant_id and channel:
        try:
            sb = sb or _sb()
            r = _call("chat.postMessage", _bot_token(tenant_id, sb),
                      {"channel": channel, "text": text,
                       "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]})
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


def _sb():
    from ingestion.scraper import get_supabase

    return get_supabase()
