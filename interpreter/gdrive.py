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

import os
import re
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
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
