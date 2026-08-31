"""
Phase 20 -- the email channel: a per-tenant mailbox the platform polls for
inbound support mail and replies from.

Non-secret settings live in `tenant_integrations (kind='email')` (`config`
jsonb); the mailbox app-password / Gmail OAuth refresh token live in
**Supabase Vault**, reached through the `integration_secret_{put,get,delete}`
SECURITY DEFINER RPCs (migration 035). Only the service-role Supabase
client touches any of it.

This module (Phase 20a):
  * `MailboxConfig`      -- the resolved channel config (secret kept out of `repr`)
  * `load_channel` / `save_channel` / `delete_channel`
  * `test_connection`   -- an IMAP/SMTP or Gmail login check for the UI's
                           "Test connection" button; opens nothing persistent
  * `parse_message`, `is_autoreply`, `looks_like_bot_address` -- pure helpers
                           the poller (20b) uses; unit-tested here.

`gmail_available()` mirrors gdrive/slack: the Gmail provider needs the
platform `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`; the IMAP provider
needs nothing platform-side.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr

KIND = "email"
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
_TOKEN_URI = "https://oauth2.googleapis.com/token"

# local-parts that are never a real person to reply to
_BOT_LOCALPARTS = {
    "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces", "notifications",
    "notification", "automated", "auto-reply", "autoreply",
}


def gmail_available() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def gmail_authorize_url(redirect_uri: str, state: str) -> str:
    """Gmail-scoped OAuth consent URL (offline access -> a refresh token).
    Reuses the platform GOOGLE_CLIENT_ID; the redirect must be registered on
    that OAuth client (see docs/EMAIL_SETUP.md)."""
    from urllib.parse import urlencode

    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "include_granted_scopes": "true",
    })


def _gmail_credentials(refresh_token: str):
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None, refresh_token=refresh_token, token_uri=_TOKEN_URI,
        client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        scopes=GMAIL_SCOPES,
    )


def gmail_profile_email(refresh_token: str) -> str:
    """The connected mailbox's own address (used to default username / From)."""
    from googleapiclient.discovery import build

    svc = build("gmail", "v1", credentials=_gmail_credentials(refresh_token),
                cache_discovery=False)
    return svc.users().getProfile(userId="me").execute().get("emailAddress", "")


@dataclass
class MailboxConfig:
    tenant_id: str
    provider: str = "imap"                 # 'imap' | 'gmail'
    team: str = "support"
    username: str = ""                     # mailbox address / login
    from_addr: str = ""
    from_name: str = ""
    no_reply_addr: str | None = None
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587
    folder: str = "INBOX"
    auto_send_enabled: bool = False
    status: str = "inactive"
    # persisted poll position — {imap_uid: int} or {internal_date_ms: int}.
    # This, not message read-state, is what stops a message being re-processed:
    # a human opening the mail in the client must not hide it from the poller.
    cursor: dict = field(default_factory=dict, repr=False)
    secret: dict = field(default_factory=dict, repr=False)   # {password} | {refresh_token}

    # ---- (de)serialisation -------------------------------------------------
    @classmethod
    def from_row(cls, tenant_id: str, config: dict, status: str, secret: dict) -> "MailboxConfig":
        c = dict(config or {})
        return cls(
            tenant_id=str(tenant_id),
            provider=c.get("provider", "imap"),
            team=c.get("team", "support"),
            username=c.get("username", ""),
            from_addr=c.get("from_addr") or c.get("username", ""),
            from_name=c.get("from_name", ""),
            no_reply_addr=c.get("no_reply_addr") or None,
            imap_host=c.get("imap_host", ""),
            imap_port=int(c.get("imap_port", 993)),
            smtp_host=c.get("smtp_host", ""),
            smtp_port=int(c.get("smtp_port", 587)),
            folder=c.get("folder", "INBOX"),
            auto_send_enabled=bool(c.get("auto_send_enabled", False)),
            status=status or "inactive",
            secret=secret or {},
        )

    def to_config(self) -> dict:
        """The non-secret jsonb stored on the row."""
        return {
            "provider": self.provider, "team": self.team, "username": self.username,
            "from_addr": self.from_addr or self.username, "from_name": self.from_name,
            "no_reply_addr": self.no_reply_addr,
            "imap_host": self.imap_host, "imap_port": self.imap_port,
            "smtp_host": self.smtp_host, "smtp_port": self.smtp_port,
            "folder": self.folder, "auto_send_enabled": self.auto_send_enabled,
        }

    def public_status(self) -> dict:
        """What the API returns to the browser -- never the secret."""
        return {
            "configured": bool(self.secret) or self.provider == "gmail" and self.status != "inactive",
            "provider": self.provider, "team": self.team,
            "username": self.username, "from_addr": self.from_addr or self.username,
            "from_name": self.from_name, "no_reply_addr": self.no_reply_addr,
            "imap_host": self.imap_host, "imap_port": self.imap_port,
            "smtp_host": self.smtp_host, "smtp_port": self.smtp_port,
            "folder": self.folder, "auto_send_enabled": self.auto_send_enabled,
            "status": self.status,
        }

    @property
    def send_from(self) -> str:
        return self.no_reply_addr or self.from_addr or self.username


