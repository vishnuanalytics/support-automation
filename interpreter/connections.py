"""
P6c — per-tenant HTTP connections for the `http_request` flow node.

A connection is `{base_url, auth}`; the node names a `slug` and can only reach
paths under that `base_url`. The `auth` dict (with the secret) is read with the
service role and never leaves the server.

    resolve(tenant_id, slug) -> {"base_url": ..., "auth": {...}} | None
    auth_headers(auth)       -> {"Authorization": ...} etc.
    redact(row)              -> the row minus the secret, for the API
"""

from __future__ import annotations

import base64
import logging
from typing import Any

log = logging.getLogger("interpreter.connections")

_SAFE_AUTH_KEYS = ("type", "header_name", "username")   # everything else is secret


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def resolve(tenant_id: str | None, slug: str, *, sb=None) -> dict[str, Any] | None:
    if not (tenant_id and slug):
        return None
    try:
        rows = ((sb or _sb()).table("connections")
                .select("base_url, auth")
                .eq("tenant_id", str(tenant_id)).eq("slug", slug)
                .limit(1).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("connections.resolve(%s): %s", slug, e)
        return None
    return rows[0] if rows else None


def auth_headers(auth: dict[str, Any] | None) -> dict[str, str]:
    a = auth or {}
    kind = (a.get("type") or "none").lower()
    if kind == "bearer" and a.get("token"):
        return {"Authorization": f"Bearer {a['token']}"}
    if kind == "header" and a.get("header_name") and a.get("value"):
        return {a["header_name"]: a["value"]}
    if kind == "basic" and a.get("username") is not None:
        raw = f"{a.get('username','')}:{a.get('password','')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    return {}


def redact(row: dict[str, Any]) -> dict[str, Any]:
    auth = row.get("auth") or {}
    return {
        "slug": row.get("slug"),
        "base_url": row.get("base_url"),
        "auth": {k: auth[k] for k in _SAFE_AUTH_KEYS if k in auth},
        "has_secret": any(k not in _SAFE_AUTH_KEYS for k in auth),
        "created_at": row.get("created_at"),
    }
