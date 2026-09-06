"""
Nolt KB source connector (docs/KB_SOURCE_CONNECTORS.md §6).

Read-only. Nolt (nolt.io) is a hosted feedback/roadmap board: users post
requests/bugs, vote, and comment; an admin sets each post's status. Only a
**resolved** post — status "done"/"complete"/"shipped", i.e. the change
landed and the resolution is in the post body or a pinned admin comment —
is embedded (`quality="community_resolved"`). An open / planned / declined
post is a wish, not an answer: skipped. Same "pattern vs proof" discipline
as `interpreter/linear.py`'s resolved-issue filter.

Auth: a board **API key** (Nolt board admin -> API), stored in Supabase
Vault (kind='nolt'), sent as the raw `Authorization` header. REST at
https://api.nolt.io.
"""

from __future__ import annotations

import logging

KIND = "nolt"
_API = "https://api.nolt.io"
log = logging.getLogger("interpreter.nolt")

_RESOLVED = ("done", "complete", "completed", "shipped", "released", "live")


def _key(tenant_id: str, sb) -> str:
    from . import vault_secrets

    k = (vault_secrets.get(tenant_id, KIND, sb=sb) or {}).get("api_key")
    if not k:
        raise RuntimeError("no Nolt API key stored for this tenant")
    return k


def available(tenant_id: str | None, sb) -> "tuple[bool, str | None]":
    try:
        from . import vault_secrets

        if (vault_secrets.get(tenant_id, KIND, sb=sb) or {}).get("api_key"):
            return True, None
    except Exception:  # noqa: BLE001
        pass
    return False, "Add a Nolt board API key (board admin → API)"


def _get(tenant_id: str, sb, path: str, params: dict | None = None) -> "list | dict":
    import requests

    r = requests.get(
        f"{_API}{path}", params=params or {},
        headers={"Authorization": _key(tenant_id, sb)}, timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Nolt API {r.status_code} on {path}: {r.text[:300]}")
    return r.json()


def _is_resolved(post: dict) -> bool:
    st = ((post.get("status") or {}).get("title") or "").strip().lower()
    return st in _RESOLVED


def fetch_resolved_posts(tenant_id: str, sb, board_id: str, *, max_posts: int = 500) -> list[dict]:
    """Resolved posts on the board, each with `_comments` (up to 10) attached."""
    out: list[dict] = []
    skip = 0
    while skip < max_posts:
        batch = _get(tenant_id, sb, f"/v1/boards/{board_id}/posts",
                     {"limit": 50, "skip": skip})
        rows = batch if isinstance(batch, list) else (batch.get("data") or batch.get("posts") or [])
        if not rows:
            break
        for p in rows:
            if _is_resolved(p):
                try:
                    c = _get(tenant_id, sb, f"/v1/posts/{p['id']}/comments", {"limit": 10})
                    p["_comments"] = c if isinstance(c, list) else (c.get("data") or [])
                except Exception as e:  # noqa: BLE001
                    log.warning("nolt comments for %s: %s", p.get("id"), e)
                    p["_comments"] = []
                out.append(p)
        skip += len(rows)
        if len(rows) < 50:
            break
    return out