# ── storage (service-role Supabase client) ────────────────────────────
def load_channel(tenant_id: str, sb) -> "MailboxConfig | None":
    rows = (sb.table("tenant_integrations")
            .select("config,status,vault_secret_id,cursor")
            .eq("tenant_id", tenant_id).eq("kind", KIND).execute().data or [])
    if not rows:
        return None
    secret: dict = {}
    try:
        raw = sb.rpc("integration_secret_get",
                     {"p_tenant": tenant_id, "p_kind": KIND}).execute().data
        if raw:
            secret = json.loads(raw)
    except Exception:  # noqa: BLE001 -- no secret / bad json -> treat as unconfigured
        secret = {}
    mc = MailboxConfig.from_row(tenant_id, rows[0]["config"], rows[0]["status"], secret)
    mc.cursor = rows[0].get("cursor") or {}
    return mc


def list_active_channels(sb) -> list["MailboxConfig"]:
    """Every tenant whose email channel is switched on (status='active')."""
    rows = (sb.table("tenant_integrations")
            .select("tenant_id").eq("kind", KIND).eq("status", "active")
            .execute().data or [])
    out: list[MailboxConfig] = []
    for r in rows:
        ch = load_channel(r["tenant_id"], sb)
        if ch:
            out.append(ch)
    return out


def _error_backoff_minutes(retries: int) -> int:
    """1, 2, 4, 8, 16, 30, 30 … — a channel that errored is retried on this
    schedule instead of being skipped forever."""
    return min(2 ** max(0, int(retries)), 30)


