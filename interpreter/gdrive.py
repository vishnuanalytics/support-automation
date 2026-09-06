"""
Google Drive / Docs connector (Phase 15).

A tenant connects Google once (OAuth, offline access); the refresh token is
stored in `tenant_integrations (tenant_id, kind='google')`. A linked doc
becomes a `kb_entries` row with `origin='gdoc'` inside an `internal_kb`
collection — synced, not hand-edited. `ingestion/sources/gdoc_sync.py`
re-exports it when Drive reports a newer `modifiedTime`.

Mirrors `salesforce.py`: everything degrades to a clear error when
`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` aren't set, so the rest of the
app (and CI) runs without Google creds. `docs_json_to_markdown` is a pure
function and unit-tested offline.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger("interpreter.gdrive")

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    # 2026-09-05 — Google Sheets KB connector (interpreter/gsheets.py). A
    # tenant that connected before this shipped needs to reconnect once;
    # Google doesn't retroactively grant a new scope to an existing token.
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    # 2026-09-06 — KB write-back (docs/KB_SOURCE_CONNECTORS.md §2). Only
    # exercised for a gdocs connection with `config.access == 'write_back'`;
    # a read-only tenant never uses it, and an existing token keeps working
    # with its old (read-only) scopes — the job just fails gracefully until
    # the tenant re-runs Google consent. `documents` = rewrite the passage;
    # `drive` = post the verification comment on the doc (best-effort).
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]
_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


def available() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def _need() -> tuple[str, str]:
    cid, secret = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
    if not (cid and secret):
        raise RuntimeError(
            "Google is not configured — set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET "
            "in .env (see docs/GOOGLE_SETUP.md)"
        )
    return cid, secret


# ── doc id / url ──────────────────────────────────────────────────────
def parse_doc_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", s):
        return s
    raise ValueError(f"not a Google Doc URL or id: {url_or_id!r}")


# ── OAuth ────────────────────────────────────────────────────────────
def authorize_url(redirect_uri: str, state: str) -> str:
    cid, _ = _need()
    from urllib.parse import urlencode

    q = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",              # force a refresh_token every time
        "state": state,
        "include_granted_scopes": "true",
    }
    return f"{_AUTH_URI}?{urlencode(q)}"


def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    """code -> {refresh_token, token, ...}. Needs `requests` (transitively
    pulled in by google-auth libs)."""
    cid, secret = _need()
    import requests

    r = requests.post(_TOKEN_URI, data={
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, timeout=15)
    r.raise_for_status()
    body = r.json()
    if "refresh_token" not in body:
        raise RuntimeError("Google did not return a refresh_token "
                           "(revoke the app's access and retry with prompt=consent)")
    return body


# ── credentials / API clients ────────────────────────────────────────
def _credentials(refresh_token: str):
    cid, secret = _need()
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None, refresh_token=refresh_token,
        token_uri=_TOKEN_URI, client_id=cid, client_secret=secret, scopes=SCOPES,
    )


def _integration(tenant_id: str, sb) -> dict[str, Any]:
    from interpreter import vault_secrets

    secret = vault_secrets.get(tenant_id, "google", sb=sb)
    if not secret:
        raise RuntimeError(f"tenant {tenant_id} has not connected Google")
    return secret


def connected(tenant_id: str, sb) -> bool:
    try:
        return bool(_integration(tenant_id, sb).get("refresh_token"))
    except Exception:  # noqa: BLE001
        return False


def _services(tenant_id: str, sb):
    from googleapiclient.discovery import build

    creds = _credentials(_integration(tenant_id, sb)["refresh_token"])
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    return drive, docs


def fetch_doc(tenant_id: str, doc_id: str, sb) -> dict[str, Any]:
    """-> {title, markdown, modified_time}. Raises on auth / not-found."""
    drive, docs = _services(tenant_id, sb)
    meta = drive.files().get(fileId=doc_id, fields="name,modifiedTime,mimeType").execute()
    if meta.get("mimeType") != "application/vnd.google-apps.document":
        raise ValueError(f"{doc_id} is not a Google Doc ({meta.get('mimeType')})")
    doc = docs.documents().get(documentId=doc_id).execute()
    return {
        "title": meta.get("name") or doc.get("title") or doc_id,
        "markdown": docs_json_to_markdown(doc),
        "modified_time": meta.get("modifiedTime"),
    }


def get_modified_time(tenant_id: str, doc_id: str, sb) -> str:
    drive, _ = _services(tenant_id, sb)
    return drive.files().get(fileId=doc_id, fields="modifiedTime").execute()["modifiedTime"]


# ── write-back (KB write-back, docs/KB_SOURCE_CONNECTORS.md §2) ──────────
# Only reached for a gdocs connection with `config.access == 'write_back'`.
# Needs the read-write `documents` / `drive` scopes (see SCOPES) — a tenant
# on an older read-only token gets a clean failure, not a silent no-op.
def replace_passage(tenant_id: str, doc_id: str, old_text: str, new_text: str,
                    sb) -> int:
    """In-place rewrite of one passage via `documents.batchUpdate` /
    `replaceAllText`. Returns the number of occurrences replaced — 0 means
    the old text wasn't found verbatim (formatting split the runs, or the
    doc already changed), and the caller should fall back to flagging the
    block for a manual edit rather than guessing."""
    if not (old_text or "").strip():
        return 0
    _, docs = _services(tenant_id, sb)
    resp = docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{
            "replaceAllText": {
                "containsText": {"text": old_text, "matchCase": True},
                "replaceText": new_text,
            }
        }]},
    ).execute()
    for r in resp.get("replies", []):
        rat = (r.get("replaceAllText") or {}).get("occurrencesChanged")
        if rat is not None:
            return int(rat)
    return 0


def comment(tenant_id: str, doc_id: str, text: str, sb) -> str | None:
    """Post an (unanchored) comment on the doc for human visibility. Best
    effort — returns the comment id, or None if the token can't comment
    (older read-only scope); the caller must not depend on it."""
    try:
        drive, _ = _services(tenant_id, sb)
        c = drive.comments().create(
            fileId=doc_id, fields="id", body={"content": text},
        ).execute()
        return c.get("id")
    except Exception as e:  # noqa: BLE001
        log.warning("gdrive.comment(%s): %s", doc_id, e)
        return None


# ── Docs JSON -> Markdown (pure) ─────────────────────────────────────
_HEADING = {
    "TITLE": "# ", "SUBTITLE": "## ",
    "HEADING_1": "# ", "HEADING_2": "## ", "HEADING_3": "### ",
    "HEADING_4": "#### ", "HEADING_5": "##### ", "HEADING_6": "###### ",
}


def _para_text(para: dict[str, Any]) -> str:
    out = []
    for el in para.get("elements", []):
        tr = el.get("textRun")
        if not tr:
            continue
        t = tr.get("content", "")
        style = tr.get("textStyle", {})
        if t.strip():
            if style.get("bold"):
                t = f"**{t.rstrip()}**" + (" " if t.endswith(" ") else "")
            if style.get("italic"):
                t = f"*{t.rstrip()}*" + (" " if t.endswith(" ") else "")
        out.append(t)
    return "".join(out).replace("\x0b", " ").rstrip("\n")


def docs_json_to_markdown(doc: dict[str, Any]) -> str:
    lines: list[str] = []
    content = (doc.get("body") or {}).get("content", [])
    for block in content:
        if "paragraph" in block:
            para = block["paragraph"]
            text = _para_text(para)
            if not text.strip():
                lines.append("")
                continue
            style = (para.get("paragraphStyle") or {}).get("namedStyleType", "NORMAL_TEXT")
            if style in _HEADING:
                lines.append(_HEADING[style] + text.strip())
            elif "bullet" in para:
                lines.append(f"- {text.strip()}")
            else:
                lines.append(text.strip())
        elif "table" in block:
            for row in block["table"].get("tableRows", []):
                cells = []
                for cell in row.get("tableCells", []):
                    ct = " ".join(
                        _para_text(c["paragraph"]).strip()
                        for c in cell.get("content", []) if "paragraph" in c
                    )
                    cells.append(ct.replace("|", "\\|"))
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    md = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
