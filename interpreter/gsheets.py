"""
Google Sheets connector (KB source connector #2 — docs/KB_SOURCE_CONNECTORS.md).

A support/FAQ spreadsheet is structured Q&A/tabular data, not prose —
treating a whole sheet as one `body_md` blob would destroy retrieval
quality (a 200-row sheet becomes one giant chunk nothing scores well
against). This produces **one KB document per data row** instead: the
first row is a header, each subsequent row becomes `{title, body_md}`
where the title is the row's first non-empty cell and the body is a
`**Header:** value` list of every non-empty column — reads naturally for
a `Question`/`Answer`-shaped sheet without hardcoding those exact column
names (works for any header shape).

Reuses the exact same Google connection `gdrive.py` already manages
(`tenant_integrations`, kind='google', one refresh_token per tenant) — no
new OAuth flow. `gdrive.SCOPES` now also requests `spreadsheets.readonly`;
a tenant connected before this shipped needs to reconnect once for Sheets
access to work (Google doesn't retroactively grant a new scope to an
existing token).
"""

from __future__ import annotations

import re
from typing import Any

from . import gdrive


def parse_sheet_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", s):
        return s
    raise ValueError(f"not a Google Sheets URL or id: {url_or_id!r}")


def _services(tenant_id: str, sb):
    from googleapiclient.discovery import build

    creds = gdrive._credentials(gdrive._integration(tenant_id, sb)["refresh_token"])
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return drive, sheets


def _first_tab_name(sheets, sheet_id: str) -> str:
    meta = sheets.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties.title").execute()
    props = ((meta.get("sheets") or [{}])[0]).get("properties", {})
    return props.get("title") or "Sheet1"


def _row_doc(headers: list[str], row: list[str]) -> tuple[str, str] | None:
    """`(title, body_md)` for one data row, or `None` for a fully-empty one."""
    pairs = [(h, v) for h, v in zip(headers, row) if (v or "").strip()]
    if not pairs:
        return None
    title = pairs[0][1].strip()[:200]
    body = "\n".join(f"**{h.strip()}:** {v.strip()}" for h, v in pairs if h.strip())
    return title, body


def fetch_sheet(tenant_id: str, sheet_id: str, *, sheet_name: str | None = None,
                sb) -> dict[str, Any]:
    """-> {title, modified_time, tab, rows: [{row, title, body_md}, ...]}.
    Raises on auth / not-found / wrong-mimetype, same discipline as
    `gdrive.fetch_doc`. `rows` skips fully-empty rows; `row` is the
    1-based row number within `tab` (header is row 1, so data starts at 2)
    — used as this row's stable-ish identity for re-sync (not stable
    across a row insert/delete that shifts numbering — a known limit, not
    silently pretended away)."""
    drive, sheets = _services(tenant_id, sb)
    meta = drive.files().get(fileId=sheet_id, fields="name,modifiedTime,mimeType").execute()
    if meta.get("mimeType") != "application/vnd.google-apps.spreadsheet":
        raise ValueError(f"{sheet_id} is not a Google Sheet ({meta.get('mimeType')})")
    tab = sheet_name or _first_tab_name(sheets, sheet_id)
    values = (sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=tab)
              .execute().get("values", []))
    rows: list[dict[str, Any]] = []
    if values:
        headers, data = values[0], values[1:]
        for i, row in enumerate(data, start=2):
            doc = _row_doc(headers, row)
            if doc:
                rows.append({"row": i, "title": doc[0], "body_md": doc[1]})
    return {"title": meta.get("name") or sheet_id, "modified_time": meta.get("modifiedTime"),
           "tab": tab, "rows": rows}
