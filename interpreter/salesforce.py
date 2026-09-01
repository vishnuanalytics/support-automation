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
    sf = Salesforce(**kw)
    # simple_salesforce leaves requests with no timeout — one dropped
    # connection then hangs the caller forever. Force a per-request ceiling.
    import functools

    _to = float(os.environ.get("SF_HTTP_TIMEOUT", "30"))
    sf.session.request = functools.partial(sf.session.request, timeout=_to)
    return sf


def _client():
    """The env-configured client (the default / single-tenant path)."""
    global _client_obj
    if _client_obj is None:
        _client_obj = _build_client({k: v for k, v in os.environ.items() if k.startswith("SF_")})
    return _client_obj


def reset_client() -> None:
    """Drop the cached client so the next call mints a fresh session token.
    Used by the long-lived CDC subscriber when Salesforce returns
    UNAUTHENTICATED mid-stream (the JWT bearer token has expired)."""
    global _client_obj
    _client_obj = None
    _tenant_clients.clear()


_org_id: str | None = None


def pubsub_auth(*, refresh: bool = False) -> tuple[str, str, str]:
    """`(access_token, instance_url, org_id)` for the Salesforce Pub/Sub API
    gRPC metadata headers (`accesstoken` / `instanceurl` / `tenantid`).
    Reuses the env client's session; `refresh=True` forces a new token."""
    global _org_id
    if refresh:
        reset_client()
        _org_id = None
    if not available():
        raise RuntimeError("pubsub_auth needs Salesforce creds in the env (SF_USERNAME / SF_CONSUMER_KEY / …)")
    sf = _client()
    instance_url = f"https://{sf.sf_instance}".rstrip("/")
    if _org_id is None:
        _org_id = sf.query("SELECT Id FROM Organization LIMIT 1")["records"][0]["Id"]
    return sf.session_id, instance_url, _org_id


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


def latest_inbound_email(case_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
    """The newest *incoming* EmailMessage on a Case — what the customer last
    said. Used to re-run the flow on a reply instead of on the (stale) Case
    Description. None if no creds / no inbound message / query fails."""
    if not available():
        return None
    try:
        rows = client_for(tenant_id).query(
            "SELECT Id, Subject, TextBody, FromAddress, MessageIdentifier, MessageDate "
            f"FROM EmailMessage WHERE ParentId = '{_soql_lit(case_id)}' AND Incoming = true "
            "ORDER BY MessageDate DESC LIMIT 1"
        ).get("records", [])
    except Exception as e:  # noqa: BLE001
        log.warning("latest_inbound_email(%s): %s", case_id, e)
        return None
    if not rows:
        return None
    m = rows[0]
    return {
        "id": m.get("Id"),
        "text": m.get("TextBody") or "",
        "subject": m.get("Subject") or "",
        "from_addr": m.get("FromAddress") or "",
        "message_id": m.get("MessageIdentifier") or "",
        "at": m.get("MessageDate"),
    }


def agent_response_since(case_id: str, since_iso: str | None = None,
                         *, tenant_id: str | None = None) -> dict[str, Any]:
    """What a human has done on a Case since `since_iso` (the bot's run time):

      {"guidance": <newest CaseComment body> | None,
       "guidance_at": iso | None,
       "outbound_email": <newest agent reply-to-customer body> | None}

    A CaseComment is treated as *internal guidance the bot should turn into a
    customer reply*; an outbound EmailMessage means the agent already
    answered the customer directly. Never raises."""
    out: dict[str, Any] = {"guidance": None, "guidance_at": None, "outbound_email": None}
    if not available():
        return out
    sf = client_for(tenant_id)
    cid = _soql_lit(case_id)
    since = f" AND CreatedDate > {since_iso}" if since_iso else ""
    try:
        rows = sf.query(
            f"SELECT CommentBody, CreatedDate FROM CaseComment WHERE ParentId = '{cid}'{since} "
            "ORDER BY CreatedDate DESC LIMIT 1"
        ).get("records", [])
        if rows and rows[0].get("CommentBody"):
            out["guidance"] = rows[0]["CommentBody"]
            out["guidance_at"] = rows[0].get("CreatedDate")
    except Exception as e:  # noqa: BLE001
        log.warning("agent_response_since/comment(%s): %s", case_id, e)
    try:
        rows = sf.query(
            f"SELECT TextBody, CreatedDate FROM EmailMessage WHERE ParentId = '{cid}' "
            f"AND Incoming = false{since} ORDER BY CreatedDate DESC LIMIT 1"
        ).get("records", [])
        if rows and rows[0].get("TextBody"):
            out["outbound_email"] = rows[0]["TextBody"]
    except Exception as e:  # noqa: BLE001
        log.warning("agent_response_since/email(%s): %s", case_id, e)
    return out


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
            # idempotent: a re-run must not stack the same [triage] block again
            if text and text.strip() and text.strip() in base:
                continue
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
    if _recent_duplicate(sf, "FeedItem", case_id, "Body", body):
        return {"posted": False, "dry_run": False, "mention_id": mention_id, "deduped": True}
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
            "chatter/feed-elements", method="POST", data=json.dumps(payload)
        )
        return {"posted": True, "dry_run": False, "mention_id": mention_id,
                "feed_element_id": (res or {}).get("id")}
    except Exception as e:  # noqa: BLE001
        log.warning("Connect API feed-element post failed (%s); trying a plain FeedItem", e)
    try:
        res = sf.FeedItem.create({"ParentId": case_id, "Body": body})
        return {"posted": True, "dry_run": False, "mention_id": None,
                "feed_element_id": res.get("id"), "mention_failed": True}
    except Exception as e:  # noqa: BLE001 — best-effort, never raise into the flow
        log.warning("post_chatter(%s) failed: %s", case_id, e)
        return {"posted": False, "dry_run": False, "mention_id": None, "error": str(e)}