def list_pollable_channels(sb) -> list["MailboxConfig"]:
    """`active` channels + `error` channels whose backoff window has elapsed
    (Phase-23 auto-recovery — one transient IMAP timeout no longer parks a
    channel permanently)."""
    from datetime import datetime, timedelta, timezone

    rows = (sb.table("tenant_integrations")
            .select("tenant_id,status,last_poll_at,config")
            .eq("kind", KIND).in_("status", ["active", "error"])
            .execute().data or [])
    now = datetime.now(timezone.utc)
    out: list[MailboxConfig] = []
    for r in rows:
        if r.get("status") == "error":
            retries = int((r.get("config") or {}).get("error_retries", 1))
            try:
                last = datetime.fromisoformat(str(r.get("last_poll_at")).replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                last = None
            if last and now - last < timedelta(minutes=_error_backoff_minutes(retries)):
                continue                       # not due for a retry yet
        ch = load_channel(r["tenant_id"], sb)
        if ch:
            out.append(ch)
    return out


def save_channel(tenant_id: str, sb, cfg: "MailboxConfig", *,
                 plaintext_secret: str | None, updated_by: str | None = None) -> None:
    vault_id = None
    if plaintext_secret is not None:
        vault_id = sb.rpc("integration_secret_put", {
            "p_tenant": tenant_id, "p_kind": KIND, "p_plaintext": plaintext_secret,
        }).execute().data
    row = {
        "tenant_id": tenant_id, "kind": KIND, "secret": {},
        "config": cfg.to_config(), "status": cfg.status,
        "updated_by": updated_by, "updated_at": _now_iso(),
    }
    if vault_id:
        row["vault_secret_id"] = vault_id
    sb.table("tenant_integrations").upsert(row, on_conflict="tenant_id,kind").execute()


def delete_channel(tenant_id: str, sb) -> None:
    try:
        sb.rpc("integration_secret_delete",
               {"p_tenant": tenant_id, "p_kind": KIND}).execute()
    except Exception:  # noqa: BLE001
        pass
    sb.table("tenant_integrations").delete().eq("tenant_id", tenant_id).eq("kind", KIND).execute()


def set_status(tenant_id: str, sb, status: str, *, error: str | None = None) -> None:
    patch: dict = {"status": status, "last_error": error, "last_poll_at": _now_iso()}
    # track consecutive errors so list_pollable_channels can back off / recover
    try:
        cur = (sb.table("tenant_integrations").select("config")
               .eq("tenant_id", tenant_id).eq("kind", KIND).execute().data or [{}])[0]
        cfg = dict(cur.get("config") or {})
        if status == "error":
            cfg["error_retries"] = min(int(cfg.get("error_retries", 0)) + 1, 8)
        elif status == "active" and cfg.pop("error_retries", None) is None:
            cfg = None            # nothing to change
        if cfg is not None:
            patch["config"] = cfg
    except Exception:  # noqa: BLE001 — status update must not fail on this
        pass
    sb.table("tenant_integrations").update(patch) \
        .eq("tenant_id", tenant_id).eq("kind", KIND).execute()


def set_cursor(tenant_id: str, sb, cursor: dict) -> None:
    """Persist the poll position (highest IMAP UID / Gmail internalDate handled)."""
    sb.table("tenant_integrations").update({"cursor": cursor}) \
        .eq("tenant_id", tenant_id).eq("kind", KIND).execute()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── connection check (used by POST /api/integrations/email/test) ──────
def test_connection(cfg: "MailboxConfig") -> dict:
    """Log in and back out. Returns {ok, imap, smtp, error}. Never raises."""
    if cfg.provider == "gmail":
        return _test_gmail(cfg)
    return _test_imap_smtp(cfg)


def _test_imap_smtp(cfg: "MailboxConfig") -> dict:
    import imaplib
    import smtplib

    out: dict = {"imap": False, "smtp": False, "ok": False, "error": None}
    pw = cfg.secret.get("password", "")
    if not (cfg.imap_host and cfg.username and pw):
        out["error"] = "imap_host, username and password are required"
        return out
    try:
        m = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=15)
        m.login(cfg.username, pw)
        typ, _ = m.select(cfg.folder, readonly=True)
        m.logout()
        if typ != "OK":
            out["error"] = f"folder {cfg.folder!r} not selectable"
            return out
        out["imap"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = f"IMAP: {e}"
        return out
    if cfg.smtp_host:
        try:
            s = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
            s.starttls()
            s.login(cfg.username, pw)
            s.quit()
            out["smtp"] = True
        except Exception as e:  # noqa: BLE001
            out["error"] = f"SMTP: {e}"
            return out
    out["ok"] = out["imap"] and (out["smtp"] or not cfg.smtp_host)
    return out


def _test_gmail(cfg: "MailboxConfig") -> dict:
    rt = cfg.secret.get("refresh_token")
    if not rt:
        return {"ok": False, "error": "Gmail is not connected yet"}
    try:
        from google.auth.transport.requests import Request

        creds = _gmail_credentials(rt)
        creds.refresh(Request())
        return {"ok": True, "imap": True, "smtp": True, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Gmail: {e}"}


# ── fetching new mail (20b) ─────────────────────────────────────────
@dataclass
class FetchedMessage:
    ref: str            # IMAP uid (str) | Gmail message id
    raw: bytes          # RFC-822 bytes
    sort_key: int = 0   # IMAP UID (int) | Gmail internalDate (ms) — for the cursor


def fetch_new(cfg: "MailboxConfig", *, lookback_days: int = 3,
              limit: int = 50) -> list["FetchedMessage"]:
    if cfg.provider == "gmail":
        return _fetch_gmail(cfg, lookback_days, limit)
    return _fetch_imap(cfg, lookback_days, limit)


def mark_processed(cfg: "MailboxConfig", refs: list[str]) -> None:
    if not refs:
        return
    if cfg.provider == "gmail":
        _mark_gmail(cfg, refs)
    else:
        _mark_imap(cfg, refs)


def _imap_login(cfg: "MailboxConfig"):
    import imaplib

    pw = cfg.secret.get("password", "")
    m = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=30)
    m.login(cfg.username, pw)
    return m


def _imap_search_args(cursor: dict, lookback_days: int) -> tuple[str, ...]:
    """IMAP SEARCH criteria. Past the first run we ask for `UID > cursor`
    (read-state independent); the first run is time-bounded by `lookback`."""
    last = int((cursor or {}).get("imap_uid") or 0)
    if last > 0:
        return ("UID", f"{last + 1}:*")
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))
             ).strftime("%d-%b-%Y")
    return ("SINCE", since)


