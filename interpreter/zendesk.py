"""
Multi-provider connectors, step 2 — Zendesk as a second real "case system"
connector, proving `tenants.case_connector` (migration 084) actually
generalizes beyond Salesforce, not just a test-only dummy.

Implements the exact 8-action contract `connectors.CASE_ACTIONS` documents
(`update_fields`/`post_note`/`add_comment`/`assign_owner`/`ensure_case`/
`log_email_message`/`identify_sender`/`send_case_reply`), matching every
return-dict SHAPE `interpreter/salesforce.py`'s equivalents use exactly —
that shape is what `registry.py`'s node handlers actually read
(`result["dry_run"]`, `chatter.get("posted")`, `sender["match"]`, ...), so
matching it is what makes this connector a drop-in, not just "a Zendesk
API wrapper." Every function is dry-run / no-op with no creds and never
raises — the same best-effort convention every sibling module holds to.

Auth: HTTP Basic, `{email}/token` : `{api_token}` (per Zendesk's own
documented pattern). Base URL `https://{subdomain}.zendesk.com/api/v2`.
Credentials live in Supabase Vault via `vault_secrets.py` (kind='zendesk')
— not multi-org like Salesforce, so no `org_label` axis is needed; the
param is still accepted on every function (ignored) so the shared
`connectors.py` call sites don't need a connector-specific branch.

**Zendesk's data model doesn't map 1:1 onto Salesforce's, and this is
honest about where it doesn't, not pretending parity it hasn't verified:**
  * No Contact/Account/Lead split — Zendesk has Users + Organizations.
    `identify_sender`/`ensure_case` map Contact->User, Account->Organization,
    Lead->a newly-created User (there's no separate "lead" object).
  * `ensure_case`'s `reuse="thread"` can't match Salesforce's exact
    Message-ID threading (Zendesk has no such field on tickets created via
    the API, only via its own native email channel) — reuses the
    requester's most recent *open* ticket instead. Looser, not a bug:
    documented here so it isn't mistaken for the same guarantee.
  * `log_email_message` is a deliberate no-op — a Zendesk ticket's comment
    thread already **is** the email log; Salesforce needs a separate
    `EmailMessage` object, Zendesk doesn't.
  * `update_fields`/`append` only maps `Status` (translated to Zendesk's
    own status values) — every other Salesforce-shaped field name
    (`Routed_Team__c`, `AI_Confidence__c`, ...) is reported in `skipped`,
    not silently dropped. `append` text becomes a private ticket comment
    (the closest real Zendesk equivalent to "append a note to a field
    without the customer seeing it"), not a field append.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

KIND = "zendesk"
log = logging.getLogger("interpreter.zendesk")

_STATUS_TO_ZENDESK = {
    "new": "new", "triaged": "open", "in progress": "open",
    "waiting on customer": "pending", "escalated": "open",
    "resolved": "solved", "closed": "closed",
}


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def _creds(tenant_id: str | None, sb=None) -> dict[str, str] | None:
    """subdomain/email live in `tenant_integrations.config` (not secret);
    only `api_token` is Vault-backed — matches `mailbox.py`/`freshchat.py`'s
    "non-sensitive display fields in config, the real credential in Vault"
    split (this module's very first version kept all three in Vault, which
    was inconsistent with that convention — fixed before building the
    connect-account API that needs to read subdomain/email back out).
    Offline tests: no live Supabase read with no `sb` passed (matches
    `routing.py`'s `_fetch_rows` guard) — every existing test either sets
    `tenant_id=None` (dry-run path) or monkeypatches this function
    directly."""
    if not tenant_id:
        return None
    if sb is None and "PYTEST_CURRENT_TEST" in os.environ:
        return None
    try:
        rows = ((sb or _sb()).table("tenant_integrations").select("config")
                .eq("tenant_id", tenant_id).eq("kind", KIND).execute().data or [])
        if not rows:
            return None
        cfg = rows[0].get("config") or {}
        from . import vault_secrets
        secret = vault_secrets.get(tenant_id, KIND, sb=sb)
        subdomain, email, api_token = cfg.get("subdomain"), cfg.get("email"), secret.get("api_token")
        if not (subdomain and email and api_token):
            return None
        return {"subdomain": subdomain, "email": email, "api_token": api_token}
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk _creds(%s): %s", tenant_id, e)
        return None


def _client(tenant_id: str | None, sb=None) -> "_ZendeskClient | None":
    c = _creds(tenant_id, sb)
    return _ZendeskClient(c["subdomain"], c["email"], c["api_token"]) if c else None


class _ZendeskClient:
    """A thin real-HTTP wrapper — not a full SDK, this project doesn't need
    one. `request()` raises on a transport/HTTP error; every caller below
    catches it (matching `salesforce.py`'s "best-effort, never raise into
    the flow" convention)."""

    def __init__(self, subdomain: str, email: str, api_token: str):
        self.base = f"https://{subdomain}.zendesk.com/api/v2"
        self.auth = (f"{email}/token", api_token)

    def request(self, method: str, path: str, *, json: dict | None = None,
               params: dict | None = None) -> dict:
        import requests

        r = requests.request(method, f"{self.base}{path}", auth=self.auth,
                             json=json, params=params, timeout=15)
        r.raise_for_status()
        return r.json() if r.content else {}


def available(tenant_id: str | None, sb=None) -> bool:
    return _creds(tenant_id, sb) is not None


# --------------------------------------------------------------------------
# connect-account: config model + storage + a lightweight connection test —
# same shape as `interpreter/freshchat.py`'s (subdomain/email non-secret in
# `tenant_integrations.config`, `api_token` Vault-backed).
# --------------------------------------------------------------------------
@dataclass
class ZendeskConfig:
    tenant_id: str
    subdomain: str = ""
    email: str = ""
    status: str = "inactive"
    api_token: str = ""

    def __repr__(self) -> str:   # never leak the token in a log/trace
        return (f"ZendeskConfig(tenant_id={self.tenant_id!r}, subdomain={self.subdomain!r}, "
                f"email={self.email!r}, status={self.status!r}, configured={bool(self.api_token)})")

    @classmethod
    def from_row(cls, tenant_id: str, config: dict | None, status: str | None,
                secret: dict | None) -> "ZendeskConfig":
        c = dict(config or {})
        return cls(tenant_id=str(tenant_id), subdomain=c.get("subdomain", ""),
                  email=c.get("email", ""), status=status or "inactive",
                  api_token=(secret or {}).get("api_token", ""))

    def to_config(self) -> dict:
        return {"subdomain": self.subdomain, "email": self.email}

    def public_status(self) -> dict:
        return {"configured": bool(self.api_token), "subdomain": self.subdomain,
                "email": self.email, "status": self.status}


def load_channel(tenant_id: str, sb) -> "ZendeskConfig | None":
    rows = (sb.table("tenant_integrations")
            .select("config,status").eq("tenant_id", tenant_id).eq("kind", KIND)
            .execute().data or [])
    if not rows:
        return None
    from . import vault_secrets
    secret = vault_secrets.get(tenant_id, KIND, sb=sb)
    return ZendeskConfig.from_row(tenant_id, rows[0]["config"], rows[0]["status"], secret)


def save_channel(cfg: "ZendeskConfig", sb, *, api_token: str | None = None) -> None:
    if api_token is not None:
        from . import vault_secrets
        vault_secrets.put(cfg.tenant_id, KIND, {"api_token": api_token}, sb=sb)
    row = {"tenant_id": cfg.tenant_id, "kind": KIND, "org_label": "default", "secret": {},
          "config": cfg.to_config(), "status": cfg.status, "updated_at": "now()"}
    sb.table("tenant_integrations").upsert(row, on_conflict="tenant_id,kind,org_label").execute()


def delete_channel(tenant_id: str, sb) -> None:
    from . import vault_secrets
    vault_secrets.delete(tenant_id, KIND, sb=sb)
    sb.table("tenant_integrations").delete().eq("tenant_id", tenant_id).eq("kind", KIND).execute()


def test_connection(cfg: "ZendeskConfig") -> dict[str, Any]:
    """`GET /users/me.json` — a well-documented, always-available Zendesk
    endpoint; confirms the subdomain/email/token combination actually
    authenticates. Never raises."""
    if not (cfg.subdomain and cfg.email and cfg.api_token):
        return {"ok": False, "error": "subdomain, email and api_token are required"}
    import requests

    try:
        r = requests.get(
            f"https://{cfg.subdomain}.zendesk.com/api/v2/users/me.json",
            auth=(f"{cfg.email}/token", cfg.api_token), timeout=15,
        )
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"authentication failed ({r.status_code})"}
        r.raise_for_status()
        return {"ok": True, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}


# --------------------------------------------------------------------------
# update_fields
# --------------------------------------------------------------------------
def update_case_fields(case_id: str, fields: dict[str, Any], *, append: dict[str, str] | None = None,
                       tenant_id: str | None = None, org_label: str | None = None,
                       sb=None) -> dict[str, Any]:
    fields = {k: v for k, v in (fields or {}).items() if v not in (None, "")}
    append = {k: v for k, v in (append or {}).items() if v}
    zc = _client(tenant_id, sb)
    out: dict[str, Any] = {"written": {}, "skipped": {}, "planned": {}, "dry_run": zc is None}
    if zc is None:
        planned = {**fields, **{k: f"(append) {v}" for k, v in append.items()}}
        if planned:
            log.info("[zendesk dry-run] ticket %s <- %s", case_id, planned)
        out["planned"] = planned
        return out

    ticket_patch: dict[str, Any] = {}
    for k, v in fields.items():
        if k == "Status":
            zs = _STATUS_TO_ZENDESK.get(str(v).strip().lower())
            if zs:
                ticket_patch["status"] = zs
                out["written"][k] = v
                continue
        out["skipped"][k] = v   # no confirmed Zendesk mapping for this field yet

    try:
        if ticket_patch:
            zc.request("PUT", f"/tickets/{case_id}.json", json={"ticket": ticket_patch})
        for text in append.values():
            zc.request("PUT", f"/tickets/{case_id}.json",
                      json={"ticket": {"comment": {"body": text, "public": False}}})
        if append:
            out["written"]["_append_as_comment"] = list(append.values())
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk update_case_fields(%s): %s", case_id, e)
        out["error"] = str(e)
    return out


# --------------------------------------------------------------------------
# post_note / add_comment — both are Zendesk ticket comments, public vs not
# --------------------------------------------------------------------------
def post_note(case_id: str, body: str, *, mention_id: str | None = None,
              tenant_id: str | None = None, org_label: str | None = None, sb=None) -> dict[str, Any]:
    zc = _client(tenant_id, sb)
    if zc is None:
        log.info("[zendesk dry-run] private comment on ticket %s: mention=%s body=%r",
                 case_id, mention_id, body)
        return {"posted": False, "dry_run": True, "mention_id": mention_id}
    text = f"cc: {mention_id}\n\n{body}" if mention_id else body
    try:
        zc.request("PUT", f"/tickets/{case_id}.json",
                  json={"ticket": {"comment": {"body": text, "public": False}}})
        return {"posted": True, "dry_run": False, "mention_id": mention_id}
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk post_note(%s): %s", case_id, e)
        return {"posted": False, "dry_run": False, "mention_id": mention_id, "error": str(e)}


def add_case_comment(case_id: str, body: str, *, published: bool = False,
                     tenant_id: str | None = None, org_label: str | None = None,
                     sb=None) -> dict[str, Any]:
    zc = _client(tenant_id, sb)
    if zc is None:
        log.info("[zendesk dry-run] comment on ticket %s (public=%s): %r", case_id, published, body[:80])
        return {"created": False, "dry_run": True, "id": None}
    try:
        zc.request("PUT", f"/tickets/{case_id}.json",
                  json={"ticket": {"comment": {"body": body[:4000], "public": bool(published)}}})
        return {"created": True, "dry_run": False, "id": None}
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk add_case_comment(%s): %s", case_id, e)
        return {"created": False, "dry_run": False, "id": None, "error": str(e)}


# --------------------------------------------------------------------------
# assign_owner — a Zendesk "queue" is a Group; a specific person is an agent
# --------------------------------------------------------------------------
def assign_case(case_id: str, *, queue: str | None = None, user_id: str | None = None,
                tenant_id: str | None = None, org_label: str | None = None,
                sb=None) -> dict[str, Any]:
    if not (queue or user_id):
        return {"assigned": False, "reason": "no queue or user configured"}
    zc = _client(tenant_id, sb)
    if zc is None:
        log.info("[zendesk dry-run] assign ticket %s -> queue=%r user=%r", case_id, queue, user_id)
        return {"assigned": False, "dry_run": True, "queue": queue, "user_id": user_id}

    try:
        if user_id:
            zc.request("PUT", f"/tickets/{case_id}.json", json={"ticket": {"assignee_id": user_id}})
            return {"assigned": True, "dry_run": False, "owner_id": user_id, "owner_type": "user"}
        groups = zc.request("GET", "/groups.json").get("groups") or []
        match = next((g for g in groups if g.get("name") == queue), None)
        if not match:
            return {"assigned": False, "reason": f"queue {queue!r} not found"}
        zc.request("PUT", f"/tickets/{case_id}.json", json={"ticket": {"group_id": match["id"]}})
        return {"assigned": True, "dry_run": False, "owner_id": match["id"], "owner_type": "queue"}
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk assign_case(%s): %s", case_id, e)
        return {"assigned": False, "error": str(e)}


# --------------------------------------------------------------------------
# ensure_case — resolve/create a Zendesk User (Contact) + Organization
# (Account) + Ticket (Case)
# --------------------------------------------------------------------------
def _find_user_by_email(zc: "_ZendeskClient", email: str) -> dict | None:
    rows = zc.request("GET", "/users/search.json", params={"query": f"email:{email}"}).get("users") or []
    return rows[0] if rows else None


def _find_org_by_domain(zc: "_ZendeskClient", domain: str) -> dict | None:
    rows = zc.request("GET", "/organizations/search.json", params={"query": domain}).get("organizations") or []
    for o in rows:
        if domain in (o.get("domain_names") or []):
            return o
    return rows[0] if rows else None


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

    zc = _client(tenant_id, sb)
    out: dict[str, Any] = {
        "sf_id": case.get("sf_id"), "case_number": None,
        "contact_id": sender.get("contact_id"), "account_id": sender.get("account_id"),
        "account_name": sender.get("account_name"), "account": {},
        "created": False, "reused": False,
        "contact_created": False, "account_created": False,
        "dry_run": zc is None,
    }
    if zc is None:
        out["reason"] = "zendesk not configured"
        return out

    try:
        if case.get("sf_id"):
            return out

        uid, aid = sender.get("contact_id"), sender.get("account_id")
        if not uid and email:
            u = _find_user_by_email(zc, email)
            if u:
                uid = u["id"]
                aid = aid or u.get("organization_id")

        if not uid and create_contact and email:
            if not aid and create_account and domain:
                org = _find_org_by_domain(zc, domain)
                if org:
                    aid, out["account_name"] = org["id"], org.get("name")
                else:
                    label = domain.split(".")[0].title()
                    org = zc.request("POST", "/organizations.json",
                                     json={"organization": {"name": label, "domain_names": [domain]}}
                                    ).get("organization") or {}
                    aid, out["account_created"], out["account_name"] = org.get("id"), True, label
            payload: dict[str, Any] = {"email": email, "name": name or email.split("@", 1)[0]}
            if aid:
                payload["organization_id"] = aid
            u = zc.request("POST", "/users.json", json={"user": payload}).get("user") or {}
            uid, out["contact_created"] = u.get("id"), True

        out["contact_id"], out["account_id"] = uid, aid

        if reuse == "thread" and uid:
            # Zendesk tickets created via this API have no Message-ID
            # threading (that's a native-email-channel-only feature) --
            # the closest available equivalent is "this requester's most
            # recent still-open ticket." Looser than Salesforce's exact
            # match, documented in this module's docstring.
            existing = zc.request("GET", "/search.json",
                                  params={"query": f"type:ticket requester:{uid} status<solved"}
                                 ).get("results") or []
            if existing:
                t = existing[0]
                out["sf_id"], out["case_number"] = t["id"], str(t["id"])
                out["reused"], out["status"] = True, t.get("status")

        if not out["sf_id"]:
            payload = {
                "subject": (case.get("subject") or "(no subject)")[:255],
                "comment": {"body": case.get("body") or "", "public": True},
                "status": _STATUS_TO_ZENDESK.get(status.strip().lower(), "new"),
            }
            if uid:
                payload["requester_id"] = uid
            else:
                payload["requester"] = {"name": name or email or "unknown", "email": email}
            t = zc.request("POST", "/tickets.json", json={"ticket": payload}).get("ticket") or {}
            out["sf_id"], out["created"] = t.get("id"), True
            out["case_number"] = str(t.get("id"))
            out["status"] = t.get("status")

        if aid and not out["account"]:
            try:
                org = zc.request("GET", f"/organizations/{aid}.json").get("organization") or {}
                out["account"] = {"name": org.get("name"), "customer_type": None, "region": None}
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk ensure_case(%s): %s", email, e)
        out["reason"] = f"error: {e}"
    return out


# --------------------------------------------------------------------------
# log_email_message — deliberate no-op (see module docstring)
# --------------------------------------------------------------------------
def log_email_message(case_id: str, *, incoming: bool, from_addr: str = "", from_name: str = "",
                      to_addrs: "str | list[str]" = "", subject: str = "", body: str = "",
                      message_id: str = "", status: str | None = None,
                      tenant_id: str | None = None, org_label: str | None = None,
                      sb=None) -> dict[str, Any]:
    return {"created": False, "dry_run": False, "id": None,
           "reason": "zendesk's ticket comment thread already is the email log"}


# --------------------------------------------------------------------------
# identify_sender — Contact->User, Account->Organization, Lead->a new User
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
    zc = _client(tenant_id, sb)
    if zc is None:
        out["reason"] = "zendesk not configured"
        return out

    try:
        u = _find_user_by_email(zc, email)
        if u:
            out.update(known=True, match="contact", contact_id=u["id"], name=u.get("name"),
                      account_id=u.get("organization_id"),
                      account_matched=bool(u.get("organization_id")))
            if u.get("organization_id"):
                org = zc.request("GET", f"/organizations/{u['organization_id']}.json").get("organization") or {}
                out["account_name"] = org.get("name")
            return out

        if domain_match and domain and not is_free:
            org = _find_org_by_domain(zc, domain)
            if org:
                out.update(match="domain", account_matched=True,
                          account_id=org.get("id"), account_name=org.get("name"))

        if create_lead and out["match"] == "none":
            u = zc.request("POST", "/users.json",
                           json={"user": {"email": email, "name": email.split("@", 1)[0]}}
                          ).get("user") or {}
            out.update(match="lead_created", lead_id=u.get("id"))
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk identify_sender(%s): %s", email, e)
        out["reason"] = f"lookup error: {e}"
    return out


# --------------------------------------------------------------------------
# send_case_reply — a PUBLIC ticket comment (customer-visible)
# --------------------------------------------------------------------------
def send_case_reply(case_id: str, body: str, *, to_email: str | None = None,
                    subject: str | None = None, tenant_id: str | None = None,
                    org_label: str | None = None, sb=None) -> dict[str, Any]:
    zc = _client(tenant_id, sb)
    if zc is None:
        log.info("[zendesk dry-run] reply on ticket %s to %s: %r", case_id, to_email, body)
        return {"sent": False, "dry_run": True, "via": "dry_run", "to": to_email}
    try:
        zc.request("PUT", f"/tickets/{case_id}.json",
                  json={"ticket": {"comment": {"body": body[:4000], "public": True}}})
        return {"sent": True, "dry_run": False, "via": "ticket_comment", "to": to_email}
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk send_case_reply(%s): %s", case_id, e)
        return {"sent": False, "dry_run": False, "via": "error", "error": str(e)}