def _recent_duplicate(sf, sobject: str, case_id: str, body_field: str, body: str,
                      minutes: int = 180) -> bool:
    """True if an identical (same leading text) row already exists on the Case
    in the last `minutes` — so a re-run of the same flow doesn't stack a
    second identical Chatter note / draft CaseComment (Phase 23). Set
    SF_DEDUP_WRITES=0 to skip the check (saves one SOQL per escalation)."""
    if os.environ.get("SF_DEDUP_WRITES", "").strip() == "0":
        return False
    try:
        from datetime import datetime, timedelta, timezone

        head = _soql_lit((body or "")[:255])
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        rows = sf.query(
            f"SELECT Id FROM {sobject} WHERE ParentId = '{_soql_lit(case_id)}' "
            f"AND {body_field} LIKE '{head}%' AND CreatedDate >= {since} LIMIT 1"
        ).get("records", [])
        return bool(rows)
    except Exception:  # noqa: BLE001 — dedup is best-effort, never blocks the write
        return False


def add_case_comment(case_id: str, body: str, *, published: bool = False,
                     tenant_id: str | None = None) -> dict[str, Any]:
    """Add a `CaseComment` (internal by default). Used for the bot's
    suggested-reply draft — Salesforce won't take an API-created outbound
    draft `EmailMessage`. Skips an identical comment posted in the last 3h.
    Dry-run with no creds; never raises."""
    if not available():
        log.info("[sf dry-run] CaseComment on %s (published=%s): %r", case_id, published, body[:80])
        return {"created": False, "dry_run": True, "id": None}
    sf = client_for(tenant_id)
    if _recent_duplicate(sf, "CaseComment", case_id, "CommentBody", body):
        return {"created": False, "dry_run": False, "id": None, "deduped": True}
    try:
        res = sf.CaseComment.create(
            {"ParentId": case_id, "CommentBody": body[:4000], "IsPublished": bool(published)}
        )
        return {"created": True, "dry_run": False, "id": res.get("id")}
    except Exception as e:  # noqa: BLE001
        log.warning("add_case_comment(%s): %s", case_id, e)
        return {"created": False, "dry_run": False, "id": None, "error": str(e)}


