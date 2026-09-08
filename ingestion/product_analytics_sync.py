"""
Phase 30 chunk 4 — pull PostHog person rollups into Neo4j and resolve
identity (docs/PRODUCT_ANALYTICS_CONNECTOR.md).

    python -m ingestion.product_analytics_sync --once
    python -m ingestion.product_analytics_sync --once --dry-run
    python -m ingestion.product_analytics_sync --tenant <uuid>

For every tenant with an **active** `posthog` integration:

  1. `posthog.fetch_person_rollups(since=<watermark>)`.
  2. MERGE `(:Contact {email, tenant_id})` with the rollup props +
     `(:Contact)-[:DID {last_ts,count}]->(:Feature {name, tenant_id})` for
     each configured milestone.
  3. Identity resolution per contact:
       - `email`  — the Contact already has an inbound `[:FILED_BY]` from a
                    Case (Salesforce knows this person).
       - `domain` — no `[:FILED_BY]`, but the email domain matches **exactly
                    one** `(:Account {domain})`; MERGE `[:AT_ACCOUNT]`.
       - `none`   — neither. Rollups still exist; no account edge.
  4. One account-rollup pass: each `(:Account)` gets `pa_active_users_30d`
     / `pa_contacts` / `pa_events_30d` / `pa_usage_trend` from its
     `[:AT_ACCOUNT]` contacts.
  5. `graph_sync_state` (`scope = 'product_analytics:<tenant>'`) — advance
     the `last_modified` (= `last_seen_at`) high-water mark and record
     `contacts_synced` + `coverage_pct` (identity-match rate this run).

Best-effort throughout: no PostHog / no Neo4j / a bad query for one tenant
is logged and skipped, never fatal.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("product_analytics_sync")

_MAX_PERSONS = int(os.environ.get("POSTHOG_MAX_PERSONS", "5000"))

_ROLLUP_CYPHER = """
UNWIND $rollups AS r
MERGE (ct:Contact {email: r.email, tenant_id: $tenant_id})
  SET ct.last_seen_at   = r.last_seen_at,
      ct.events_30d      = r.events_30d,
      ct.active_days_30d = r.active_days_30d,
      ct.usage_trend     = r.usage_trend,
      ct.pa_synced_at    = $synced_at
WITH ct, r
UNWIND (CASE WHEN r.milestones = [] THEN [null] ELSE r.milestones END) AS ms
  FOREACH (_ IN CASE WHEN ms IS NULL THEN [] ELSE [1] END |
    MERGE (f:Feature {name: ms.name, tenant_id: $tenant_id})
    MERGE (ct)-[d:DID]->(f)
      SET d.last_ts = ms.last_ts, d.count = ms.count)
"""

# identity resolution — one row per email just synced. `acct` is bound in a
# WITH (not indexed inline) so it can be a node variable in the MERGE.
_IDENTITY_CYPHER = """
UNWIND $emails AS em
MATCH (ct:Contact {email: em, tenant_id: $tenant_id})
OPTIONAL MATCH (ct)<-[:FILED_BY]-(fc:Case)
WITH ct, count(fc) AS filed, split(em, '@')[1] AS dom
OPTIONAL MATCH (a:Account {tenant_id: $tenant_id, domain: dom})
WITH ct, filed, collect(a) AS accts
WITH ct, filed,
     CASE WHEN filed = 0 AND size(accts) = 1 THEN accts[0] ELSE null END AS acct
SET ct.identity_match =
  CASE WHEN filed > 0 THEN 'email'
       WHEN acct IS NOT NULL THEN 'domain'
       ELSE 'none' END
FOREACH (_ IN CASE WHEN acct IS NULL THEN [] ELSE [1] END |
  MERGE (ct)-[:AT_ACCOUNT]->(acct))
RETURN ct.identity_match AS m
"""

_ACCOUNT_ROLLUP_CYPHER = """
MATCH (a:Account {tenant_id: $tenant_id})<-[:AT_ACCOUNT]-(ct:Contact)
WHERE ct.pa_synced_at IS NOT NULL
WITH a,
     count(ct) AS contacts,
     sum(CASE WHEN coalesce(ct.events_30d, 0) > 0 THEN 1 ELSE 0 END) AS active_users,
     sum(coalesce(ct.events_30d, 0)) AS events_30d,
     collect(coalesce(ct.usage_trend, 'flat')) AS trends
SET a.pa_contacts          = contacts,
    a.pa_active_users_30d   = active_users,
    a.pa_events_30d         = events_30d,
    a.pa_usage_trend        =
      CASE WHEN size([t IN trends WHERE t = 'down']) * 2 >= size(trends) THEN 'down'
           WHEN size([t IN trends WHERE t = 'up'])   * 2 >= size(trends) THEN 'up'
           ELSE 'flat' END,
    a.pa_synced_at          = $synced_at
