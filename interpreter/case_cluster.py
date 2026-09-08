"""
Chunk-2b — cluster resolved cases into `(:Issue)` nodes so
"what's linked to the same underlying bug as case #1423" is answerable.

The clustering is deterministic and LLM-free: `case_extract` (chunk 2)
already normalised each case's root cause into a `(:RootCause {label})`
node, so cases that share a RootCause label *are* instances of one Issue.
This pass just materialises that:

    (:RootCause)<-[:CAUSED_BY]-(:Case)   -- N cases, one label
        =>  MERGE (:Issue {issue_key, tenant_id, title, status:'auto'})
            (:Issue)-[:ROOT_CAUSE]->(:RootCause)
            (:Case)-[:INSTANCE_OF]->(:Issue)   -- for each

`issue_key` is `auto:<sha1(tenant:label)[:16]>` — stable across runs, so
re-running only updates counts. Auto Issues carry `status='auto'`; a human
promoting / renaming / merging them is a later chunk (they drive nothing
automated, they're a query grouping).

    python -m interpreter.case_cluster --tenant <uuid>
    python -m interpreter.case_cluster --tenant <uuid> --min-cases 2
    python -m interpreter.case_cluster --all          # every tenant with case data
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from interpreter import case_memory  # noqa: E402

log = logging.getLogger("interpreter.case_cluster")

# Distinct root-cause labels for a tenant — used to mint stable issue_keys
# (Community-edition Cypher has no md5/sha, so the hash is computed here).
_LABELS_CYPHER = (
    "MATCH (rc:RootCause {tenant_id: $tenant_id})<-[:CAUSED_BY]-"
    "(:Case {tenant_id: $tenant_id}) RETURN DISTINCT rc.label AS label")

# `pairs` = [{label, issue_key}] — one MERGE call, no dynamic map access.
_CLUSTER_CYPHER = """
UNWIND $pairs AS pair
MATCH (rc:RootCause {tenant_id: $tenant_id, label: pair.label})
      <-[:CAUSED_BY]-(c:Case {tenant_id: $tenant_id})
WITH pair, rc, collect(DISTINCT c) AS cases, count(DISTINCT c) AS n
WHERE n >= $min_cases
MERGE (iss:Issue {issue_key: pair.issue_key, tenant_id: $tenant_id})
  ON CREATE SET iss.title = rc.label, iss.status = 'auto', iss.created_at = $now
SET iss.case_count = n, iss.updated_at = $now
MERGE (iss)-[:ROOT_CAUSE]->(rc)
WITH iss, cases
UNWIND cases AS c
  MERGE (c)-[:INSTANCE_OF]->(iss)
RETURN count(DISTINCT iss) AS issues, count(*) AS links
"""


def issue_key(tenant_id: str, label: str) -> str:
    h = hashlib.sha1(f"{tenant_id}:{label}".encode("utf-8")).hexdigest()[:16]
    return f"auto:{h}"


def cluster_issues(tenant_id: str, *, min_cases: int = 3) -> dict:
    """MERGE one `(:Issue)` per RootCause label with >= `min_cases` cases.
    Returns `{issues, links}` (0/0 when the graph is unreachable)."""
    driver = case_memory._driver_or_none()
    if driver is None:
        return {"issues": 0, "links": 0}
    db = case_memory.graph_database(str(tenant_id))
    now = datetime.now(timezone.utc).isoformat()
    try:
        labels = driver.execute_query(
            _LABELS_CYPHER, tenant_id=str(tenant_id), database_=db).records
        pairs = [{"label": r["label"], "issue_key": issue_key(str(tenant_id), r["label"])}
                 for r in labels if r["label"]]
        if not pairs:
            return {"issues": 0, "links": 0}
        rec = driver.execute_query(
            _CLUSTER_CYPHER, tenant_id=str(tenant_id), min_cases=int(min_cases),
            pairs=pairs, now=now, database_=db).records
        row = rec[0] if rec else {}
        return {"issues": int(row.get("issues") or 0), "links": int(row.get("links") or 0)}
    except Exception as e:  # noqa: BLE001
        log.warning("cluster_issues(%s): %s", tenant_id, e)
        return {"issues": 0, "links": 0}


def _all_tenants(sb) -> list[str]:
    try:
        rows = sb.table("case_memory").select("tenant_id").execute().data or []
        return sorted({r["tenant_id"] for r in rows if r.get("tenant_id")})
    except Exception as e:  # noqa: BLE001
        log.warning("tenant list: %s", e)
        return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="interpreter.case_cluster")
    ap.add_argument("--tenant", help="tenant_id (uuid)")
    ap.add_argument("--all", action="store_true", help="every tenant with case data")
    ap.add_argument("--min-cases", type=int, default=3,
                    help="a RootCause needs at least this many cases to become an Issue")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not args.tenant and not args.all:
        ap.error("pass --tenant <uuid> or --all")

    tenants = [args.tenant]
    if args.all:
        from ingestion.scraper import get_supabase
        tenants = _all_tenants(get_supabase())

    total = {"issues": 0, "links": 0}
    for tid in tenants:
        r = cluster_issues(tid, min_cases=args.min_cases)
        log.info("tenant %s: %d issue(s), %d case link(s)", tid, r["issues"], r["links"])
        total["issues"] += r["issues"]
        total["links"] += r["links"]
    log.info("done: %d issue(s), %d link(s) across %d tenant(s)",
             total["issues"], total["links"], len(tenants))
    return 0


if __name__ == "__main__":
    sys.exit(main())