def _fetch_imap(cfg: "MailboxConfig", lookback_days: int, limit: int) -> list["FetchedMessage"]:
    m = _imap_login(cfg)
    out: list[FetchedMessage] = []
    last = int((cfg.cursor or {}).get("imap_uid") or 0)
    try:
        typ, _ = m.select(cfg.folder, readonly=False)
        if typ != "OK":
            raise RuntimeError(f"cannot select folder {cfg.folder!r}")
        typ, data = m.uid("SEARCH", *_imap_search_args(cfg.cursor, lookback_days))
        # 'UID n:*' always returns at least the last message even when none are
        # actually newer — drop anything not past the cursor, then cap.
        uids = [u for u in (data[0].split() if data and data[0] else [])
                if int(u) > last][:limit]
        for uid in uids:
            typ, msg_data = m.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            out.append(FetchedMessage(ref=uid.decode(), raw=msg_data[0][1], sort_key=int(uid)))
    finally:
        try:
            m.close()
            m.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def _mark_imap(cfg: "MailboxConfig", refs: list[str]) -> None:
    m = _imap_login(cfg)
    try:
        m.select(cfg.folder, readonly=False)
        m.uid("STORE", ",".join(refs), "+FLAGS", "(\\Seen)")   # courtesy for humans; not load-bearing
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


def _gmail_service(cfg: "MailboxConfig"):
    from googleapiclient.discovery import build

    return build("gmail", "v1",
                 credentials=_gmail_credentials(cfg.secret.get("refresh_token", "")),
                 cache_discovery=False)


def _gmail_query(cursor: dict, lookback_days: int) -> str:
    """Gmail search. Past the first run, `after:<epoch>` from the cursor
    (read-state independent); the first run is time-bounded by `lookback`."""
    after_ms = int((cursor or {}).get("internal_date_ms") or 0)
    if after_ms > 0:
        return f"in:inbox after:{after_ms // 1000}"
    return f"in:inbox newer_than:{max(1, lookback_days)}d"


def _fetch_gmail(cfg: "MailboxConfig", lookback_days: int, limit: int) -> list["FetchedMessage"]:
    import base64

    svc = _gmail_service(cfg)
    after_ms = int((cfg.cursor or {}).get("internal_date_ms") or 0)
    q = _gmail_query(cfg.cursor, lookback_days)
    resp = svc.users().messages().list(userId="me", q=q, maxResults=limit).execute()
    out: list[FetchedMessage] = []
    for ref in [m["id"] for m in resp.get("messages", [])]:
        full = svc.users().messages().get(userId="me", id=ref, format="raw").execute()
        idt = int(full.get("internalDate") or 0)
        if idt and after_ms and idt <= after_ms:      # `after:` is coarse — filter precisely
            continue
        out.append(FetchedMessage(ref=ref, sort_key=idt,
                                  raw=base64.urlsafe_b64decode(full["raw"].encode())))
    return out


def _mark_gmail(cfg: "MailboxConfig", refs: list[str]) -> None:
    svc = _gmail_service(cfg)
    svc.users().messages().batchModify(
        userId="me", body={"ids": refs, "removeLabelIds": ["UNREAD"]}
    ).execute()