RETURN count(a) AS accounts
"""


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def _driver():
    try:
        from ingestion.neo4j_sync import get_neo4j_driver
        return get_neo4j_driver()
    except Exception as e:  # noqa: BLE001
        log.warning("neo4j driver unavailable: %s", e)
        return None


def _active_posthog_tenants(sb, only: str | None) -> list[str]:
    try:
        q = (sb.table("tenant_integrations").select("tenant_id")
             .eq("kind", "posthog").eq("status", "active"))
        rows = q.execute().data or []
    except Exception as e:  # noqa: BLE001
        log.warning("tenant_integrations read: %s", e)
        return []
    tids = [r["tenant_id"] for r in rows if r.get("tenant_id")]
    return [t for t in tids if (only is None or t == only)]


def _load_watermark(sb, scope: str) -> str | None:
    try:
        rows = (sb.table("graph_sync_state").select("last_modified, contacts_synced")
                .eq("scope", scope).limit(1).execute().data or [])
        return rows[0].get("last_modified") if rows else None
    except Exception as e:  # noqa: BLE001
        log.warning("graph_sync_state read: %s", e)
        return None


def _save_state(sb, scope: str, tenant_id: str, *, high_water: str | None,
                contacts: int, coverage_pct: float | None) -> None:
    try:
        prev = (sb.table("graph_sync_state").select("contacts_synced")
                .eq("scope", scope).limit(1).execute().data or [{}])
        now = datetime.now(timezone.utc).isoformat()
        sb.table("graph_sync_state").upsert({
            "scope": scope,
            "tenant_id": tenant_id,
            "last_modified": high_water,
            "contacts_synced": (prev[0].get("contacts_synced") or 0) + contacts,
            "coverage_pct": coverage_pct,
            "last_run_at": now,
            "updated_at": now,
        }, on_conflict="scope").execute()
    except Exception as e:  # noqa: BLE001
        log.warning("graph_sync_state write: %s", e)


def _sync_one(sb, driver, tenant_id: str, *, dry: bool) -> dict:
    from interpreter import posthog

    scope = f"product_analytics:{tenant_id}"
    since = _load_watermark(sb, scope)
    rollups = posthog.fetch_person_rollups(tenant_id, sb, since=since, limit=_MAX_PERSONS)
    if not rollups:
        return {"tenant_id": tenant_id, "contacts": 0, "note": "no rollups"}

    payload = [{
        "email": r.email,
        "last_seen_at": r.last_seen_at,
        "events_30d": r.events_30d,
        "active_days_30d": r.active_days_30d,
        "usage_trend": r.usage_trend,
        "milestones": r.milestones or [],
    } for r in rollups]
    emails = [r["email"] for r in payload]
    high_water = max((r["last_seen_at"] for r in payload if r["last_seen_at"]), default=since)

    if dry:
        return {"tenant_id": tenant_id, "contacts": len(payload), "dry_run": True,
                "high_water": high_water}
    if driver is None:
        return {"tenant_id": tenant_id, "contacts": 0, "note": "no neo4j"}

    from interpreter.case_memory import graph_database
    db = graph_database(str(tenant_id))
    synced_at = datetime.now(timezone.utc).isoformat()
    driver.execute_query(_ROLLUP_CYPHER, rollups=payload, tenant_id=tenant_id,
                         synced_at=synced_at, database_=db)
    rec, _s, _k = driver.execute_query(_IDENTITY_CYPHER, emails=emails,
                                       tenant_id=tenant_id, database_=db)
    matches = [row["m"] for row in rec]
    matched = sum(1 for m in matches if m and m != "none")
    coverage_pct = round(100.0 * matched / len(matches), 1) if matches else None
    driver.execute_query(_ACCOUNT_ROLLUP_CYPHER, tenant_id=tenant_id,
                         synced_at=synced_at, database_=db)

    _save_state(sb, scope, tenant_id, high_water=high_water,
                contacts=len(payload), coverage_pct=coverage_pct)
    return {"tenant_id": tenant_id, "contacts": len(payload),
            "coverage_pct": coverage_pct,
            "by_match": {k: matches.count(k) for k in set(matches)}}


def sync(*, tenant_id: str | None = None, dry: bool = False) -> list[dict]:
    sb = _sb()
    tenants = _active_posthog_tenants(sb, tenant_id)
    if not tenants:
        log.info("no active posthog integrations — nothing to sync")
        return []
    driver = None if dry else _driver()
    out: list[dict] = []
    for tid in tenants:
        try:
            res = _sync_one(sb, driver, tid, dry=dry)
        except Exception as e:  # noqa: BLE001
            log.warning("product_analytics_sync(%s): %s", tid, e)
            res = {"tenant_id": tid, "error": str(e)[:200]}
        log.info("product_analytics_sync %s", res)
        out.append(res)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.product_analytics_sync")
    ap.add_argument("--once", action="store_true", help="run one pass and exit")
    ap.add_argument("--tenant", help="only this tenant id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    sync(tenant_id=args.tenant, dry=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