# --------------------------------------------------------------------------
# case bootstrap from an inbound message (Phase 20e / 20f)
# --------------------------------------------------------------------------
def _thread_msg_ids(case: dict[str, Any]) -> list[str]:
    """RFC Message-IDs this inbound email is linked to — In-Reply-To,
    References, **and its own Message-ID**. De-duplicated, each with and
    without angle brackets (Salesforce stores `EmailMessage.MessageIdentifier`
    without them). Including the message's own id lets `find_case_by_thread`
    reuse a Case that Salesforce Email-to-Case already created for this exact
    mail — so the poller and E2C don't both open a Case."""
    raw: list[str] = []
    v = case.get("message_id")
    if isinstance(v, str) and v.strip():
        raw.append(v.strip())
    v = case.get("in_reply_to")
    if isinstance(v, str) and v.strip():
        raw.append(v.strip())
    for r in case.get("references") or []:
        if isinstance(r, str) and r.strip():
            raw.append(r.strip())
    out: list[str] = []
    seen: set[str] = set()
    for m in raw:
        for form in (m, m.strip("<>")):
            if form and form not in seen:
                seen.add(form)
                out.append(form)
    return out


def find_case_by_thread(message_ids: "list[str]", *, tenant_id: str | None = None) -> dict[str, Any]:
    """Given the Message-IDs an inbound email replies to, find the **open**
    Case those messages are already recorded on (via `EmailMessage`). Returns
    {sf_id, case_number} or {} — never raises."""
    if not message_ids or not available():
        return {}
    sf = client_for(tenant_id)
    lits = ", ".join(f"'{_soql_lit(m)}'" for m in message_ids[:50])
    try:
        rows = sf.query(
            f"SELECT ParentId, Parent.CaseNumber, Parent.IsClosed FROM EmailMessage "
            f"WHERE MessageIdentifier IN ({lits}) AND ParentId != null "
            f"ORDER BY CreatedDate DESC"
        ).get("records", [])
    except Exception as e:  # noqa: BLE001
        log.warning("find_case_by_thread: %s", e)
        return {}
    for r in rows:
        pid = r.get("ParentId") or ""
        parent = r.get("Parent") or {}
        if pid.startswith("500") and not parent.get("IsClosed"):
            return {"sf_id": pid, "case_number": parent.get("CaseNumber")}
    return {}


# EmailMessage.Status codes
_EM_NEW, _EM_SENT, _EM_DRAFT = "0", "3", "5"