def mark_needs_human(cfg: "MailboxConfig", message_id: str) -> None:
    """Hand a message back to a human: re-mark it unread and flag/star it.
    Looks the message up by its Message-ID (the poller already marked it
    read on enqueue). Best-effort."""
    if not message_id:
        return
    if cfg.provider == "gmail":
        svc = _gmail_service(cfg)
        found = svc.users().messages().list(
            userId="me", q=f"rfc822msgid:{message_id}").execute().get("messages", [])
        ids = [m["id"] for m in found]
        if ids:
            svc.users().messages().batchModify(
                userId="me", body={"ids": ids, "addLabelIds": ["UNREAD", "STARRED"]}
            ).execute()
        return
    m = _imap_login(cfg)
    try:
        m.select(cfg.folder, readonly=False)
        typ, data = m.search(None, "HEADER", "Message-ID", message_id)
        uids = data[0].split() if data and data[0] else []
        if uids:
            joined = b",".join(uids).decode()
            m.store(joined, "-FLAGS", "(\\Seen)")
            m.store(joined, "+FLAGS", "(\\Flagged)")
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


# ── pure helpers for the poller ─────────────────────────────────────
def thread_key(case: dict) -> str:
    """A stable key for a conversation -- the thread root, so a customer's
    reply correlates with the run that asked them for more info (Phase 17d
    clarify rounds key on `case_id`)."""
    refs = case.get("references") or []
    return (refs[0] if refs else "") or case.get("in_reply_to") or case.get("message_id") or ""


def should_process(case: dict, cfg: "MailboxConfig") -> tuple[bool, str]:
    """Loop-breaker gate for an inbound message. Pure."""
    frm = (case.get("from") or "").lower()
    if not frm:
        return False, "no From address"
    if case.get("is_autoreply"):
        return False, "auto-responder / bulk / list mail"
    own = {x.lower() for x in (cfg.username, cfg.from_addr, cfg.no_reply_addr or "") if x}
    if frm in own:
        return False, "message is from this mailbox itself"
    if not (case.get("subject") or case.get("body")):
        return False, "empty message"
    return True, "ok"


def looks_like_bot_address(addr: str) -> bool:
    local = (parseaddr(addr or "")[1].split("@", 1) or [""])[0].lower()
    return local in _BOT_LOCALPARTS or local.startswith(("no-reply", "noreply", "donotreply"))


def is_autoreply(headers: dict) -> bool:
    """True for vacation responders, list mail, bounces, bulk mail -- anything
    we must not answer (headers: a case-insensitive dict-like of the message)."""
    h = {str(k).lower(): str(v or "") for k, v in dict(headers).items()}
    auto = h.get("auto-submitted", "").lower()
    if auto and auto != "no":
        return True
    if h.get("x-autoreply") or h.get("x-autorespond") or h.get("x-auto-response-suppress"):
        return True
    if h.get("precedence", "").lower() in {"bulk", "junk", "list", "auto_reply"}:
        return True
    if h.get("list-id") or h.get("list-unsubscribe"):
        return True
    if h.get("x-support-bot"):                       # our own outbound (20c stamps this)
        return True
    return looks_like_bot_address(h.get("from", ""))


def parse_message(raw: bytes) -> dict:
    """RFC-822 bytes -> the `case` dict the flow runs on. Pure."""
    import email
    from email import policy

    msg = email.message_from_bytes(raw, policy=policy.default)
    name, addr = parseaddr(msg.get("From", ""))
    refs = [r for r in (msg.get("References", "") or "").replace(",", " ").split() if r]

    body = ""
    if msg.is_multipart():
        plain = next((p for p in msg.walk() if p.get_content_type() == "text/plain"
                      and "attachment" not in str(p.get("Content-Disposition", ""))), None)
        if plain is not None:
            body = plain.get_content()
        else:
            html = next((p for p in msg.walk() if p.get_content_type() == "text/html"), None)
            if html is not None:
                body = _strip_html(html.get_content())
    else:
        payload = msg.get_content()
        body = payload if msg.get_content_type() == "text/plain" else _strip_html(payload)

    headers = {k: v for k, v in msg.items()}
    return {
        "channel": "email",
        "from": addr,
        "from_name": name or "",
        "to": [a for _, a in getaddresses(msg.get_all("To", []))],
        "subject": (msg.get("Subject", "") or "").strip(),
        "body": (body or "").strip(),
        "message_id": (msg.get("Message-ID", "") or "").strip(),
        "in_reply_to": (msg.get("In-Reply-To", "") or "").strip(),
        "references": refs,
        "date": msg.get("Date", ""),
        "is_autoreply": is_autoreply(headers),
    }


def _strip_html(s: str) -> str:
    import re

    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\n{3,}", "\n\n", s).strip()
