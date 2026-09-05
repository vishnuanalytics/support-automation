"""
Multi-provider connectors, step 3 — the Freshchat channel: the first
pluggable chat/call channel (FR-20 / FR-51). Prompted by the user's own
company (UrbanPiper), which uses Freshchat for chat support today.

Unlike Salesforce/Zendesk (case *systems*, selected per tenant via
`tenants.case_connector` — step 1), a chat channel like Freshchat is
customer-facing delivery, architecturally closer to the email channel
(`interpreter/mailbox.py`) than to a `connectors.CASE_ACTIONS`
implementation: a Freshchat-originated Case still goes through whichever
case connector the tenant has configured for CRM writeback. Freshchat
itself only owns (a) turning an inbound webhook into a case-shaped dict
and (b) delivering the customer-facing reply back into the right
conversation — this module is (a) plus the pure webhook-parsing/signature
pieces; the public webhook receiver and outbound delivery wiring are a
separate, later piece of this same step.

Credentials — a per-account API token (Bearer, "Admin API" scope) and the
webhook public key used to verify `X-Freshchat-Signature` — live in
Supabase Vault via `vault_secrets.py` (kind='freshchat'); non-secret
display fields (the account subdomain, team, `auto_send_enabled`) live in
`tenant_integrations.config`, matching `mailbox.py`/`slack.py`'s existing
shape. Freshchat isn't multi-org like Salesforce, so `org_label` always
stays the table's own default ('default') — no new axis needed.

Real Freshchat API shape (developers.freshchat.com, v2), confirmed via
the vendor's own docs, not guessed:
  * webhook `message_create` event: an `actor` (`actor_type`: "user" |
    "agent" | "system") and `data.message.message_parts[].text.content`
    plus a `conversation_id`. Exact nesting has minor variance across
    the vendor's own documented examples/API versions — `parse_webhook_
    message` checks a couple of known shapes defensively rather than
    assuming one.
  * signature: header `X-Freshchat-Signature` — an RSA/SHA256 signature
    of the raw request body, verified against the account's own webhook
    public key (a PEM string, pasted in when a tenant connects).
  * outbound reply: `POST {domain}/v2/conversations/{id}/messages`,
    `Authorization: Bearer <api_token>`.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

KIND = "freshchat"
log = logging.getLogger("interpreter.freshchat")


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


@dataclass
class FreshchatConfig:
    tenant_id: str
    domain: str = ""              # "yourcompany.freshchat.com" — the account's own API host
    team: str = "support"
    auto_send_enabled: bool = False
    status: str = "inactive"
    api_token: str = ""
    webhook_public_key: str = ""  # PEM

    def __repr__(self) -> str:    # never leak the token/key in a log/trace
        return (f"FreshchatConfig(tenant_id={self.tenant_id!r}, domain={self.domain!r}, "
                f"team={self.team!r}, status={self.status!r}, "
                f"configured={bool(self.api_token)})")

    @classmethod
    def from_row(cls, tenant_id: str, config: dict | None, status: str | None,
                secret: dict | None) -> "FreshchatConfig":
        c = dict(config or {})
        s = secret or {}
        return cls(
            tenant_id=str(tenant_id),
            domain=c.get("domain", ""),
            team=c.get("team", "support"),
            auto_send_enabled=bool(c.get("auto_send_enabled", False)),
            status=status or "inactive",
            api_token=s.get("api_token", ""),
            webhook_public_key=s.get("webhook_public_key", ""),
        )

    def to_config(self) -> dict:
        """The non-secret jsonb stored on the row."""
        return {"domain": self.domain, "team": self.team,
                "auto_send_enabled": self.auto_send_enabled}

    def public_status(self) -> dict:
        """What the API returns to the browser — never the secret."""
        return {
            "configured": bool(self.api_token), "domain": self.domain, "team": self.team,
            "auto_send_enabled": self.auto_send_enabled, "status": self.status,
            "signature_verification": bool(self.webhook_public_key),
        }

    @property
    def base_url(self) -> str:
        d = self.domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        return f"https://{d}/v2" if d else ""


def available(cfg: "FreshchatConfig | None") -> bool:
    return bool(cfg and cfg.api_token and cfg.base_url)


# ── storage (service-role Supabase client) ──────────────────────────────
def load_channel(tenant_id: str, sb) -> "FreshchatConfig | None":
    rows = (sb.table("tenant_integrations")
            .select("config,status").eq("tenant_id", tenant_id).eq("kind", KIND)
            .execute().data or [])
    if not rows:
        return None
    from . import vault_secrets
    secret = vault_secrets.get(tenant_id, KIND, sb=sb)
    return FreshchatConfig.from_row(tenant_id, rows[0]["config"], rows[0]["status"], secret)


def save_channel(cfg: "FreshchatConfig", sb, *, api_token: str | None = None,
                 webhook_public_key: str | None = None) -> None:
    """Persist `cfg`'s non-secret fields; `api_token`/`webhook_public_key`
    (only passed when the caller is actually changing one) get merged into
    whatever's already in Vault, so re-saving team/auto_send_enabled alone
    doesn't require re-pasting the token."""
    from . import vault_secrets

    if api_token is not None or webhook_public_key is not None:
        secret = vault_secrets.get(cfg.tenant_id, KIND, sb=sb)
        if api_token is not None:
            secret["api_token"] = api_token
        if webhook_public_key is not None:
            secret["webhook_public_key"] = webhook_public_key
        vault_secrets.put(cfg.tenant_id, KIND, secret, sb=sb)

    row = {
        "tenant_id": cfg.tenant_id, "kind": KIND, "org_label": "default", "secret": {},
        "config": cfg.to_config(), "status": cfg.status, "updated_at": "now()",
    }
    # ON CONFLICT must name the real constraint (tenant_id, kind, org_label) —
    # migration 082 widened tenant_integrations' primary key for multi-org
    # Salesforce; the same gotcha broke email-channel saves once already
    # (see mailbox.py's save_channel), matched here from the start.
    sb.table("tenant_integrations").upsert(row, on_conflict="tenant_id,kind,org_label").execute()


