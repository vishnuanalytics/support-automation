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

OAuth (2026-09-05 addition) — a second, per-tenant auth mode alongside the
static `api_token` above, for a tenant whose only credential is a
"Developer Profile > Connectivity" client_id/client_secret (an account-
level OAuth client Freshworks issues directly — not a published Custom/
External App, no separate app-registration/publish step). Confirmed two
ways: Freshworks' own developer docs, and — because those docs turned out
inconsistent about the URL shape (one section omitted the `/org/` segment
a worked example included) — a real third-party integration (n8n's
Freshworks OAuth2 node, per a Freshworks Developer Community thread) using
the exact same Developer Profile credential type against a live account:
  * authorize: `GET https://{oauth_domain}/org/oauth/v2/authorize?
    response_type=code&client_id=...&redirect_uri=...&state=...&scope=...`
    — `/org/` is a **literal path segment**, not a placeholder for
    anything account-specific; the org identity is already carried by
    `oauth_domain` itself.
  * token exchange / refresh: `POST https://{oauth_domain}/org/oauth/v2/token`,
    `Authorization: Basic base64(client_id:client_secret)`, form body
    `grant_type=authorization_code&code=...&redirect_uri=...` (or
    `grant_type=refresh_token&refresh_token=...`).
  * access token lives 30 minutes; refresh token lives 365 days — so only
    the refresh_token is persisted (in Vault, alongside client_id/secret),
    and a fresh access token is minted on demand for every real API call
    rather than cached, same "don't persist what expires in minutes"
    choice `gdrive.py` makes for Google's access tokens.
  * `oauth_domain` is the Freshworks org host (e.g. a `.myfreshworks.com`
    account); defaults to `domain` when left blank. `scope` is still
    unconfirmed (no working example needed one) — left blank by default.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

KIND = "freshchat"
log = logging.getLogger("interpreter.freshchat")
_OAUTH_TOKEN_PATH = "/org/oauth/v2/token"
_OAUTH_AUTHORIZE_PATH = "/org/oauth/v2/authorize"
# Freshworks rejects an authorize request with no scope at all ("The
# requested scope is invalid or not applicable to your account") —
# confirmed live 2026-09-05. These three are Freshworks' own documented
# worked example (developers.freshworks.com, oauth-in-external-apps) —
# read-only ("view"). No confirmed scope exists yet for *sending* a
# message or listing agents (what test_connection/send_message need) —
# real gap, not silently assumed covered.
_DEFAULT_OAUTH_SCOPE = ("freshchat.conversation.view "
                       "freshchat.conversation.properties.view "
                       "freshchat.conversation.messages.view")


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
    # OAuth mode (alongside the static api_token above) — a tenant whose
    # account only exposes a Custom/External App uses this instead.
    oauth_domain: str = ""        # the Freshworks org host the app was registered under
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""       # 365-day; access tokens are minted on demand, never stored

    def __repr__(self) -> str:    # never leak secrets in a log/trace
        return (f"FreshchatConfig(tenant_id={self.tenant_id!r}, domain={self.domain!r}, "
                f"team={self.team!r}, status={self.status!r}, "
                f"configured={bool(self.api_token or self.refresh_token)}, "
                f"oauth={bool(self.refresh_token)})")

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
            oauth_domain=c.get("oauth_domain", ""),
            client_id=s.get("client_id", ""),
            client_secret=s.get("client_secret", ""),
            refresh_token=s.get("refresh_token", ""),
        )

    def to_config(self) -> dict:
        """The non-secret jsonb stored on the row."""
        return {"domain": self.domain, "team": self.team,
                "auto_send_enabled": self.auto_send_enabled,
                "oauth_domain": self.oauth_domain}

    def public_status(self) -> dict:
        """What the API returns to the browser — never the secret."""
        return {
            "configured": bool(self.api_token or self.refresh_token),
            "domain": self.domain, "team": self.team,
            "auto_send_enabled": self.auto_send_enabled, "status": self.status,
            "signature_verification": bool(self.webhook_public_key),
            "oauth": bool(self.refresh_token),
            "oauth_client_configured": bool(self.client_id and self.client_secret),
        }

    @property
    def base_url(self) -> str:
        d = self.domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        return f"https://{d}/v2" if d else ""

    @property
    def effective_oauth_domain(self) -> str:
        d = (self.oauth_domain or self.domain).strip()
        return d.removeprefix("https://").removeprefix("http://").rstrip("/")


def available(cfg: "FreshchatConfig | None") -> bool:
    if not (cfg and cfg.base_url):
        return False
    return bool(cfg.api_token or (cfg.refresh_token and cfg.client_id and cfg.client_secret))


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
                 webhook_public_key: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, refresh_token: str | None = None) -> None:
    """Persist `cfg`'s non-secret fields; each secret kwarg (only passed
    when the caller is actually changing it) gets merged into whatever's
    already in Vault, so re-saving team/auto_send_enabled alone doesn't
    require re-pasting every secret."""
    from . import vault_secrets

    changed = {"api_token": api_token, "webhook_public_key": webhook_public_key,
               "client_id": client_id, "client_secret": client_secret,
               "refresh_token": refresh_token}
    if any(v is not None for v in changed.values()):
        secret = vault_secrets.get(cfg.tenant_id, KIND, sb=sb)
        for k, v in changed.items():
            if v is not None:
                secret[k] = v
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