def log_email_message(
    case_id: str,
    *,
    incoming: bool,
    from_addr: str = "",
    from_name: str = "",
    to_addrs: "str | list[str]" = "",
    subject: str = "",
    body: str = "",
    message_id: str = "",
    status: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Create an `EmailMessage` on the Case so the real email shows in
    Salesforce's Emails related list (not only the Description). `incoming`
    True = the customer's mail (Status New); False + `status=_EM_DRAFT` = a
    ready-to-send agent draft. Idempotent on `MessageIdentifier`. Dry-run
    with no creds — never raises."""
    to = ", ".join(to_addrs) if isinstance(to_addrs, list) else (to_addrs or "")
    mid = (message_id or "").strip().strip("<>")
    if not available():
        log.info("[sf dry-run] EmailMessage on Case %s (incoming=%s) mid=%s", case_id, incoming, mid)
        return {"created": False, "dry_run": True, "id": None}

    sf = client_for(tenant_id)
    try:
        if mid:
            # Salesforce Email-to-Case stores MessageIdentifier WITH angle
            # brackets; match both forms or `sf_case` records a duplicate.
            forms = ", ".join(f"'{_soql_lit(v)}'" for v in (mid, f"<{mid}>"))
            dup = sf.query(
                f"SELECT Id FROM EmailMessage WHERE ParentId = '{_soql_lit(case_id)}' "
                f"AND MessageIdentifier IN ({forms}) LIMIT 1"
            ).get("records", [])
            if dup:
                return {"created": False, "dry_run": False, "id": dup[0]["Id"], "idempotent": True}
        from datetime import datetime, timezone

        rec: dict[str, Any] = {
            "ParentId": case_id,
            "Incoming": bool(incoming),
            "Status": status or (_EM_NEW if incoming else _EM_DRAFT),
            "Subject": (subject or "")[:3000],
            "TextBody": body or "",
            "FromAddress": from_addr or "",
            "FromName": from_name or "",
            "ToAddress": to[:4000],
            "MessageDate": datetime.now(timezone.utc).isoformat(),
        }
        if mid:
            rec["MessageIdentifier"] = mid[:700]
        res = sf.EmailMessage.create({k: v for k, v in rec.items() if v not in (None, "")})
        return {"created": True, "dry_run": False, "id": res.get("id")}
    except Exception as e:  # noqa: BLE001
        log.warning("log_email_message(Case %s): %s", case_id, e)
        return {"created": False, "dry_run": False, "id": None, "error": str(e)}


# ── classifier slug / country -> the Case picklists (scripts/sf_support_setup.py) ──
_MODULE_RULES: "list[tuple[tuple[str, ...], str]]" = [
    (("billing", "refund", "charge", "invoice", "plan", "pricing", "payment",
      "subscription", "proration", "chargeback"), "Billing & Plans"),
    (("sso", "saml", "login", "password", "2fa", "mfa", "two-factor",
      "account", "member", "role", "seat", "signin", "sign-in"), "Account & Login"),
    (("webhook", "api", "rest", "rate-limit", "ratelimit", "endpoint", "token",
      "429"), "API & Webhooks"),
    (("export", "gdpr", "retention", "deletion", "dump"), "Data & Export"),
    (("zap", "trigger", "action", "filter", "path", "schedul"), "Zaps"),
    (("integration", "connector"), "Integrations & Apps"),
]
_SUBMODULE_RULES: "dict[str, list[tuple[tuple[str, ...], str]]]" = {
    "Billing & Plans": [(("refund", "chargeback"), "Refunds"),
                        (("invoice", "receipt"), "Invoices"),
                        (("plan", "upgrade", "downgrade", "proration"), "Plan Change"),
                        (("charge", "billed", "double", "duplicate", "payment"), "Charges")],
    "Account & Login": [(("sso", "saml", "okta"), "SSO"),
                        (("password", "reset"), "Password"),
                        (("2fa", "mfa", "two-factor"), "Two-Factor"),
                        (("member", "role", "seat", "invite"), "Members & Roles")],
    "API & Webhooks": [(("webhook",), "Webhooks"),
                       (("rate", "limit", "429"), "Rate Limits"),
                       (("api", "rest", "endpoint", "token"), "REST API")],
    "Data & Export": [(("gdpr", "deletion", "delete"), "Deletion / GDPR"),
                      (("retention",), "Retention"),
                      (("export", "dump"), "Export")],
    "Zaps": [(("trigger",), "Triggers"), (("action",), "Actions"),
             (("filter",), "Filters"), (("path",), "Paths"), (("schedul",), "Scheduling")],
    "Integrations & Apps": [(("auth",), "Authentication"), (("error",), "App Errors"),
                            (("new-app", "new app", "request"), "New App Request")],
}
_REGION_BY_COUNTRY = {c: r for r, cs in {
    "NA": ("united states", "usa", "us", "u.s.", "canada"),
    "EMEA": ("united kingdom", "uk", "gb", "ireland", "germany", "france", "spain",
             "italy", "netherlands", "sweden", "poland", "switzerland", "belgium",
             "austria", "norway", "denmark", "finland", "portugal", "greece",
             "czechia", "czech republic", "romania", "israel",
             "united arab emirates", "uae", "saudi arabia", "south africa",
             "nigeria", "kenya", "egypt", "turkey"),
    "APAC": ("india", "singapore", "australia", "japan", "china", "hong kong",
             "indonesia", "malaysia", "philippines", "thailand", "vietnam",
             "south korea", "korea", "new zealand", "taiwan", "bangladesh", "pakistan"),
    "LATAM": ("brazil", "mexico", "argentina", "chile", "colombia", "peru", "uruguay"),
}.items() for c in cs}


def map_case_fields(topic: str | None, country: str | None) -> dict[str, str]:
    """Classifier `topic` slug + Account country -> the restricted Case
    picklists (`Module__c` / `SubModule__c` / `Region__c`). `Topic__c` always
    gets the raw slug — the safety net; the rest are best-effort and omitted
    when nothing matches. Pure."""
    slug = (topic or "").strip().lower()
    out: dict[str, str] = {}
    if slug:
        out["Topic__c"] = str(topic)
    module = next((m for keys, m in _MODULE_RULES if any(k in slug for k in keys)),
                  "Other" if slug else "")
    if module:
        out["Module__c"] = module
        sub = next((s for keys, s in _SUBMODULE_RULES.get(module, [])
                    if any(k in slug for k in keys)), "")
        if sub:
            out["SubModule__c"] = sub
    region = _REGION_BY_COUNTRY.get((country or "").strip().lower())
    if region:
        out["Region__c"] = region
    return out


# ── classifier -> Case.Type (Phase 20n) ─────────────────────────────────────
# The standard `Case.Type` picklist (scripts/sf_support_setup.py CASE_TYPE_VALUES).
# It is the field queue owners scan a list view by, and it maps to a support
# *function* (billing / login / bug / product) — so it, not `Module__c`, is the
# key the `notify` node routes an internal ping on.
CASE_TYPE_VALUES = [
    "Question", "How-to", "Problem / Bug", "Billing",
    "Account / Login", "Feature Request", "Other",
]

# keyword -> Case.Type, first match wins. Deterministic fallback for when the
# classifier LLM is stubbed (quota) or returns something off-list.
_CASE_TYPE_RULES: "list[tuple[tuple[str, ...], str]]" = [
    (("refund", "chargeback", "charge", "billed", "invoice", "receipt", "billing",
      "payment", "pricing", "proration", "subscription", "coupon", "plan change"),
     "Billing"),
    (("sso", "saml", "okta", "login", "log in", "log-in", "signin", "sign in",
      "sign-in", "password", "2fa", "mfa", "two-factor", "locked out", "lockout",
      "can't access my account", "cannot access my account", "account access"),
     "Account / Login"),
    (("bug", "error", "broken", "not working", "isn't working", "stopped working",
      "fails", "failing", "failure", "exception", "500 error", "crash", "crashing",
      "regression", "unexpected"),
     "Problem / Bug"),
    (("feature request", "feature-request", "would be nice", "please add",
      "can you add", "roadmap", "suggestion", "enhancement", "wishlist"),
     "Feature Request"),
    (("how do i", "how do we", "how can i", "how to", "how-to", "step by step",
      "step-by-step", "walk me through", "tutorial", "is it possible to"),
     "How-to"),
]


def normalize_case_type(value: str | None) -> str:
    """Coerce a free-form / LLM `type` string to an exact `Case.Type` picklist
    value, or `""` if it doesn't map. Pure."""
    if not value:
        return ""
    s = str(value).strip().lower().replace("_", " ").replace("-", " ")
    s = " ".join(s.split())
    for canon in CASE_TYPE_VALUES:
        c = canon.lower().replace(" / ", " ").replace("-", " ")
        if s in (canon.lower(), c) or s.replace(" ", "") == c.replace(" ", ""):
            return canon
    if "bill" in s or "refund" in s or "invoice" in s:
        return "Billing"
    if "login" in s or "log in" in s or "auth" in s or ("account" in s and "access" in s):
        return "Account / Login"
    if "bug" in s or "problem" in s or "error" in s or "broken" in s:
        return "Problem / Bug"
    if "feature" in s:
        return "Feature Request"
    if s.startswith("how"):
        return "How-to"
    if "question" in s:
        return "Question"
    return ""


def map_case_type(topic: str | None, text: str | None = None) -> str:
    """Best-effort `Case.Type` from the classifier `topic` slug (+ optional raw
    case text). Returns `"Question"` for any non-empty input that matches no
    rule, `""` for empty. Pure."""
    hay = " ".join(x for x in ((topic or ""), (text or "")) if x).strip().lower()
    if not hay:
        return ""
    for keys, t in _CASE_TYPE_RULES:
        if any(k in hay for k in keys):
            return t
    return "Question"


def org_metadata(tenant_id: str | None = None) -> dict[str, Any]:
    """Routing queues + the `Case.Type` / `Case.Module__c` picklist values —
    for the flow editor's dropdowns (Phase 20o). `available=False` with empty
    lists when there are no Salesforce creds; never raises. The API layer
    caches this."""
    empty = {"available": False, "queues": [], "case_types": [], "modules": []}
    if not available():
        return empty
    try:
        sf = client_for(tenant_id)
        queues = [
            {"id": r["Id"], "name": r["Name"], "developer_name": r.get("DeveloperName")}
            for r in sf.query(
                "SELECT Id, Name, DeveloperName FROM Group WHERE Type = 'Queue' ORDER BY Name"
            ).get("records", [])
        ]
        fields = {f["name"]: f for f in sf.Case.describe()["fields"]}

        def _picklist(name: str) -> list[str]:
            return [v["value"] for v in fields.get(name, {}).get("picklistValues", [])
                    if v.get("active", True)]

        return {
            "available": True,
            "queues": queues,
            "case_types": _picklist("Type"),
            "modules": _picklist("Module__c"),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("org_metadata: %s", e)
        return {**empty, "error": str(e)}


def assign_case(
    case_id: str,
    *,
    queue: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Route a Case to a human — set `OwnerId` to a queue (resolved by
    DeveloperName or Name) or a user. No target / no creds -> a no-op with a
    reason. Never raises."""
    if not (queue or user_id):
        return {"assigned": False, "reason": "no queue or user configured"}
    if not available():
        log.info("[sf dry-run] assign Case %s -> queue=%r user=%r", case_id, queue, user_id)
        return {"assigned": False, "dry_run": True, "queue": queue, "user_id": user_id}

    sf = client_for(tenant_id)
    owner_id, owner_type = user_id, "user"
    try:
        if not owner_id and queue:
            q = _soql_lit(queue)
            rows = sf.query(
                f"SELECT Id FROM Group WHERE Type = 'Queue' AND "
                f"(DeveloperName = '{q}' OR Name = '{q}') LIMIT 1"
            ).get("records", [])
            if not rows:
                return {"assigned": False, "reason": f"queue {queue!r} not found"}
            owner_id, owner_type = rows[0]["Id"], "queue"
        sf.Case.update(case_id, {"OwnerId": owner_id})
        return {"assigned": True, "dry_run": False, "owner_id": owner_id, "owner_type": owner_type}
    except Exception as e:  # noqa: BLE001
        log.warning("assign_case(%s): %s", case_id, e)
        return {"assigned": False, "error": str(e)}


def _account_snapshot(sf, account_id: str) -> dict[str, Any]:
    """{name, customer_type, region} for the classify node — tolerates an org
    with no custom `Account.Tier__c` (falls back to the standard Type)."""
    for soql in (
        f"SELECT Name, Tier__c, Type, BillingCountry FROM Account WHERE Id = '{account_id}'",
        f"SELECT Name, Type, BillingCountry FROM Account WHERE Id = '{account_id}'",
    ):
        try:
            rows = sf.query(soql).get("records", [])
            if not rows:
                return {}
            a = rows[0]
            return {
                "name": a.get("Name"),
                "customer_type": a.get("Tier__c") or a.get("Type"),
                "region": a.get("BillingCountry"),
            }
        except Exception as e:  # noqa: BLE001 — Tier__c absent -> try the base query
            if _bad_field(e):
                continue
            raise
    return {}


def ensure_case(
    case: dict[str, Any],
    sender: dict[str, Any] | None = None,
    *,
    origin: str = "Email",
    status: str = "New",
    create_contact: bool = True,
    create_account: bool = True,
    reuse: str = "thread",
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Resolve an inbound `case` dict to a real Salesforce Case (Phase 20e/f).

    * `case['sf_id']` already set -> just refresh the Account snapshot.
    * resolve the Contact: `sender['contact_id']`, else an exact Contact by
      the sender's email, else (when `create_contact`) create one. When
      `create_account` and the sender has a *business* email domain with no
      Account, create an Account from the domain and link the Contact to it
      (free-mail domains get a Contact with no Account).
    * `reuse="thread"` (default): attach to an existing **open** Case only
      when the email is a genuine reply — its `In-Reply-To` / `References`
      match an `EmailMessage` already on that Case (Phase 20f / FR-6).
      `reuse="never"`: always a new Case. Otherwise create a new Case
      (Subject / Description / ContactId / AccountId / SuppliedEmail /
      Origin / Status).

    Returns {sf_id, case_number, contact_id, account_id, account_name,
    account: {name, customer_type, region}, created, reused, contact_created,
    account_created, dry_run, reason?}. No SF creds -> a dry-run result;
    never raises.
    """
    sender = sender or {}
    email = (
        (case.get("from") or "")
        or (case.get("contact") or {}).get("email")
        or case.get("supplied_email")
        or sender.get("email")
        or ""
    ).strip().lower()
    name = case.get("from_name") or (case.get("contact") or {}).get("name") or ""
    domain = email.split("@", 1)[1] if "@" in email else ""
    is_free = domain in FREE_EMAIL_DOMAINS

    out: dict[str, Any] = {
        "sf_id": case.get("sf_id"), "case_number": None,
        "contact_id": sender.get("contact_id"), "account_id": sender.get("account_id"),
        "account_name": sender.get("account_name"), "account": {},
        "created": False, "reused": False,
        "contact_created": False, "account_created": False,
        "dry_run": not available(),
    }
    if not available():
        out["reason"] = "salesforce not configured"
        return out

    sf = client_for(tenant_id)
    try:
        if case.get("sf_id"):
            if out["account_id"]:
                out["account"] = _account_snapshot(sf, out["account_id"])
            return out

        cid = sender.get("contact_id")
        aid = sender.get("account_id")

        if not cid and email:
            rows = sf.query(
                f"SELECT Id, AccountId, Account.Name FROM Contact "
                f"WHERE Email = '{_soql_lit(email)}' LIMIT 1"
            ).get("records", [])
            if rows:
                cid = rows[0]["Id"]
                aid = aid or rows[0].get("AccountId")
                out["account_name"] = out["account_name"] or (rows[0].get("Account") or {}).get("Name")

        if not cid and create_contact and email:
            if not aid and create_account and domain and not is_free:
                arows = sf.query(
                    f"SELECT Id, Name FROM Account WHERE Website LIKE '%{_soql_lit(domain)}%' LIMIT 1"
                ).get("records", [])
                if arows:
                    aid, out["account_name"] = arows[0]["Id"], arows[0].get("Name")
                else:
                    label = domain.split(".")[0].title()
                    acc = sf.Account.create({"Name": label, "Website": domain})
                    aid, out["account_created"], out["account_name"] = acc.get("id"), True, label
            local = email.split("@", 1)[0]
            payload: dict[str, Any] = {"Email": email, "LastName": (name or local)[:80]}
            if name and " " in name:
                first, _, last = name.partition(" ")
                payload["FirstName"] = first[:40]
                payload["LastName"] = (last or name)[:80]
            if aid:
                payload["AccountId"] = aid
            con = sf.Contact.create(payload)
            cid, out["contact_created"] = con.get("id"), True

        out["contact_id"], out["account_id"] = cid, aid

        if reuse == "thread":
            match = find_case_by_thread(_thread_msg_ids(case), tenant_id=tenant_id)
            if match:
                out["sf_id"] = match["sf_id"]
                out["case_number"] = match.get("case_number")
                out["reused"] = True

        if not out["sf_id"]:
            payload = {
                "Subject": (case.get("subject") or "(no subject)")[:255],
                "Description": case.get("body") or "",
                "Origin": origin,
                "Status": status,
            }
            if cid:
                payload["ContactId"] = cid
            if aid:
                payload["AccountId"] = aid
            if email:
                payload["SuppliedEmail"] = email
            cres = sf.Case.create(payload)
            out["sf_id"], out["created"] = cres.get("id"), True

        if aid:
            out["account"] = _account_snapshot(sf, aid)
    except Exception as e:  # noqa: BLE001 — case bootstrap is best-effort
        log.warning("ensure_case(%s): %s", email, e)
        out["reason"] = f"error: {e}"
    return out


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
