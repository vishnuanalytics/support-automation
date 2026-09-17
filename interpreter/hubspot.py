"""
Multi-provider connectors, step 3 — HubSpot (Service Hub) as a third real
"case system" connector, alongside Salesforce and Zendesk.

Implements the exact 8-action contract `connectors.CASE_ACTIONS` documents
(`update_fields`/`post_note`/`add_comment`/`assign_owner`/`ensure_case`/
`log_email_message`/`identify_sender`/`send_case_reply`), matching every
return-dict SHAPE `interpreter/salesforce.py`'s equivalents use exactly —
that shape is what `registry.py`'s node handlers actually read
(`result["dry_run"]`, `chatter.get("posted")`, `sender["match"]`, ...), so
matching it is what makes this connector a drop-in, not just "a HubSpot API
wrapper." Every function is dry-run / no-op with no creds and never
raises — the same best-effort convention every sibling module holds to.

Auth: a HubSpot **Private App** access token (Bearer) — simpler than a full
OAuth2 app-install flow, and (like Zendesk) not something this module needs
to build since the token itself is the whole credential. Base URL is fixed:
`https://api.hubapi.com`. The token lives in Supabase Vault via
`vault_secrets.py` (kind='hubspot'); an optional `portal_id` is stored in
`tenant_integrations.config` (non-secret, filled in automatically the first
time `test_connection`/`save_channel` succeeds) purely for display.

**HubSpot's data model doesn't map 1:1 onto Salesforce's either, and this
is honest about where it doesn't, not pretending parity it hasn't
verified:**
  * No Contact/Account/Lead split as separate object *types* the way
    Salesforce has — HubSpot has Contacts and Companies; there's no
    separate Lead object, only a `lifecyclestage` property on a Contact.
    `identify_sender`/`ensure_case` map Contact->Contact, Account->Company,
    Lead->a Contact created with `lifecyclestage: "lead"`.
  * `ensure_case`'s `reuse="thread"` can't match Salesforce's exact
    Message-ID threading (a ticket created via this API has no such field)
    — reuses the contact's most recently modified still-open ticket
    instead, found via the v4 associations list. Looser, not a bug.
  * `update_fields`/Status: HubSpot tickets don't have a fixed status enum
    — they have a per-portal, per-pipeline `hs_pipeline_stage`, whose ids
    are opaque and tenant-specific. This resolves a friendly status word to
    a stage in the ticket pipeline **by label** (e.g. "New" / "Waiting on
    contact" / "Waiting on us" / "Closed" — HubSpot's own default pipeline
    labels), not a hardcoded stage id; no match -> reported in `skipped`,
    not silently dropped. Every other Salesforce-shaped field name also
    goes to `skipped`.
  * `post_note`/`add_comment`: HubSpot has no separate public/internal
    ticket-comment object the way Zendesk does — the closest primitive is a
    Note engagement, which is **always internal** (never customer-visible)
    regardless of the `published` flag. Both actions create the same kind
    of Note; `published` is accepted but has no real effect, documented
    here rather than silently ignored.
  * `assign_owner`: HubSpot has no queue/group object for tickets either —
    a `queue` is resolved to a single matching Owner by name/email
    substring (the closest real primitive), not a real group; `user_id`
    sets the ticket's owner directly.
  * `log_email_message` is a REAL feature here, not a no-op like Zendesk's:
    HubSpot has a first-class Email engagement object
    (`/crm/v3/objects/emails`) meant for exactly this — logging an inbound/
    outbound email onto a record's timeline — so this creates one and
    associates it with the ticket (and the contact, when known).
  * `send_case_reply`: HubSpot's Tickets API has **no native "send an email
    reply to the customer" endpoint** without a connected Conversations
    inbox (a separate product surface this project doesn't set up). If a
    `thread_id` param is given (a hook for a future Conversations-inbox
    integration — nothing calls it today), this tries the Conversations
    API; otherwise it logs the intended reply as an internal Note and
    honestly reports `sent: False` with the reason, rather than claiming a
    delivery that didn't happen.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

KIND = "hubspot"
log = logging.getLogger("interpreter.hubspot")

_BASE = "https://api.hubapi.com"

_STATUS_TO_STAGE_LABEL = {
    "new": "new",
    "triaged": "waiting on us",
    "in progress": "waiting on us",
    "waiting on customer": "waiting on contact",
    "escalated": "waiting on us",
    "resolved": "closed",
    "closed": "closed",
}


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def _creds(tenant_id: str | None, sb=None) -> dict[str, str] | None:
    """`portal_id` (display-only) lives in `tenant_integrations.config`;
    the real credential (`access_token`) is Vault-backed — matches
    `zendesk.py`/`freshchat.py`'s "non-sensitive display fields in config,
    the real credential in Vault" split. Offline tests: no live Supabase
    read with no `sb` passed (matches `routing.py`'s `_fetch_rows` guard)."""
    if not tenant_id:
        return None
    if sb is None and "PYTEST_CURRENT_TEST" in os.environ:
        return None
    try:
        rows = ((sb or _sb()).table("tenant_integrations").select("config")
                .eq("tenant_id", tenant_id).eq("kind", KIND).execute().data or [])
        if not rows:
            return None
        from . import vault_secrets
        secret = vault_secrets.get(tenant_id, KIND, sb=sb)
        access_token = secret.get("access_token")
        if not access_token:
            return None
        return {"access_token": access_token, "portal_id": (rows[0].get("config") or {}).get("portal_id")}
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot _creds(%s): %s", tenant_id, e)
        return None


def _client(tenant_id: str | None, sb=None) -> "_HubSpotClient | None":
    c = _creds(tenant_id, sb)
    return _HubSpotClient(c["access_token"]) if c else None


class _HubSpotClient:
    """A thin real-HTTP wrapper — not a full SDK, this project doesn't need
    one. `request()` raises on a transport/HTTP error; every caller below
    catches it (matching `salesforce.py`/`zendesk.py`'s "best-effort, never
    raise into the flow" convention)."""

    def __init__(self, access_token: str):
        self.headers = {"Authorization": f"Bearer {access_token}"}

    def request(self, method: str, path: str, *, json: dict | None = None,
               params: dict | None = None) -> dict:
        import requests

        r = requests.request(method, f"{_BASE}{path}", headers=self.headers,
                             json=json, params=params, timeout=15)
        r.raise_for_status()
        return r.json() if r.content else {}


def available(tenant_id: str | None, sb=None) -> bool:
    return _creds(tenant_id, sb) is not None


def resolve_entry_flow(tenant_id: str, sb) -> str | None:
    """The published flow this tenant's inbound tickets run through: the one
    marked as the case-system entry (`flows.sf_entry` — the column name
    predates multi-provider connectors but the flag itself is already
    connector-agnostic), else the tenant's sole published flow. Ambiguous ->
    None (skip + warn). Shared by `ingestion.hubspot_ticket_watch` (polling)
    and the `/webhooks/hubspot/{tenant_id}` receiver (push) so both triggers
    resolve the same flow the same way.

    A tenant runs ONE flow for every channel — a HubSpot case and a
    Salesforce case both trigger this same flow, which then routes each
    case-touching node to the right connector via `tenants.
    channel_connector_map` (migration 110), keyed by `case.channel`. An
    earlier same-day design routed HubSpot to a second, fully duplicated
    flow (`team="hubspot"`) instead — reverted: this project's case-
    touching nodes are scattered across a flow, not clustered, so
    duplicating the graph per channel meant re-duplicating almost all of
    it. See PROJECT_SCOPE.md for the full reasoning."""
    rows = (sb.table("flows").select("flow_id, sf_entry")
            .eq("tenant_id", tenant_id).eq("status", "published")
            .execute().data or [])
    if not rows:
        return None
    entry = [r for r in rows if r.get("sf_entry")]
    if len(entry) == 1:
        return entry[0]["flow_id"]
    if len(rows) == 1:
        return rows[0]["flow_id"]
    return None


def active_connector_tenants(sb, only: str | None = None) -> list[str]:
    """Tenant ids with `tenants.case_connector = 'hubspot'` AND an active
    `hubspot` integration. Best-effort: a read failure -> []."""
    try:
        trows = (sb.table("tenants").select("tenant_id")
                 .eq("case_connector", KIND).execute().data or [])
        irows = (sb.table("tenant_integrations").select("tenant_id")
                 .eq("kind", KIND).eq("status", "active").execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("active_connector_tenants: %s", e)
        return []
    active = ({r["tenant_id"] for r in trows if r.get("tenant_id")}
              & {r["tenant_id"] for r in irows if r.get("tenant_id")})
    return [t for t in active if (only is None or t == only)]


# --------------------------------------------------------------------------
# connect-account: config model + storage + a lightweight connection test —
# same shape as `interpreter/zendesk.py`'s (a non-secret display field in
# `tenant_integrations.config`, the credential Vault-backed).
# --------------------------------------------------------------------------
@dataclass
class HubSpotConfig:
    tenant_id: str
    portal_id: str = ""
    status: str = "inactive"
    access_token: str = ""
    # the webhook app's Client Secret (a SEPARATE HubSpot app from the
    # Private App above — the classic Private App UI has no Webhooks tab;
    # this is HMAC-verifying the push webhook, not used for any real API
    # call). Vault-backed like access_token, never logged.
    webhook_client_secret: str = ""
    # master switch for customer-facing auto-send on a `channel=hubspot` run
    # (mirrors email/Zendesk/Freshchat `auto_send_enabled`; `emailer.decide`
    # reads it). Off -> an `auto_reply` outcome is flagged for a human.
    auto_send_enabled: bool = False

    def __repr__(self) -> str:   # never leak secrets in a log/trace
        return (f"HubSpotConfig(tenant_id={self.tenant_id!r}, portal_id={self.portal_id!r}, "
                f"status={self.status!r}, auto_send={self.auto_send_enabled}, "
                f"configured={bool(self.access_token)}, "
                f"webhooks_configured={bool(self.webhook_client_secret)})")

    @classmethod
    def from_row(cls, tenant_id: str, config: dict | None, status: str | None,
                secret: dict | None) -> "HubSpotConfig":
        c, s = dict(config or {}), secret or {}
        return cls(tenant_id=str(tenant_id), portal_id=str(c.get("portal_id") or ""),
                  status=status or "inactive",
                  auto_send_enabled=bool(c.get("auto_send_enabled", False)),
                  access_token=s.get("access_token", ""),
                  webhook_client_secret=s.get("webhook_client_secret", ""))

    def to_config(self) -> dict:
        return {"portal_id": self.portal_id, "auto_send_enabled": self.auto_send_enabled}

    def public_status(self) -> dict:
        return {"configured": bool(self.access_token), "portal_id": self.portal_id,
                "status": self.status, "auto_send_enabled": self.auto_send_enabled,
                "webhooks_configured": bool(self.webhook_client_secret)}


def load_channel(tenant_id: str, sb) -> "HubSpotConfig | None":
    rows = (sb.table("tenant_integrations")
            .select("config,status").eq("tenant_id", tenant_id).eq("kind", KIND)
            .execute().data or [])
    if not rows:
        return None
    from . import vault_secrets
    secret = vault_secrets.get(tenant_id, KIND, sb=sb)
    return HubSpotConfig.from_row(tenant_id, rows[0]["config"], rows[0]["status"], secret)


def save_channel(cfg: "HubSpotConfig", sb, *, access_token: str | None = None,
                 webhook_client_secret: str | None = None) -> None:
    """Persist `cfg`'s non-secret fields; each secret kwarg (only passed
    when the caller is actually changing it) gets merged into whatever's
    already in Vault, so re-saving one doesn't require re-pasting the
    other (same pattern as freshchat.save_channel)."""
    changed = {"access_token": access_token, "webhook_client_secret": webhook_client_secret}
    if any(v is not None for v in changed.values()):
        from . import vault_secrets
        secret = vault_secrets.get(cfg.tenant_id, KIND, sb=sb)
        for k, v in changed.items():
            if v is not None:
                secret[k] = v
        vault_secrets.put(cfg.tenant_id, KIND, secret, sb=sb)
    row = {"tenant_id": cfg.tenant_id, "kind": KIND, "org_label": "default", "secret": {},
          "config": cfg.to_config(), "status": cfg.status, "updated_at": "now()"}
    sb.table("tenant_integrations").upsert(row, on_conflict="tenant_id,kind,org_label").execute()


def verify_webhook_signature(client_secret: str, *, method: str, url: str,
                             body: bytes, signature_b64: str | None,
                             timestamp_ms: str | None) -> bool:
    """HubSpot's v3 webhook signature (confirmed against the official
    `@hubspot/api-client` SDK's own `Signature.getSignature`/`isValid`, not
    guessed): `base64(HMAC-SHA256(clientSecret, method + url + body +
    timestamp))`, rejecting anything older than 5 minutes (replay window,
    same limit HubSpot's own SDK enforces) or lacking a signature/timestamp
    at all. `url` must be the exact URL HubSpot POSTed to (this app's
    configured `targetUrl`, including any per-tenant path)."""
    import hashlib
    import hmac as _hmac
    import base64
    import time

    if not (client_secret and signature_b64 and timestamp_ms):
        return False
    try:
        ts = int(timestamp_ms)
    except ValueError:
        return False
    if abs(time.time() * 1000 - ts) > 300_000:
        return False
    source = f"{method}{url}{body.decode('utf-8', errors='replace')}{ts}"
    mac = _hmac.new(client_secret.encode(), source.encode(), hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode()
    return _hmac.compare_digest(expected, signature_b64)


def delete_channel(tenant_id: str, sb) -> None:
    from . import vault_secrets
    vault_secrets.delete(tenant_id, KIND, sb=sb)
    sb.table("tenant_integrations").delete().eq("tenant_id", tenant_id).eq("kind", KIND).execute()


def test_connection(cfg: "HubSpotConfig") -> dict[str, Any]:
    """`GET /account-info/v3/details` — a well-documented, always-available
    HubSpot endpoint; confirms the access token actually authenticates and
    returns the portal id for display. Never raises."""
    if not cfg.access_token:
        return {"ok": False, "error": "access_token is required"}
    import requests

    try:
        r = requests.get(f"{_BASE}/account-info/v3/details",
                         headers={"Authorization": f"Bearer {cfg.access_token}"}, timeout=15)
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"authentication failed ({r.status_code})"}
        r.raise_for_status()
        portal_id = (r.json() or {}).get("portalId")
        return {"ok": True, "error": None, "portal_id": str(portal_id) if portal_id else None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}


# --------------------------------------------------------------------------
# association helper — the v4 "default association" endpoint sets the
# correct association type automatically, so this never has to hardcode
# HubSpot's opaque numeric association-type ids.
# --------------------------------------------------------------------------
def _associate(hc: "_HubSpotClient", from_type: str, from_id: str, to_type: str, to_id: str) -> None:
    hc.request("PUT", f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}")


def _stage_id_for_status(hc: "_HubSpotClient", status: str) -> str | None:
    label = _STATUS_TO_STAGE_LABEL.get(status.strip().lower())
    if not label:
        return None
    try:
        pipelines = hc.request("GET", "/crm/v3/pipelines/tickets").get("results") or []
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot pipelines lookup: %s", e)
        return None
    if not pipelines:
        return None
    for stage in pipelines[0].get("stages") or []:
        if (stage.get("label") or "").strip().lower() == label:
            return stage.get("id")
    return None


# --------------------------------------------------------------------------
# update_fields
# --------------------------------------------------------------------------
def update_case_fields(case_id: str, fields: dict[str, Any], *, append: dict[str, str] | None = None,
                       tenant_id: str | None = None, org_label: str | None = None,
                       sb=None) -> dict[str, Any]:
    fields = {k: v for k, v in (fields or {}).items() if v not in (None, "")}
    append = {k: v for k, v in (append or {}).items() if v}
    hc = _client(tenant_id, sb)
    out: dict[str, Any] = {"written": {}, "skipped": {}, "planned": {}, "dry_run": hc is None}
    if hc is None:
        planned = {**fields, **{k: f"(append) {v}" for k, v in append.items()}}
        if planned:
            log.info("[hubspot dry-run] ticket %s <- %s", case_id, planned)
        out["planned"] = planned
        return out

    props: dict[str, Any] = {}
    for k, v in fields.items():
        if k == "Status":
            stage_id = _stage_id_for_status(hc, str(v))
            if stage_id:
                props["hs_pipeline_stage"] = stage_id
                out["written"][k] = v
                continue
        out["skipped"][k] = v   # no confirmed HubSpot mapping for this field yet

    try:
        if props:
            hc.request("PATCH", f"/crm/v3/objects/tickets/{case_id}", json={"properties": props})
        for text in append.values():
            note = hc.request("POST", "/crm/v3/objects/notes",
                              json={"properties": {"hs_note_body": text,
                                                    "hs_timestamp": _now_ms()}}).get("id")
            if note:
                _associate(hc, "notes", note, "tickets", case_id)
        if append:
            out["written"]["_append_as_note"] = list(append.values())
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot update_case_fields(%s): %s", case_id, e)
        out["error"] = str(e)
    return out


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# post_note / add_comment — both create a HubSpot Note (always internal;
# HubSpot has no public/private ticket-comment distinction via the API)
# --------------------------------------------------------------------------
def _create_note(hc: "_HubSpotClient", case_id: str, body: str) -> str | None:
    note = hc.request("POST", "/crm/v3/objects/notes",
                      json={"properties": {"hs_note_body": body, "hs_timestamp": _now_ms()}}).get("id")
    if note:
        _associate(hc, "notes", note, "tickets", case_id)
    return note


def post_note(case_id: str, body: str, *, mention_id: str | None = None,
              tenant_id: str | None = None, org_label: str | None = None, sb=None) -> dict[str, Any]:
    hc = _client(tenant_id, sb)
    if hc is None:
        log.info("[hubspot dry-run] note on ticket %s: mention=%s body=%r", case_id, mention_id, body)
        return {"posted": False, "dry_run": True, "mention_id": mention_id}
    text = f"cc: {mention_id}\n\n{body}" if mention_id else body
    try:
        _create_note(hc, case_id, text)
        return {"posted": True, "dry_run": False, "mention_id": mention_id}
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot post_note(%s): %s", case_id, e)
        return {"posted": False, "dry_run": False, "mention_id": mention_id, "error": str(e)}


def add_case_comment(case_id: str, body: str, *, published: bool = False,
                     tenant_id: str | None = None, org_label: str | None = None,
                     sb=None) -> dict[str, Any]:
    hc = _client(tenant_id, sb)
    if hc is None:
        log.info("[hubspot dry-run] comment on ticket %s (published=%s, always internal): %r",
                 case_id, published, body[:80])
        return {"created": False, "dry_run": True, "id": None}
    try:
        note_id = _create_note(hc, case_id, body[:65000])
        return {"created": True, "dry_run": False, "id": note_id}
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot add_case_comment(%s): %s", case_id, e)
        return {"created": False, "dry_run": False, "id": None, "error": str(e)}


# --------------------------------------------------------------------------
# assign_owner — a HubSpot "queue" doesn't exist for tickets; `queue`
# resolves to a single matching Owner (by name/email substring)
# --------------------------------------------------------------------------
def assign_case(case_id: str, *, queue: str | None = None, user_id: str | None = None,
                tenant_id: str | None = None, org_label: str | None = None,
                sb=None) -> dict[str, Any]:
    if not (queue or user_id):
        return {"assigned": False, "reason": "no queue or user configured"}
    hc = _client(tenant_id, sb)
    if hc is None:
        log.info("[hubspot dry-run] assign ticket %s -> queue=%r user=%r", case_id, queue, user_id)
        return {"assigned": False, "dry_run": True, "queue": queue, "user_id": user_id}

    try:
        if user_id:
            hc.request("PATCH", f"/crm/v3/objects/tickets/{case_id}",
                      json={"properties": {"hubspot_owner_id": user_id}})
            return {"assigned": True, "dry_run": False, "owner_id": user_id, "owner_type": "user"}
        owners = hc.request("GET", "/crm/v3/owners").get("results") or []
        needle = queue.strip().lower()
        match = next((o for o in owners
                     if needle in (o.get("email") or "").lower()
                     or needle in f"{o.get('firstName', '')} {o.get('lastName', '')}".strip().lower()),
                    None)
        if not match:
            return {"assigned": False,
                    "reason": f"queue {queue!r} not found (HubSpot has no queue object for "
                              "tickets — resolved by owner name/email match)"}
        hc.request("PATCH", f"/crm/v3/objects/tickets/{case_id}",
                  json={"properties": {"hubspot_owner_id": match["id"]}})
        return {"assigned": True, "dry_run": False, "owner_id": match["id"], "owner_type": "queue"}
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot assign_case(%s): %s", case_id, e)
        return {"assigned": False, "error": str(e)}


# --------------------------------------------------------------------------
# ensure_case — resolve/create a HubSpot Contact + Company + Ticket
# --------------------------------------------------------------------------
def _find_contact_by_email(hc: "_HubSpotClient", email: str) -> dict | None:
    res = hc.request("POST", "/crm/v3/objects/contacts/search", json={
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname"], "limit": 1,
    }).get("results") or []
    return res[0] if res else None


def _find_company_by_domain(hc: "_HubSpotClient", domain: str) -> dict | None:
    res = hc.request("POST", "/crm/v3/objects/companies/search", json={
        "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}],
        "properties": ["name", "domain"], "limit": 1,
    }).get("results") or []
    return res[0] if res else None


def _open_ticket_for_contact(hc: "_HubSpotClient", contact_id: str) -> dict | None:
    """The contact's most recently modified ticket that isn't in the
    "closed"-labelled stage — the closest available equivalent to
    Salesforce's exact Message-ID thread match (documented gap)."""
    assoc = hc.request("GET", f"/crm/v4/objects/contacts/{contact_id}/associations/tickets").get("results") or []
    tickets = []
    for a in assoc:
        tid = a.get("toObjectId") or a.get("id")
        if not tid:
            continue
        try:
            t = hc.request("GET", f"/crm/v3/objects/tickets/{tid}",
                          params={"properties": "hs_pipeline_stage,hs_lastmodifieddate,subject,content"})
        except Exception:  # noqa: BLE001
            continue
        tickets.append(t)
    if not tickets:
        return None
    tickets.sort(key=lambda t: (t.get("properties") or {}).get("hs_lastmodifieddate") or "", reverse=True)
    return tickets[0]


def ensure_case(case: dict[str, Any], sender: dict[str, Any] | None = None, *,
               origin: str = "Email", status: str = "New",
               create_contact: bool = True, create_account: bool = True,
               reuse: str = "thread", tenant_id: str | None = None,
               org_label: str | None = None, sb=None) -> dict[str, Any]:
    sender = sender or {}
    email = ((case.get("from") or "") or (case.get("contact") or {}).get("email")
            or case.get("supplied_email") or sender.get("email") or "").strip().lower()
    name = case.get("from_name") or (case.get("contact") or {}).get("name") or ""
    domain = email.split("@", 1)[1] if "@" in email else ""

    hc = _client(tenant_id, sb)
    out: dict[str, Any] = {
        "sf_id": case.get("sf_id"), "case_number": None,
        "contact_id": sender.get("contact_id"), "account_id": sender.get("account_id"),
        "account_name": sender.get("account_name"), "account": {},
        "created": False, "reused": False,
        "contact_created": False, "account_created": False,
        "dry_run": hc is None,
    }
    if hc is None:
        out["reason"] = "hubspot not configured"
        return out

    try:
        if case.get("sf_id"):
            return out

        cid, aid = sender.get("contact_id"), sender.get("account_id")
        if not cid and email:
            c = _find_contact_by_email(hc, email)
            if c:
                cid = c["id"]

        if not cid and create_contact and email:
            if not aid and create_account and domain:
                comp = _find_company_by_domain(hc, domain)
                if comp:
                    aid, out["account_name"] = comp["id"], (comp.get("properties") or {}).get("name")
                else:
                    label = domain.split(".")[0].title()
                    comp = hc.request("POST", "/crm/v3/objects/companies",
                                      json={"properties": {"name": label, "domain": domain}})
                    aid, out["account_created"], out["account_name"] = comp.get("id"), True, label
            first, _, last = (name or email.split("@", 1)[0]).partition(" ")
            c = hc.request("POST", "/crm/v3/objects/contacts",
                          json={"properties": {"email": email, "firstname": first, "lastname": last}})
            cid, out["contact_created"] = c.get("id"), True
            if aid:
                _associate(hc, "contacts", cid, "companies", aid)

        out["contact_id"], out["account_id"] = cid, aid

        if reuse == "thread" and cid:
            t = _open_ticket_for_contact(hc, cid)
            if t and (t.get("properties") or {}).get("hs_pipeline_stage") != _closed_stage_id(hc):
                out["sf_id"], out["case_number"] = t["id"], t["id"]
                out["reused"] = True
                out["status"] = (t.get("properties") or {}).get("hs_pipeline_stage")

        if not out["sf_id"]:
            props: dict[str, Any] = {
                "subject": (case.get("subject") or "(no subject)")[:255],
                "content": case.get("body") or "",
            }
            stage_id = _stage_id_for_status(hc, status)
            if stage_id:
                props["hs_pipeline_stage"] = stage_id
            t = hc.request("POST", "/crm/v3/objects/tickets", json={"properties": props})
            out["sf_id"], out["created"] = t.get("id"), True
            out["case_number"] = t.get("id")
            out["status"] = (t.get("properties") or {}).get("hs_pipeline_stage")
            if cid:
                _associate(hc, "tickets", out["sf_id"], "contacts", cid)
            if aid:
                _associate(hc, "tickets", out["sf_id"], "companies", aid)

        if aid and not out["account"]:
            try:
                comp = hc.request("GET", f"/crm/v3/objects/companies/{aid}",
                                 params={"properties": "name"})
                out["account"] = {"name": (comp.get("properties") or {}).get("name"),
                                  "customer_type": None, "region": None}
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot ensure_case(%s): %s", email, e)
        out["reason"] = f"error: {e}"
    return out


def _closed_stage_id(hc: "_HubSpotClient") -> str | None:
    """Only used to decide whether a reused ticket counts as "open" within
    a single `ensure_case` call — a fresh lookup per call is fine (mirrors
    `_stage_id_for_status`'s own no-cache style)."""
    return _stage_id_for_status(hc, "closed")


# --------------------------------------------------------------------------
# log_email_message — a REAL feature: HubSpot's Email engagement object is
# built for exactly this (unlike Zendesk, which has no separate object).
# --------------------------------------------------------------------------
def log_email_message(
    case_id: str, *, incoming: bool, from_addr: str = "", from_name: str = "",
    to_addrs: "str | list[str]" = "", subject: str = "", body: str = "",
    message_id: str = "", status: str | None = None,
    tenant_id: str | None = None, org_label: str | None = None, sb=None,
) -> dict[str, Any]:
    to = ", ".join(to_addrs) if isinstance(to_addrs, list) else (to_addrs or "")
    hc = _client(tenant_id, sb)
    if hc is None:
        log.info("[hubspot dry-run] Email engagement on ticket %s (incoming=%s)", case_id, incoming)
        return {"created": False, "dry_run": True, "id": None}
    try:
        props = {
            "hs_timestamp": _now_ms(),
            "hs_email_direction": "INCOMING_EMAIL" if incoming else "EMAIL",
            "hs_email_subject": subject,
            "hs_email_text": body,
            "hs_email_from_email": from_addr,
            "hs_email_to_email": to,
        }
        eng = hc.request("POST", "/crm/v3/objects/emails", json={"properties": props})
        eid = eng.get("id")
        if eid:
            _associate(hc, "emails", eid, "tickets", case_id)
        return {"created": True, "dry_run": False, "id": eid}
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot log_email_message(%s): %s", case_id, e)
        return {"created": False, "dry_run": False, "id": None, "error": str(e)}


# --------------------------------------------------------------------------
# identify_sender — Contact->Contact, Account->Company, Lead->a new Contact
# --------------------------------------------------------------------------
def identify_sender(email: str, *, free_domains: "set[str] | list[str] | None" = None,
                    domain_match: bool = True, create_lead: bool = False,
                    tenant_id: str | None = None, org_label: str | None = None,
                    sb=None) -> dict[str, Any]:
    email = (email or "").strip().lower()
    domain = email.split("@", 1)[1] if "@" in email else ""
    free = {d.lower() for d in (free_domains or [])}
    is_free = domain in free
    out: dict[str, Any] = {
        "email": email, "domain": domain, "is_free_domain": is_free,
        "known": False, "account_matched": False, "match": "none",
        "contact_id": None, "lead_id": None, "name": None,
        "account_id": None, "account_name": None,
    }
    if not email or "@" not in email:
        out["reason"] = "no sender email"
        return out
    hc = _client(tenant_id, sb)
    if hc is None:
        out["reason"] = "hubspot not configured"
        return out

    try:
        c = _find_contact_by_email(hc, email)
        if c:
            props = c.get("properties") or {}
            name = " ".join(p for p in (props.get("firstname"), props.get("lastname")) if p) or None
            out.update(known=True, match="contact", contact_id=c["id"], name=name)
            assoc = hc.request("GET", f"/crm/v4/objects/contacts/{c['id']}/associations/companies").get("results") or []
            if assoc:
                comp_id = assoc[0].get("toObjectId") or assoc[0].get("id")
                comp = hc.request("GET", f"/crm/v3/objects/companies/{comp_id}", params={"properties": "name"})
                out.update(account_matched=True, account_id=comp_id,
                          account_name=(comp.get("properties") or {}).get("name"))
            return out

        if domain_match and domain and not is_free:
            comp = _find_company_by_domain(hc, domain)
            if comp:
                out.update(match="domain", account_matched=True, account_id=comp["id"],
                          account_name=(comp.get("properties") or {}).get("name"))

        if create_lead and out["match"] == "none":
            c = hc.request("POST", "/crm/v3/objects/contacts",
                           json={"properties": {"email": email,
                                                "firstname": email.split("@", 1)[0],
                                                "lifecyclestage": "lead"}})
            out.update(match="lead_created", lead_id=c.get("id"))
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot identify_sender(%s): %s", email, e)
        out["reason"] = f"lookup error: {e}"
    return out


# --------------------------------------------------------------------------
# send_case_reply — HubSpot's Tickets API has no native customer-email-send
# endpoint without a connected Conversations inbox (not set up by this
# project). Best-effort: use a `thread_id` param if a caller ever supplies
# one (forward-looking hook, unused today); otherwise log the reply as an
# internal Note and honestly report it wasn't delivered.
# --------------------------------------------------------------------------
def send_case_reply(case_id: str, body: str, *, to_email: str | None = None,
                    subject: str | None = None, thread_id: str | None = None,
                    tenant_id: str | None = None, org_label: str | None = None,
                    sb=None) -> dict[str, Any]:
    hc = _client(tenant_id, sb)
    if hc is None:
        log.info("[hubspot dry-run] reply on ticket %s to %s: %r", case_id, to_email, body)
        return {"sent": False, "dry_run": True, "via": "dry_run", "to": to_email}
    if thread_id:
        try:
            hc.request("POST", f"/conversations/v3/conversations/threads/{thread_id}/messages",
                      json={"type": "MESSAGE", "text": body, "richText": body})
            return {"sent": True, "dry_run": False, "via": "conversations_thread", "to": to_email}
        except Exception as e:  # noqa: BLE001
            log.warning("hubspot conversations reply (thread=%s) failed: %s; falling back to a note",
                       thread_id, e)
    try:
        _create_note(hc, case_id, body[:65000])
        return {"sent": False, "dry_run": False, "via": "note_fallback", "to": to_email,
                "reason": "HubSpot Tickets has no native send-email-to-customer endpoint without a "
                          "connected Conversations inbox; the reply was logged as an internal note "
                          "for a human to send"}
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot send_case_reply(%s): %s", case_id, e)
        return {"sent": False, "dry_run": False, "via": "error", "error": str(e)}


# --------------------------------------------------------------------------
# ingestion — poll new tickets so a `case_connector=hubspot` tenant's bot
# actually fires on inbound tickets (mirrors ingestion/zendesk_ticket_watch.py
# via zendesk.list_new_tickets/ticket_as_case).
# --------------------------------------------------------------------------
def list_new_tickets(tenant_id: str | None, sb=None, *, lookback_min: int = 120,
                     limit: int = 200) -> list[dict[str, Any]]:
    """Tickets in the pipeline's "New" stage, modified in the last
    `lookback_min`, newest first. Best-effort: no creds / an API error ->
    `[]` (never raises into a cron)."""
    hc = _client(tenant_id, sb)
    if hc is None:
        return []
    new_stage = _stage_id_for_status(hc, "new")
    if not new_stage:
        return []
    import datetime as _dt
    cutoff_ms = int((_dt.datetime.now(_dt.timezone.utc)
                     - _dt.timedelta(minutes=max(lookback_min, 1))).timestamp() * 1000)
    try:
        res = hc.request("POST", "/crm/v3/objects/tickets/search", json={
            "filterGroups": [{"filters": [
                {"propertyName": "hs_pipeline_stage", "operator": "EQ", "value": new_stage},
                {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": cutoff_ms},
            ]}],
            "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
            "properties": ["subject", "content", "hs_pipeline_stage", "hs_lastmodifieddate"],
            "limit": min(limit, 100),
        }).get("results") or []
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot list_new_tickets(%s): %s", tenant_id, e)
        return []
    return res[:limit]


def list_recent_tickets(tenant_id: str | None, sb=None, *, limit: int = 10) -> list[dict[str, Any]]:
    """The tenant's most recently modified tickets, ANY pipeline stage —
    unlike `list_new_tickets` (deliberately "New" stage only, for the
    trigger/watcher path), this powers the flow editor's "try a real recent
    case" test picker, where a closed or in-progress ticket is just as
    useful a test case as a fresh one. Best-effort: no creds / an API error
    -> `[]`."""
    hc = _client(tenant_id, sb)
    if hc is None:
        return []
    try:
        res = hc.request("POST", "/crm/v3/objects/tickets/search", json={
            "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
            "properties": ["subject", "content", "hs_pipeline_stage", "hs_lastmodifieddate"],
            "limit": min(limit, 100),
        }).get("results") or []
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot list_recent_tickets(%s): %s", tenant_id, e)
        return []
    return res[:limit]


def ticket_as_case(ticket: dict[str, Any], tenant_id: str | None = None,
                   sb=None) -> dict[str, Any]:
    """Normalise a HubSpot ticket into the interpreter's `case` shape (the
    same keys `initial_state` + the node handlers read). `sf_id` carries the
    ticket id — the seam's convention for "the case system's case id" (see
    ensure_case). One extra call to resolve the associated contact's email."""
    tid = ticket.get("id")
    props = ticket.get("properties") or {}
    case: dict[str, Any] = {
        "sf_id": str(tid) if tid is not None else None,
        "id": str(tid) if tid is not None else None,
        "case_number": str(tid) if tid is not None else None,
        "subject": props.get("subject") or "(no subject)",
        "body": props.get("content") or "",
        "channel": "hubspot",
        "status": props.get("hs_pipeline_stage"),
    }
    hc = _client(tenant_id, sb)
    if hc is not None and tid is not None:
        try:
            assoc = hc.request("GET", f"/crm/v4/objects/tickets/{tid}/associations/contacts").get("results") or []
            if assoc:
                cid = assoc[0].get("toObjectId") or assoc[0].get("id")
                c = hc.request("GET", f"/crm/v3/objects/contacts/{cid}",
                              params={"properties": "email,firstname,lastname"})
                cprops = c.get("properties") or {}
                if cprops.get("email"):
                    case["from"] = cprops["email"]
                name = " ".join(p for p in (cprops.get("firstname"), cprops.get("lastname")) if p)
                if name:
                    case["from_name"] = name
        except Exception as e:  # noqa: BLE001
            log.warning("hubspot ticket_as_case contact for ticket %s: %s", tid, e)
    return case


# --------------------------------------------------------------------------
# org_metadata — the flow editor's live-data pickers (queue/field/user),
# same return shape `salesforce.org_metadata`/`api.main.salesforce_meta`
# use so the editor's existing SfMeta-shaped components (QueuePicker,
# FieldMapEditor, UserOrQueuePicker) work unmodified against this connector
# too. HubSpot has neither a queue/group object nor a Case-Type/Module
# picklist for tickets (documented gaps in this module's own docstring), so
# `queues`/`users` are both resolved from the same Owners list (`queue`
# matches an owner by name/email substring — see `assign_case`) and
# `case_types`/`modules` are intentionally empty rather than faked.
# --------------------------------------------------------------------------
def org_metadata(tenant_id: str | None = None, sb=None) -> dict[str, Any]:
    """Owners and ticket properties are fetched independently (each in its
    own try/except) — a real live-testing finding: a Private App missing
    just the `crm.objects.owners.read` scope must not blank out the field-
    map picker too, which needs a completely different scope
    (`crm.schemas.tickets.read`) and works fine on its own. `available`
    reflects whether the connector is reachable at all (creds present),
    not whether every sub-call succeeded — a partial-scope token still
    degrades gracefully per-picker, not all-or-nothing."""
    hc = _client(tenant_id, sb)
    if hc is None:
        return {"available": False, "queues": [], "case_types": [], "modules": [],
                "case_fields": [], "users": []}

    errors: list[str] = []
    try:
        owners = hc.request("GET", "/crm/v3/owners").get("results") or []
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot org_metadata(%s) owners: %s", tenant_id, e)
        owners, err = [], str(e)[:200]
        errors.append(f"owners: {err}")
    try:
        props = hc.request("GET", "/crm/v3/properties/tickets").get("results") or []
    except Exception as e:  # noqa: BLE001
        log.warning("hubspot org_metadata(%s) properties: %s", tenant_id, e)
        props, err = [], str(e)[:200]
        errors.append(f"ticket properties: {err}")

    def _owner_name(o: dict) -> str:
        name = " ".join(p for p in (o.get("firstName"), o.get("lastName")) if p)
        return name or o.get("email") or str(o.get("id"))

    users = [{"id": o["id"], "name": _owner_name(o), "email": o.get("email")}
            for o in owners if o.get("id")]
    queues = [{"id": o.get("email") or str(o["id"]), "name": _owner_name(o),
              "developer_name": o.get("email")}
             for o in owners if o.get("id")]
    case_fields = [{
        "name": p.get("name"), "label": p.get("label") or p.get("name"),
        "type": p.get("type"), "custom": not bool(p.get("hubspotDefined")),
        "picklist_values": [{"value": opt.get("value"), "label": opt.get("label")}
                            for opt in (p.get("options") or [])],
    } for p in props if p.get("name")]
    return {"available": True, "queues": queues, "case_types": [], "modules": [],
           "case_fields": case_fields, "users": users,
           **({"error": "; ".join(errors)} if errors else {})}
