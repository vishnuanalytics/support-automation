"""
PostHog product-analytics connector (docs/PRODUCT_ANALYTICS_CONNECTOR.md).

The tenant's PostHog stays the system of record for raw product events;
this connector only pulls **per-person activity rollups** (last-seen,
30-day event/active-day counts, a 30d-vs-prior usage trend) plus counts
for the tenant's *configured* milestone events. `product_analytics_sync`
(chunk 4) MERGEs those onto `(:Contact {email, tenant_id})` in Neo4j.

Auth: a PostHog **personal API key** (`phx_…`), read-only where the
tenant can scope it (`query:read`, `person:read`). The key goes to
Supabase Vault (kind='posthog'); the host + project id + milestone list
sit in `tenant_integrations.config`.

One HogQL query per rollup kind against `POST /api/projects/:id/query/`.
Everything is best-effort — no key / PostHog down / a bad query returns
`[]` (or a `{ok: False}` from `test_connection`), never an exception into
the sync loop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

KIND = "posthog"
log = logging.getLogger("interpreter.posthog")

_DEFAULT_HOST = "https://us.posthog.com"
_MAX_PERSONS = 5000
# a milestone event name we're willing to inline into HogQL — PostHog event
# names are dotted / spaced / $-prefixed identifiers, nothing exotic.
_EVENT_NAME_RE = re.compile(r"^[\w.$ :/-]{1,80}$")


# ── config ───────────────────────────────────────────────────────────
@dataclass
class PostHogConfig:
    tenant_id: str
    host: str = _DEFAULT_HOST
    project_id: str = ""
    milestone_events: list[str] = field(default_factory=list)
    status: str = "inactive"
    has_credentials: bool = False

    def public_status(self) -> dict:
        return {"configured": True, "status": self.status,
                "host": self.host, "project_id": self.project_id,
                "milestone_events": self.milestone_events,
                "has_credentials": self.has_credentials}


def _clean_host(h: str | None) -> str:
    h = (h or "").strip().rstrip("/") or _DEFAULT_HOST
    if not h.startswith(("http://", "https://")):
        h = "https://" + h
    return h


def _clean_milestones(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",")]
    out: list[str] = []
    for name in (raw or []):
        name = str(name).strip()
        if name and _EVENT_NAME_RE.match(name) and name not in out:
            out.append(name)
    return out[:40]


def load(tenant_id: str | None, sb) -> PostHogConfig | None:
    if not tenant_id:
        return None
    rows = (sb.table("tenant_integrations").select("config, status, vault_secret_id")
            .eq("tenant_id", tenant_id).eq("kind", KIND).limit(1).execute().data or [])
    if not rows:
        return None
    cfg = rows[0].get("config") or {}
    return PostHogConfig(
        tenant_id=str(tenant_id),
        host=_clean_host(cfg.get("host")),
        project_id=str(cfg.get("project_id") or ""),
        milestone_events=_clean_milestones(cfg.get("milestone_events")),
        status=rows[0].get("status") or "inactive",
        has_credentials=bool(rows[0].get("vault_secret_id")) or bool(_key(tenant_id, sb)),
    )


def save(cfg: PostHogConfig, sb, *, api_key: str | None = None) -> None:
    from . import vault_secrets

    vault_id = None
    if api_key:
        vault_id = vault_secrets.put(cfg.tenant_id, KIND, {"api_key": api_key.strip()}, sb=sb)
    row: dict[str, Any] = {
        "tenant_id": cfg.tenant_id, "kind": KIND, "org_label": "default",
        "secret": {},
        "config": {"host": cfg.host, "project_id": cfg.project_id,
                   "milestone_events": cfg.milestone_events},
        "status": cfg.status or "active",
        "updated_at": "now()",
    }
    if vault_id:
        row["vault_secret_id"] = vault_id
    sb.table("tenant_integrations").upsert(row, on_conflict="tenant_id,kind,org_label").execute()


def delete(tenant_id: str, sb) -> None:
    from . import vault_secrets
    vault_secrets.delete(tenant_id, KIND, sb=sb)
    sb.table("tenant_integrations").delete().eq("tenant_id", tenant_id).eq("kind", KIND).execute()


def _key(tenant_id: str | None, sb, override: str | None = None) -> str | None:
    if override:
        return override.strip()
    try:
        from . import vault_secrets
        return (vault_secrets.get(tenant_id, KIND, sb=sb) or {}).get("api_key") or None
    except Exception:  # noqa: BLE001
        return None


def available(tenant_id: str | None, sb) -> "tuple[bool, str | None]":
    cfg = load(tenant_id, sb)
    if not cfg:
        return False, "PostHog is not connected for this workspace"
    if not cfg.project_id:
        return False, "PostHog project id is not set"
    if not _key(tenant_id, sb):
        return False, "PostHog API key is not stored"
    return True, None


# ── HogQL ────────────────────────────────────────────────────────────
def _query(tenant_id: str | None, sb, hogql: str, *, host: str, project_id: str,
           api_key: str | None = None, timeout: int = 45) -> list[dict]:
    """Run one HogQL query -> list[dict] (columns zipped onto each row)."""
    import requests

    k = _key(tenant_id, sb, api_key)
    if not k:
        raise RuntimeError("no PostHog API key")
    url = f"{_clean_host(host)}/api/projects/{project_id}/query/"
    r = requests.post(url, timeout=timeout,
                      headers={"Authorization": f"Bearer {k}",
                               "Content-Type": "application/json"},
                      json={"query": {"kind": "HogQLQuery", "query": hogql}})
    if r.status_code >= 300:
        raise RuntimeError(f"PostHog query {r.status_code}: {r.text[:200]}")
    body = r.json()
    cols = body.get("columns") or []
    return [dict(zip(cols, row)) for row in (body.get("results") or [])]


def test_connection(tenant_id: str | None, sb, *, host: str | None = None,
                    project_id: str | None = None, api_key: str | None = None) -> dict:
    host = _clean_host(host)
    project_id = str(project_id or "")
    if not project_id:
        return {"ok": False, "detail": "project id is required"}
    try:
        rows = _query(tenant_id, sb, "SELECT 1 AS ok", host=host,
                      project_id=project_id, api_key=api_key, timeout=20)
        ok = bool(rows and (rows[0].get("ok") == 1 or list(rows[0].values())[0] == 1))
        return {"ok": ok, "detail": f"reached project {project_id}" if ok
                else "query returned no rows"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:300]}


# ── rollups ──────────────────────────────────────────────────────────
@dataclass
class PersonRollup:
    email: str
    last_seen_at: str | None = None
    events_30d: int = 0
    active_days_30d: int = 0
    usage_trend: str = "flat"          # 'down' | 'flat' | 'up'
    milestones: list[dict] = field(default_factory=list)  # [{name, last_ts, count}]


def _trend(recent: float, prior: float) -> str:
    if prior <= 0:
        return "up" if recent > 0 else "flat"
    if recent < prior * 0.7:
        return "down"
    if recent > prior * 1.3:
        return "up"
    return "flat"


def _s(v: Any) -> str | None:
    if v in (None, ""):
        return None
    return v.isoformat() if isinstance(v, datetime) else str(v)


def fetch_person_rollups(tenant_id: str | None, sb, *, since: str | None = None,
                         limit: int | None = None, api_key: str | None = None
                         ) -> list[PersonRollup]:
    """One rollup per identified person (has a non-empty `email` property),
    seen in the last 30 days. `since` (ISO) drops persons whose most recent
    activity predates it — the rollup windows themselves stay fixed at
    30/60/90 days. Best-effort: any failure -> []."""
    cfg = load(tenant_id, sb)
    if not cfg or not cfg.project_id:
        return []
    cap = max(1, min(_MAX_PERSONS, limit or _MAX_PERSONS))
    q = dict(host=cfg.host, project_id=cfg.project_id, api_key=api_key)

    try:
        base = _query(tenant_id, sb, f"""
            SELECT person.properties.email AS email,
                   max(timestamp) AS last_seen_at,
                   count() AS events_30d,
                   count(DISTINCT toStartOfDay(timestamp)) AS active_days_30d
            FROM events
            WHERE timestamp > now() - INTERVAL 30 DAY
              AND notEmpty(person.properties.email)
            GROUP BY email
            ORDER BY last_seen_at DESC
            LIMIT {cap}
        """, **q)
    except Exception as e:  # noqa: BLE001
        log.warning("posthog rollup base query: %s", e)
        return []

    rollups: dict[str, PersonRollup] = {}
    for row in base:
        email = (row.get("email") or "").strip().lower()
        if not email:
            continue
        last = _s(row.get("last_seen_at"))
        if since and last and last < since:
            continue
        rollups[email] = PersonRollup(
            email=email, last_seen_at=last,
            events_30d=int(row.get("events_30d") or 0),
            active_days_30d=int(row.get("active_days_30d") or 0))
    if not rollups:
        return []

    try:
        for row in _query(tenant_id, sb, """
            SELECT person.properties.email AS email,
                   countIf(timestamp > now() - INTERVAL 30 DAY) AS recent,
                   countIf(timestamp <= now() - INTERVAL 30 DAY) AS prior
            FROM events
            WHERE timestamp > now() - INTERVAL 60 DAY
              AND notEmpty(person.properties.email)
            GROUP BY email
        """, **q):
            email = (row.get("email") or "").strip().lower()
            if email in rollups:
                rollups[email].usage_trend = _trend(
                    float(row.get("recent") or 0), float(row.get("prior") or 0))
    except Exception as e:  # noqa: BLE001
        log.warning("posthog trend query: %s", e)

    if cfg.milestone_events:
        inlist = ", ".join("'" + n.replace("'", "") + "'" for n in cfg.milestone_events)
        try:
            for row in _query(tenant_id, sb, f"""
                SELECT person.properties.email AS email, event AS name,
                       max(timestamp) AS last_ts, count() AS cnt
                FROM events
                WHERE timestamp > now() - INTERVAL 90 DAY
                  AND event IN ({inlist})
                  AND notEmpty(person.properties.email)
                GROUP BY email, event
            """, **q):
                email = (row.get("email") or "").strip().lower()
                if email in rollups:
                    rollups[email].milestones.append({
                        "name": row.get("name"),
                        "last_ts": _s(row.get("last_ts")),
                        "count": int(row.get("cnt") or 0)})
        except Exception as e:  # noqa: BLE001
            log.warning("posthog milestone query: %s", e)

    return list(rollups.values())


def sync_watermark() -> str:
    """The `since` a caller should pass on the next incremental run — now,
    minus a small overlap so a person active right at the boundary isn't
    missed. (The rollup windows are fixed; this only bounds MERGE volume.)"""
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
