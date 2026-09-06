"""
Discourse KB source connector (docs/KB_SOURCE_CONNECTORS.md §5 — forums).

Discourse (the common forum platform) has a real JSON API, so we use it
rather than screen-scraping HTML through `webcrawl.py`: an API gives the
accepted-answer flag, author, and timestamps a generic crawler would have
to guess at from markup.

Extraction mirrors `interpreter/linear.py`'s resolved-issue filter: a
thread with a **marked solution** (the Solved plugin's `accepted_answer`)
is real institutional knowledge — `quality="community_resolved"`; an
unresolved / argumentative thread isn't worth embedding (unless the
connection sets `resolved_only=no`, in which case it's `quality="unverified"`).

Auth: a **public** forum needs none. A gated one takes an `Api-Key` +
`Api-Username` header — the key goes to Supabase Vault (kind='discourse'),
the username sits in the connection config.
"""

from __future__ import annotations

import logging

KIND = "discourse"
log = logging.getLogger("interpreter.discourse")


def _key(tenant_id: str | None, sb, override: str | None = None) -> str | None:
    if override:
        return override
    try:
        from . import vault_secrets
        return (vault_secrets.get(tenant_id, KIND, sb=sb) or {}).get("api_key") or None
    except Exception:  # noqa: BLE001
        return None


def available(tenant_id: str | None, sb) -> "tuple[bool, str | None]":
    # a public forum works with no key; the base_url is per-connection, so
    # there's nothing tenant-wide to gate on.
    return True, None


def _get(tenant_id: str | None, sb, base_url: str, path: str, *,
         api_key: str | None = None, api_username: str | None = None) -> dict:
    import requests

    headers = {"Accept": "application/json"}
    k = _key(tenant_id, sb, api_key)
    if k:
        headers["Api-Key"] = k
        headers["Api-Username"] = api_username or "system"
    r = requests.get(f"{base_url.rstrip('/')}{path}", headers=headers, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Discourse API {r.status_code} on {path}: {r.text[:200]}")
    return r.json()


def test_connection(tenant_id: str | None, sb, base_url: str, *,
                    api_key: str | None = None, api_username: str | None = None) -> dict:
    try:
        j = _get(tenant_id, sb, base_url, "/site.json", api_key=api_key, api_username=api_username)
        return {"ok": True, "detail": f"reached {j.get('title') or base_url}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:300]}


def _topic_is_solved(t: dict) -> bool:
    return bool(t.get("has_accepted_answer") or t.get("accepted_answer"))


def fetch_topics(tenant_id: str | None, sb, base_url: str, *, category: str | None = None,
                 resolved_only: bool = True, limit: int | None = None,
                 api_key: str | None = None, api_username: str | None = None) -> list[dict]:
    """-> [{id, title, url, solved, posts: [{username, cooked/raw, accepted}]}].
    Lists `/latest.json` (or `/c/<category>.json`), then pulls each topic's
    posts from `/t/<id>.json`. `/latest` is a window, not the whole forum —
    the connector marks the result non-exhaustive so nothing is archived."""
    listing_path = f"/c/{category}.json" if category else "/latest.json"
    out: list[dict] = []
    page = 0
    while True:
        j = _get(tenant_id, sb, base_url, f"{listing_path}?page={page}",
                 api_key=api_key, api_username=api_username)
        topics = ((j.get("topic_list") or {}).get("topics")) or []
        if not topics:
            break
        for t in topics:
            solved = _topic_is_solved(t)
            if resolved_only and not solved:
                continue
            try:
                full = _get(tenant_id, sb, base_url, f"/t/{t['id']}.json",
                            api_key=api_key, api_username=api_username)
            except Exception as e:  # noqa: BLE001
                log.warning("discourse topic %s: %s", t.get("id"), e)
                continue
            posts = ((full.get("post_stream") or {}).get("posts")) or []
            out.append({
                "id": t["id"],
                "title": t.get("title") or full.get("title") or f"Topic {t['id']}",
                "url": f"{base_url.rstrip('/')}/t/{t['id']}",
                "solved": solved,
                "posts": [{"username": p.get("username"),
                           "text": p.get("raw") or _strip_html(p.get("cooked") or ""),
                           "accepted": bool(p.get("accepted_answer"))}
                          for p in posts],
            })
            if limit and len(out) >= limit:
                return out[:limit]
        page += 1
        if page > 40:            # hard stop; max_items is the real bound
            break
    return out


def _strip_html(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s or "").strip()