def delete_channel(tenant_id: str, sb) -> None:
    from . import vault_secrets
    vault_secrets.delete(tenant_id, KIND, sb=sb)
    sb.table("tenant_integrations").delete().eq("tenant_id", tenant_id).eq("kind", KIND).execute()


# ── pure webhook parsing (no network) ───────────────────────────────────
def verify_signature(public_key_pem: str, raw_body: bytes, signature_b64: str | None) -> bool:
    """RSA/SHA256 verification of the `X-Freshchat-Signature` header against
    the tenant's stored webhook public key. Pure — no network, no DB.
    Fails closed: a missing key/signature, a malformed PEM, or a bad
    signature all return False, never raise."""
    if not (public_key_pem and signature_b64):
        return False
    try:
        from cryptography.exceptions import InvalidSignature  # noqa: F401
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        key = serialization.load_pem_public_key(public_key_pem.encode())
        key.verify(base64.b64decode(signature_b64), raw_body, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception as e:  # noqa: BLE001 — any failure mode here means "reject"
        log.warning("freshchat verify_signature failed: %s", e)
        return False


def _first(*vals):
    for v in vals:
        if v:
            return v
    return None


def parse_webhook_message(body: dict[str, Any]) -> dict[str, Any] | None:
    """A `message_create` webhook event -> `{"conversation_id", "text",
    "actor_id"}`, or `None` when this isn't a fresh customer message worth
    starting a run for — an agent/bot/system echo (would otherwise loop:
    the bot's own reply re-arriving as a new "message"), a non-message
    event, or empty text. Pure — no network, no DB."""
    body = body or {}
    data = body.get("data") or {}
    msg = data.get("message") or data
    actor = body.get("actor") or msg.get("actor") or {}
    actor_type = str(actor.get("actor_type") or msg.get("actor_type") or "").strip().lower()
    if actor_type and actor_type != "user":
        return None
    parts = msg.get("message_parts") or []
    text = " ".join(
        p["text"]["content"].strip() for p in parts
        if isinstance(p, dict) and isinstance(p.get("text"), dict) and p["text"].get("content")
    ).strip()
    if not text:
        return None
    conversation_id = _first(msg.get("conversation_id"), data.get("conversation_id"),
                             body.get("conversation_id"))
    if not conversation_id:
        return None
    return {
        "conversation_id": str(conversation_id),
        "text": text,
        "actor_id": actor.get("actor_id") or msg.get("actor_id"),
        # Freshchat's Message resource has its own `id` in every documented
        # example; kept optional (not confirmed for every account/API
        # version) -- the webhook receiver falls back to a content hash for
        # idempotency when it's absent, so this is a nice-to-have, not load
        # bearing.
        "message_id": msg.get("id") or body.get("id"),
    }


# ── outbound (real HTTP) ────────────────────────────────────────────────
def send_message(cfg: "FreshchatConfig", conversation_id: str, text: str) -> dict[str, Any]:
    """Reply into an existing conversation. Dry-run (no creds), never
    raises — matches emailer.send_reply / slack.post_message's convention."""
    if not available(cfg):
        return {"sent": False, "dry_run": True, "reason": "freshchat not connected"}
    import requests

    try:
        r = requests.post(
            f"{cfg.base_url}/conversations/{conversation_id}/messages",
            headers={"Authorization": f"Bearer {cfg.api_token}",
                     "Content-Type": "application/json"},
            json={"message_parts": [{"text": {"content": text}}], "actor_type": "agent"},
            timeout=15,
        )
        r.raise_for_status()
        return {"sent": True, "dry_run": False, "status": r.status_code}
    except Exception as e:  # noqa: BLE001
        log.warning("freshchat send_message(%s): %s", conversation_id, e)
        return {"sent": False, "dry_run": False, "error": str(e)[:300]}
