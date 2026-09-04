"""
P6c — per-tenant HTTP connections for the `http_request` flow node.

A connection is `{base_url, auth}`; a node names a `slug` and can only reach
paths under that `base_url`. The `auth` dict (with the secret) is read with the
service role and never leaves the server.

    resolve(tenant_id, slug) -> {"connection_id": ..., "base_url": ..., "auth": {...}} | None
    auth_headers(auth)       -> {"Authorization": ...} etc.
    redact(row)              -> the row minus the secret, for the API
    execute(connection, ...) -> {"status", "ok", "json"|"text"} — the safe-request
                                 logic shared by `h_http_request` and a saved
                                 `connection_actions` row (FR-47, see below)

FR-47 addition: a connection can also carry named, reusable **actions**
(`connection_actions` — method/path/params/body_template), turning it into a
`connectors.ConnectorSpec` (`as_connector`/`as_connectors`) so any tenant's own
REST API is a first-class connector next to the Salesforce/Slack builtins —
addable from the web UI with zero Python changes.
"""

from __future__ import annotations

import base64
import logging
import re
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
                .select("connection_id, base_url, auth")
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


def execute(connection: dict[str, Any], *, method: str, path: str,
            query: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
            body: Any = None, timeout: float = 15) -> dict[str, Any]:
    """Make one HTTP call through a connection's `base_url` + `auth`. Shared by
    `h_http_request` (registry.py) and a saved `connection_actions` row — same
    allow-list (an absolute `path` is rejected) and response shape either way.
    Raises on a transport error or an absolute path; callers decide the
    `on_error` policy, matching `h_http_request`'s pre-existing contract."""
    import requests  # noqa: PLC0415

    if "://" in path:
        raise ValueError("path must be relative to the connection base_url")
    url = connection["base_url"].rstrip("/") + "/" + path.lstrip("/")
    hdrs = {**auth_headers(connection.get("auth")), **(headers or {})}
    r = requests.request(method.upper(), url, headers=hdrs or None, params=query or None,
                         json=body, timeout=timeout)
    ct = (r.headers.get("content-type") or "").lower()
    payload = r.json() if "json" in ct else None
    return {"status": r.status_code, "ok": 200 <= r.status_code < 300,
            "json": payload, "text": None if payload is not None else r.text[:8000]}


def list_actions(connection_id: str, *, sb=None) -> list[dict[str, Any]]:
    rows = ((sb or _sb()).table("connection_actions")
            .select("action_id, name, method, path, params, body_template")
            .eq("connection_id", connection_id).order("name").execute().data or [])
    return rows


def save_action(connection_id: str, name: str, *, method: str, path: str,
                 params: list[dict[str, Any]] | None = None,
                 body_template: Any = None, sb=None) -> dict[str, Any]:
    row = ((sb or _sb()).table("connection_actions").upsert({
        "connection_id": connection_id, "name": name.strip(),
        "method": method.upper(), "path": path,
        "params": params or [], "body_template": body_template,
    }, on_conflict="connection_id,name").execute().data[0])
    return row


def delete_action(connection_id: str, name: str, *, sb=None) -> None:
    (sb or _sb()).table("connection_actions").delete() \
        .eq("connection_id", connection_id).eq("name", name).execute()


def _fill(value: Any, params: dict[str, Any]) -> Any:
    """`{{ dotted.path }}` substitution over a flat `params` dict — the same
    idiom as registry.py's `_render_template`, but over an action's own saved
    `path`/`body_template` rather than the full flow state (connections.py
    has no notion of `CaseState`)."""
    if isinstance(value, str):
        def sub(m: "re.Match[str]") -> str:
            v: Any = params
            for part in m.group(1).strip().split("."):
                v = v.get(part) if isinstance(v, dict) else None
                if v is None:
                    return ""
            return str(v)
        return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", sub, value)
    if isinstance(value, dict):
        return {k: _fill(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, params) for v in value]
    return value


def _action_impl(connection_row: dict[str, Any], action_row: dict[str, Any]):
    def impl(tenant_id: str | None, org_label: str | None, params: dict[str, Any]) -> dict[str, Any]:
        path = _fill(action_row["path"], params)
        body = _fill(action_row.get("body_template"), params) if action_row.get("body_template") else None
        query = _fill(params.get("query"), params) if isinstance(params.get("query"), dict) else None
        return execute(connection_row, method=action_row.get("method", "GET"),
                        path=path, query=query, body=body)
    return impl


def as_connector(tenant_id: str | None, slug: str, *, sb=None) -> "connectors.ConnectorSpec | None":  # noqa: F821
    from .connectors import ActionSpec, ConnectorSpec

    conn = resolve(tenant_id, slug, sb=sb)
    if not conn:
        return None
    actions = {
        a["name"]: ActionSpec(a["name"], f"{a.get('method','GET')} {a.get('path','')}",
                               params=a.get("params") or [], impl=_action_impl(conn, a))
        for a in list_actions(conn["connection_id"], sb=sb)
    }
    return ConnectorSpec(slug=slug, label=slug, auth="apikey", actions=actions)


def as_connectors(tenant_id: str | None, *, sb=None) -> "list[connectors.ConnectorSpec]":  # noqa: F821
    if not tenant_id:
        return []
    rows = ((sb or _sb()).table("connections").select("slug")
            .eq("tenant_id", str(tenant_id)).order("slug").execute().data or [])
    out = []
    for r in rows:
        spec = as_connector(tenant_id, r["slug"], sb=sb)
        if spec:
            out.append(spec)
    return out
