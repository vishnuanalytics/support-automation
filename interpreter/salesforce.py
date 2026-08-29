"""
Thin Salesforce client for the `sf_writeback` node and the Chatter
"ask human" escalation.

Same pattern as `llm.py`: real calls when SF creds are in the env, a
**dry-run** (log the intended write, return a shaped result with
`dry_run=True`) when they're not — so the graph still runs in CI / eval /
demo with no org attached.

Env (.env). Three auth modes, tried in this order by which vars are set:

  A. JWT bearer flow (recommended -- headless, no password, survives MFA and
     the username-password flow being disabled). Needs a Connected App with
     an uploaded cert:
       SF_USERNAME, SF_CONSUMER_KEY, SF_PRIVATE_KEY_FILE  (or SF_PRIVATE_KEY)
  B. OAuth username-password flow. Needs a Connected App + the org's
     "Allow OAuth Username-Password Flows" toggle:
       SF_USERNAME, SF_PASSWORD, SF_CONSUMER_KEY, SF_CONSUMER_SECRET
  C. Legacy SOAP login (disabled by default on new Agentforce/trial orgs):
       SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN

  SF_DOMAIN  optional -- 'login' (default), 'test' for a sandbox, or a My
             Domain token like 'mycompany-dev-ed.develop.my'. A full URL is
             accepted too (scheme / .salesforce.com are stripped).

Field writes are **tolerant**: if the org doesn't have a custom field the
flow config references (e.g. `Module__c`), the API 400 is caught, that one
field is dropped, and the rest still write. See `SALESFORCE_SETUP.md` for
the two custom fields the reference flow expects.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger("interpreter.salesforce")

_client_obj = None
_tenant_clients: dict[str, Any] = {}   # tenant_id -> Salesforce, from tenant_integrations


def _normalize_domain(raw: str | None) -> str:
    """
    Accept whatever the user pasted and return the token simple-salesforce
    wants: 'login', 'test', or a My Domain like 'acme-dev-ed.develop.my'.
    Strips scheme, trailing slash, and a '.salesforce.com' /
    '.salesforce-setup.com' suffix.
    """
    d = (raw or "login").strip()
    d = re.sub(r"^https?://", "", d).strip("/")
    d = re.sub(r"\.salesforce(-setup)?\.com$", "", d)
    return d or "login"


def _jwt_key() -> str | None:
    inline = os.environ.get("SF_PRIVATE_KEY")
    if inline:
        return inline
    path = os.environ.get("SF_PRIVATE_KEY_FILE")
    return path or None


def available() -> bool:
    """True when real API calls will be made."""
    user = os.environ.get("SF_USERNAME")
    ck = os.environ.get("SF_CONSUMER_KEY")
    if user and ck and _jwt_key():                                   # A: JWT
        return True
    if user and os.environ.get("SF_PASSWORD") and ck \
            and os.environ.get("SF_CONSUMER_SECRET"):                # B: OAuth u/p
        return True
    if user and os.environ.get("SF_PASSWORD") \
            and os.environ.get("SF_SECURITY_TOKEN"):                 # C: SOAP
        return True
    return False


def _build_client(creds: dict[str, str]):
    """creds keys mirror the env var names (SF_USERNAME, SF_CONSUMER_KEY, ...)."""
    from simple_salesforce import Salesforce

    g = creds.get
    kw: dict[str, Any] = {
        "username": creds["SF_USERNAME"],
        "domain": _normalize_domain(g("SF_DOMAIN")),
    }
    ck, cs = g("SF_CONSUMER_KEY"), g("SF_CONSUMER_SECRET")
    token = g("SF_SECURITY_TOKEN", "") or ""
    jwt_key = g("SF_PRIVATE_KEY") or g("SF_PRIVATE_KEY_FILE")

    if ck and jwt_key:                                    # A: JWT bearer flow
        kw["consumer_key"] = ck
        if g("SF_PRIVATE_KEY"):
            kw["privatekey"] = g("SF_PRIVATE_KEY")
        else:
            kw["privatekey_file"] = g("SF_PRIVATE_KEY_FILE")
    elif ck and cs:                                       # B: OAuth username-password
        kw["password"] = creds["SF_PASSWORD"] + token
        kw["consumer_key"], kw["consumer_secret"] = ck, cs
    else:                                                 # C: legacy SOAP login
        kw["password"] = creds["SF_PASSWORD"]
        kw["security_token"] = token
    return Salesforce(**kw)


def _client():
    """The env-configured client (the default / single-tenant path)."""
    global _client_obj
    if _client_obj is None:
        _client_obj = _build_client({k: v for k, v in os.environ.items() if k.startswith("SF_")})
    return _client_obj


def client_for(tenant_id: str | None, sb=None):
    """Per-tenant client from `tenant_integrations` if a row exists, else the
    env client. (Phase 12 — real multi-tenancy.)"""
    if not tenant_id:
        return _client()
    if tenant_id not in _tenant_clients:
        try:
            from ingestion.scraper import get_supabase

            sb = sb or get_supabase()
            rows = (
                sb.table("tenant_integrations").select("secret")
                .eq("tenant_id", tenant_id).eq("kind", "salesforce").execute().data
            )
            _tenant_clients[tenant_id] = _build_client(rows[0]["secret"]) if rows else _client()
        except Exception as e:  # noqa: BLE001
            log.warning("tenant %s SF creds lookup failed (%s); using env client", tenant_id, e)
            _tenant_clients[tenant_id] = _client()
    return _tenant_clients[tenant_id]


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------
# Prefer a custom Account.Tier__c (basic|premium|enterprise) if the org has
# one; fall back to the standard restricted Account.Type picklist otherwise.
_CASE_SOQL_TIER = (
    "SELECT Id, CaseNumber, Subject, Description, Status, Priority, "
    "Account.Name, Account.Tier__c, Account.Type, Account.BillingCountry, "
    "Contact.Name, Contact.Email "
    "FROM Case WHERE Id = '{cid}'"
)
_CASE_SOQL_BASE = (
    "SELECT Id, CaseNumber, Subject, Description, Status, Priority, "
    "Account.Name, Account.Type, Account.BillingCountry, "
    "Contact.Name, Contact.Email "
    "FROM Case WHERE Id = '{cid}'"
)


def get_case(case_id: str) -> dict[str, Any]:
    """Fetch a Case (+ Account/Contact) and shape it as a flow `case` dict."""
    if not available():
        raise RuntimeError(
            "--sf-case needs SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN in .env"
        )
    rows: list[dict] = []
    for soql in (_CASE_SOQL_TIER, _CASE_SOQL_BASE):
        try:
            rows = _client().query(soql.format(cid=case_id)).get("records", [])
            break
        except Exception as e:  # noqa: BLE001  -- Tier__c absent -> try the base query
            if _bad_field(e):
                continue
            raise
    if not rows:
        raise LookupError(f"no Salesforce Case with Id {case_id!r}")
    r = rows[0]
    acct = r.get("Account") or {}
    con = r.get("Contact") or {}
    return {
        "sf_id": r["Id"],
        "case_id": r.get("CaseNumber") or r["Id"],
        "subject": r.get("Subject") or "",
        "body": r.get("Description") or "",
        "account": {
            "name": acct.get("Name"),
            "customer_type": acct.get("Tier__c") or acct.get("Type"),   # -> classify tier_field
            "region": acct.get("BillingCountry"),
        },
        "contact": {"name": con.get("Name"), "email": con.get("Email")},
    }


# --------------------------------------------------------------------------
# sender identification (Phase 17b)
# --------------------------------------------------------------------------
# Public / free-mail domains — a match on one of these tells us nothing about
# *which* customer the sender belongs to, so the domain -> Account step is
# skipped for them (otherwise every gmail user collapses onto one "account").
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "gmx.net", "mail.com", "yandex.com", "zoho.com", "pm.me",
}


def _soql_lit(s: str) -> str:
    """Escape a string for use inside a SOQL single-quoted literal."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def identify_sender(
    email: str,
    *,
    free_domains: "set[str] | list[str] | None" = None,
    domain_match: bool = True,
    create_lead: bool = False,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Resolve who an inbound sender is (Phase 17b).

    Order: exact Contact by email -> exact (unconverted) Lead -> the email
    **domain -> an Account** (a colleague of an existing customer; skipped
    for free-mail domains) -> unknown. `create_lead=True` opens a Lead when
    nothing matched. No SF creds -> `{"match": "none", ...}` (never raises).

    Returns: {email, domain, is_free_domain, known, account_matched, match
    ("contact"|"lead"|"domain"|"lead_created"|"none"), contact_id, lead_id,
    name, account_id, account_name, reason?}
    """
    email = (email or "").strip().lower()
    domain = email.split("@", 1)[1] if "@" in email else ""
    free = {d.lower() for d in (FREE_EMAIL_DOMAINS if free_domains is None else free_domains)}
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
    if not available():
        out["reason"] = "salesforce not configured"
        return out

    sf = client_for(tenant_id)
    lit = _soql_lit(email)
    try:
        rows = sf.query(
            f"SELECT Id, Name, AccountId, Account.Name FROM Contact "
            f"WHERE Email = '{lit}' LIMIT 1"
        ).get("records", [])
        if rows:
            c = rows[0]
            out.update(
                known=True, match="contact", contact_id=c["Id"], name=c.get("Name"),
                account_id=c.get("AccountId"),
                account_name=(c.get("Account") or {}).get("Name"),
                account_matched=bool(c.get("AccountId")),
            )
            return out

        rows = sf.query(
            f"SELECT Id, Name, Company FROM Lead "
            f"WHERE Email = '{lit}' AND IsConverted = false LIMIT 1"
        ).get("records", [])
        if rows:
            ld = rows[0]
            out.update(known=True, match="lead", lead_id=ld["Id"],
                       name=ld.get("Name"), account_name=ld.get("Company"))
            return out

        if domain_match and domain and not is_free:
            dlit = _soql_lit(domain)
            rows = sf.query(
                f"SELECT AccountId, Account.Name FROM Contact "
                f"WHERE Email LIKE '%@{dlit}' AND AccountId != null LIMIT 1"
            ).get("records", [])
            if rows and rows[0].get("AccountId"):
                out.update(match="domain", account_matched=True,
                           account_id=rows[0]["AccountId"],
                           account_name=(rows[0].get("Account") or {}).get("Name"))
            else:
                rows = sf.query(
                    f"SELECT Id, Name FROM Account WHERE Website LIKE '%{dlit}%' LIMIT 1"
                ).get("records", [])
                if rows:
                    out.update(match="domain", account_matched=True,
                               account_id=rows[0]["Id"], account_name=rows[0].get("Name"))

        if create_lead and out["match"] == "none":
            res = sf.Lead.create({
                "LastName": (email.split("@", 1)[0] or "Unknown")[:80],
                "Company": (domain or "Unknown")[:255],
                "Email": email,
            })
            out.update(match="lead_created", lead_id=res.get("id"))
    except Exception as e:  # noqa: BLE001 — identification is best-effort
        log.warning("identify_sender(%s): %s", email, e)
        out["reason"] = f"lookup error: {e}"
    return out


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------
_BAD_FIELD = re.compile(r"No such column '([^']+)'|INVALID_FIELD[^A-Za-z0-9_]+([A-Za-z0-9_]+)")


def _bad_field(exc: Exception) -> str | None:
    m = _BAD_FIELD.search(str(exc))
    if not m:
        return None
    return m.group(1) or m.group(2)


def update_case_fields(
    case_id: str,
    fields: dict[str, Any],
    *,
    append: dict[str, str] | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """
    Update `fields` on a Case. `append` maps field -> text to append to the
    field's current value (one extra read). Unknown fields are dropped and
    reported in `skipped`, not raised. Dry-run when no creds.
    """
    fields = {k: v for k, v in (fields or {}).items() if v not in (None, "")}
    append = {k: v for k, v in (append or {}).items() if v}
    out: dict[str, Any] = {"written": {}, "skipped": {}, "planned": {}, "dry_run": not available()}

    if not available():
        planned = {**fields, **{k: f"(append) {v}" for k, v in append.items()}}
        if planned:
            log.info("[sf dry-run] Case %s <- %s", case_id, planned)
        out["planned"] = planned
        return out

    sf = client_for(tenant_id)

    if append:
        current = sf.Case.get(case_id)
        for fld, text in append.items():
            base = (current.get(fld) or "").strip()
            fields[fld] = f"{base}\n\n{text}".strip() if base else text

    if not fields:
        return out

    remaining = dict(fields)
    for _ in range(len(fields) + 1):
        try:
            sf.Case.update(case_id, remaining)
            out["written"] = remaining
            return out
        except Exception as e:  # noqa: BLE001  -- SalesforceMalformedRequest et al.
            bad = _bad_field(e)
            if bad and bad in remaining:
                out["skipped"][bad] = remaining.pop(bad)
                log.warning("Case %s: field %r not writable, skipped (%s)", case_id, bad, e)
                if not remaining:
                    return out
                continue
            raise
    return out


# --------------------------------------------------------------------------
# Chatter
# --------------------------------------------------------------------------
def _current_user_id(sf) -> str | None:
    try:
        return sf.restful("chatter/users/me").get("id")
    except Exception:  # noqa: BLE001
        return None


def post_chatter(case_id: str, body: str, *, mention_id: str | None = None, tenant_id: str | None = None) -> dict[str, Any]:
    """
    Post a Chatter FeedItem on the Case, @mentioning `mention_id` (or the
    running user if None). Falls back to a plain FeedItem if the Connect API
    mention call fails. Dry-run when no creds.
    """
    if not available():
        log.info("[sf dry-run] Chatter on Case %s: mention=%s body=%r", case_id, mention_id, body)
        return {"posted": False, "dry_run": True, "mention_id": mention_id}

    sf = client_for(tenant_id)
    mention_id = mention_id or _current_user_id(sf)

    segments: list[dict[str, Any]] = []
    if mention_id:
        segments.append({"type": "Mention", "id": mention_id})
        segments.append({"type": "Text", "text": " "})
    segments.append({"type": "Text", "text": body})
    payload = {
        "feedElementType": "FeedItem",
        "subjectId": case_id,
        "body": {"messageSegments": segments},
    }
    try:
        res = sf.restful(
            "connect/records/feed-elements", method="POST", data=json.dumps(payload)
        )
        return {"posted": True, "dry_run": False, "mention_id": mention_id,
                "feed_element_id": (res or {}).get("id")}
    except Exception as e:  # noqa: BLE001
        log.warning("Connect API mention failed (%s); falling back to plain FeedItem", e)
        res = sf.FeedItem.create({"ParentId": case_id, "Body": body})
        return {"posted": True, "dry_run": False, "mention_id": None,
                "feed_element_id": res.get("id"), "mention_failed": True}


def send_case_reply(
    case_id: str,
    body: str,
    *,
    to_email: str | None = None,
    subject: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Send a customer-facing reply on a Case (Phase 17c — `clarify.auto_send`).

    Tries the `emailSimple` invocable action (actually sends) when a
    recipient address is known; otherwise (or on failure) records a public
    `CaseComment` so an agent / the customer portal still sees it. Dry-run
    when there are no creds — never raises.
    """
    subject = subject or "We need a bit more information"
    if not available():
        log.info("[sf dry-run] reply on Case %s to %s: %r", case_id, to_email, body)
        return {"sent": False, "dry_run": True, "via": "dry_run", "to": to_email}

    sf = client_for(tenant_id)
    if to_email:
        try:
            sf.restful(
                "actions/standard/emailSimple", method="POST",
                data=json.dumps({"inputs": [{
                    "emailAddresses": to_email,
                    "emailSubject": subject,
                    "emailBody": body,
                    "senderType": "CurrentUser",
                    "relatedRecordId": case_id,
                }]}),
            )
            return {"sent": True, "dry_run": False, "via": "email", "to": to_email}
        except Exception as e:  # noqa: BLE001
            log.warning("emailSimple failed (%s); falling back to CaseComment", e)

    try:
        res = sf.CaseComment.create(
            {"ParentId": case_id, "CommentBody": body[:4000], "IsPublished": True}
        )
        return {"sent": True, "dry_run": False, "via": "case_comment",
                "comment_id": res.get("id"), "to": to_email}
    except Exception as e:  # noqa: BLE001
        log.warning("send_case_reply: CaseComment failed: %s", e)
        return {"sent": False, "dry_run": False, "via": "error", "error": str(e)}