# ── OAuth (per-tenant Custom/External App — see module docstring) ──────────
def oauth_authorize_url(oauth_domain: str, client_id: str, redirect_uri: str,
                        state: str, scope: str = _DEFAULT_OAUTH_SCOPE) -> str:
    from urllib.parse import urlencode

    d = oauth_domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    params = {"response_type": "code", "client_id": client_id,
              "redirect_uri": redirect_uri, "state": state}
    if scope:
        params["scope"] = scope
    return f"https://{d}{_OAUTH_AUTHORIZE_PATH}?" + urlencode(params)


def _oauth_token_request(oauth_domain: str, client_id: str, client_secret: str,
                         **form: str) -> dict[str, Any]:
    import base64 as _b64

    import requests

    d = oauth_domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    basic = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(
        f"https://{d}{_OAUTH_TOKEN_PATH}",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=form, timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("access_token"):
        raise RuntimeError(f"Freshchat OAuth token request returned no access_token: {body}")
    return body


def oauth_exchange_code(oauth_domain: str, client_id: str, client_secret: str,
                        code: str, redirect_uri: str) -> dict[str, Any]:
    """Authorization code -> `{access_token, refresh_token, expires_in}`.
    Raises on failure — the caller (the OAuth callback handler) decides how
    to show that to the user."""
    return _oauth_token_request(
        oauth_domain, client_id, client_secret,
        grant_type="authorization_code", code=code, redirect_uri=redirect_uri,
    )


def oauth_refresh_access_token(oauth_domain: str, client_id: str, client_secret: str,
                               refresh_token: str) -> dict[str, Any]:
    """Refresh token -> a fresh `{access_token, refresh_token, expires_in}`.
    Freshworks may rotate the refresh_token on any call — the caller must
    persist the returned one if it differs from what it passed in."""
    return _oauth_token_request(
        oauth_domain, client_id, client_secret,
        grant_type="refresh_token", refresh_token=refresh_token,
    )


def _bearer_token(cfg: "FreshchatConfig", sb=None) -> str:
    """The token to send as `Authorization: Bearer <...>` for a real API
    call — the static `api_token` when the tenant is in that mode, or a
    freshly-minted access token (via the stored refresh_token) in OAuth
    mode. A rotated refresh_token is persisted back to Vault when `sb` is
    given; without `sb` the rotation is used for this call only and the
    next call re-derives from the (now stale) stored refresh_token, which
    still works unless Freshworks has already invalidated it — callers
    that can pass `sb` should."""
    if cfg.api_token:
        return cfg.api_token
    if not (cfg.refresh_token and cfg.client_id and cfg.client_secret):
        raise RuntimeError("freshchat: no api_token and no complete OAuth credentials")
    tok = oauth_refresh_access_token(
        cfg.effective_oauth_domain, cfg.client_id, cfg.client_secret, cfg.refresh_token)
    new_rt = tok.get("refresh_token")
    if sb is not None and new_rt and new_rt != cfg.refresh_token:
        save_channel(cfg, sb, refresh_token=new_rt)
        cfg.refresh_token = new_rt
    return tok["access_token"]


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
def send_message(cfg: "FreshchatConfig", conversation_id: str, text: str, sb=None) -> dict[str, Any]:
    """Reply into an existing conversation. Dry-run (no creds), never
    raises — matches emailer.send_reply / slack.post_message's convention.
    `sb` (optional) lets OAuth-mode token minting persist a rotated
    refresh_token; omit it and the call still works, just without that
    persistence."""
    if not available(cfg):
        return {"sent": False, "dry_run": True, "reason": "freshchat not connected"}
    import requests

    try:
        token = _bearer_token(cfg, sb)
        r = requests.post(
            f"{cfg.base_url}/conversations/{conversation_id}/messages",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"message_parts": [{"text": {"content": text}}], "actor_type": "agent"},
            timeout=15,
        )
        r.raise_for_status()
        return {"sent": True, "dry_run": False, "status": r.status_code}
    except Exception as e:  # noqa: BLE001
        log.warning("freshchat send_message(%s): %s", conversation_id, e)
        return {"sent": False, "dry_run": False, "error": str(e)[:300]}


def test_connection(cfg: "FreshchatConfig", sb=None) -> dict[str, Any]:
    """A lightweight authenticated read — proves the credentials + domain
    actually work, without sending anything or needing a webhook. Never
    raises. Uses `GET /v2/agents` (every account has at least one — the
    owner — so this doesn't depend on any conversation/contact existing
    yet); **not live-verified against a real account** (no credentials in
    this environment) — if this endpoint turns out wrong for a real
    account, the fix is isolated to this one function."""
    if not cfg.base_url:
        return {"ok": False, "error": "invalid or missing domain"}
    if not available(cfg):
        return {"ok": False, "error": "api_token, or a complete OAuth client + refresh_token, is required"}
    import requests

    try:
        token = _bearer_token(cfg, sb)
        r = requests.get(
            f"{cfg.base_url}/agents",
            headers={"Authorization": f"Bearer {token}"},
            params={"items_per_page": 1},
            timeout=15,
        )
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"authentication failed ({r.status_code})"}
        r.raise_for_status()
        return {"ok": True, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
