"""
Linear KB source connector (docs/KB_SOURCE_CONNECTORS.md §4).

Read-only. Pulls two content types:

  * **Documents** — Linear's long-form wiki pages. One KBDocument each,
    direct markdown, `quality="official"`.
  * **Resolved issues** (`state.type == "completed"`) + their comment
    thread — a shipped bug's root-cause discussion is real institutional
    knowledge. A still-open / canceled / bodyless issue isn't worth
    embedding — same "pattern vs proof" filter `interpreter/case_memory.py`
    applies to resolved support cases. `quality="community_resolved"`.

Auth: a Linear **personal API key** (Settings -> API -> Personal API keys),
stored in Supabase Vault (kind='linear'). GraphQL at
https://api.linear.app/graphql; the key is the raw `Authorization` header
value (no "Bearer"). Best-effort like every sibling module — a missing key
raises, which the generic sync driver records as the connection's error.
"""

from __future__ import annotations

import logging

KIND = "linear"
_GQL = "https://api.linear.app/graphql"
log = logging.getLogger("interpreter.linear")


def _key(tenant_id: str, sb, override: str | None = None) -> str:
    if override:
        return override
    from . import vault_secrets

    k = (vault_secrets.get(tenant_id, KIND, sb=sb) or {}).get("api_key")
    if not k:
        raise RuntimeError("no Linear API key stored for this tenant")
    return k


def available(tenant_id: str | None, sb) -> "tuple[bool, str | None]":
    try:
        from . import vault_secrets

        if (vault_secrets.get(tenant_id, KIND, sb=sb) or {}).get("api_key"):
            return True, None
    except Exception:  # noqa: BLE001
        pass
    return False, "Add a Linear API key (Settings → API → Personal API keys)"


def _gql(tenant_id: str, sb, query: str, variables: dict | None = None,
         *, api_key: str | None = None) -> dict:
    import requests

    r = requests.post(
        _GQL, json={"query": query, "variables": variables or {}},
        headers={"Authorization": _key(tenant_id, sb, api_key),
                 "Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Linear API {r.status_code}: {r.text[:300]}")
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"Linear GraphQL error: {str(body['errors'])[:300]}")
    return body["data"]


def test_connection(tenant_id: str | None, sb, *, api_key: str | None = None) -> dict:
    """A trivial authed read (`{ viewer { id name } }`) — saves nothing.
    Uses `api_key` if given (not-yet-saved key from the form), else the
    stored one. -> {ok, detail}."""
    try:
        v = (_gql(tenant_id, sb, "{ viewer { id name } }", api_key=api_key) or {}).get("viewer") or {}
        return {"ok": True, "detail": f"connected as {v.get('name') or v.get('id') or 'a user'}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:300]}


_DOCS_Q = """
query Docs($after: String, $filter: DocumentFilter) {
  documents(first: 50, after: $after, filter: $filter) {
    pageInfo { hasNextPage endCursor }
    nodes { id title content updatedAt url }
  }
}"""

_ISSUES_Q = """
query Issues($after: String, $filter: IssueFilter) {
  issues(first: 50, after: $after, filter: $filter) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id identifier title description url updatedAt
      comments(first: 20) { nodes { body createdAt user { name } } }
    }
  }
}"""


def _page(tenant_id, sb, query, key, variables, *, limit: int | None = None):
    out, after = [], None
    while True:
        v = dict(variables or {})
        v["after"] = after
        conn = _gql(tenant_id, sb, query, v)[key]
        out.extend(conn.get("nodes") or [])
        if limit and len(out) >= limit:
            return out[:limit]
        if not (conn.get("pageInfo") or {}).get("hasNextPage"):
            return out
        after = conn["pageInfo"]["endCursor"]


def fetch_documents(tenant_id: str, sb, *, limit: int | None = None,
                    updated_after: str | None = None) -> list[dict]:
    filt = {"updatedAt": {"gt": updated_after}} if updated_after else None
    return _page(tenant_id, sb, _DOCS_Q, "documents", {"filter": filt}, limit=limit)


def fetch_resolved_issues(tenant_id: str, sb, *, team_key: str | None = None,
                          limit: int | None = None,
                          updated_after: str | None = None) -> list[dict]:
    filt: dict = {"state": {"type": {"eq": "completed"}}}
    if team_key:
        filt["team"] = {"key": {"eq": team_key}}
    if updated_after:
        filt["updatedAt"] = {"gt": updated_after}
    return _page(tenant_id, sb, _ISSUES_Q, "issues", {"filter": filt}, limit=limit)
