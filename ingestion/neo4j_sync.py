"""
Phase 1: sync docs.zapier.com graph relations into Neo4j (Aura).

Split of responsibilities:
  - Supabase  -> doc content + pgvector embeddings (scraper.py writes it)
  - Neo4j     -> the relation graph pgvector can't express: which docs sit
                 under the same section, the breadcrumb hierarchy, and
                 (once link capture exists) in-content doc->doc hyperlinks.

Every node is keyed on `url` -- the same primary key as `zapier_docs.url`
in Supabase -- so the two stores stay joinable.

Graph model:
  (:Doc {url, title, status, content_hash, last_changed_at, synced_at})
  (:Section {path})                          -- URL path prefix = a doc "area"
  (:Doc)-[:IN_SECTION]->(:Section)           -- doc's deepest section
  (:Section)-[:SUBSECTION_OF]->(:Section)    -- hierarchy from nested paths
  (:Doc)-[:LINKS_TO]->(:Doc)                 -- in-content links, from doc_links

Run daily via cron, right after scraper.py:
  30 3 * * * /path/to/venv/bin/python neo4j_sync.py >> /var/log/zapier_sync.log 2>&1

Env vars required (put in .env):
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD
  NEO4J_DATABASE  -- optional, defaults to "neo4j"; some Aura instances name
                     the default DB after the instance id instead.
"""

import os
import sys
import logging
from urllib.parse import urlparse

from neo4j import GraphDatabase
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neo4j_sync")

NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "neo4j")
BATCH_SIZE = 500


def get_supabase():
    url = os.environ["SUPABASE_URL"].strip()
    key = os.environ["SUPABASE_SERVICE_KEY"].strip()  # service role -- trusted backend job
    return create_client(url, key)


def get_neo4j_driver():
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
    )


def fetch_docs(sb) -> list[dict]:
    """All docs, including soft-deleted -- Neo4j mirrors the same status flag."""
    rows = (
        sb.table("zapier_docs")
        .select("url, title, status, content_hash, last_changed_at")
        .execute()
        .data
    )
    log.info(f"Fetched {len(rows)} docs from Supabase")
    return rows


def fetch_links(sb) -> list[dict]:
    """Live doc-to-doc links (soft-deleted ones excluded)."""
    rows = (
        sb.table("doc_links")
        .select("source_url, target_url")
        .neq("status", "deleted")
        .execute()
        .data
    )
    log.info(f"Fetched {len(rows)} links from Supabase")
    return rows


def section_hierarchy(url: str) -> tuple[list[str], list[tuple[str, str]], str | None]:
    """
    From a doc URL derive its section structure.

    e.g. https://docs.zapier.com/platform/build/cli-intro
      prefixes = ['platform', 'platform/build']       -- Section nodes
      pairs    = [('platform/build', 'platform')]     -- SUBSECTION_OF edges
      deepest  = 'platform/build'                     -- doc's IN_SECTION target

    Top-level docs (no ancestor path) return ([], [], None).
    """
    path = urlparse(url).path.strip("/")
    segments = [s for s in path.split("/") if s]
    ancestors = segments[:-1]  # last segment is the doc's own slug, not a section
    prefixes = ["/".join(ancestors[: i + 1]) for i in range(len(ancestors))]
    pairs = [(prefixes[i], prefixes[i - 1]) for i in range(1, len(prefixes))]
    deepest = prefixes[-1] if prefixes else None
    return prefixes, pairs, deepest


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def ensure_constraints(driver):
    for cypher in (
        "CREATE CONSTRAINT doc_url IF NOT EXISTS FOR (d:Doc) REQUIRE d.url IS UNIQUE",
        "CREATE CONSTRAINT section_path IF NOT EXISTS FOR (s:Section) REQUIRE s.path IS UNIQUE",
        "CREATE CONSTRAINT module_name IF NOT EXISTS FOR (m:Module) REQUIRE m.name IS UNIQUE",
        # audit NEO-5 (2026-09-03) -- these were single-property (sf_id /
        # case_sf_id / id) uniqueness constraints. Salesforce record ids are
        # per-org, not globally unique, and case_memory.py's MERGEs used to
        # key on sf_id alone (tenant_id only stamped via SET afterward) --
        # two tenants on different orgs sharing an id would silently MERGE
        # into the SAME node, cross-contaminating case history. Both the
        # Cypher (case_memory.py's _MERGE_CYPHER / _LIFECYCLE_CYPHER, now
        # keyed on (sf_id, tenant_id)) and these constraints need to agree,
        # or a real collision would throw a constraint violation instead of
        # silently merging -- fail-loud is strictly better than the old
        # behavior, but the composite constraint is what makes two tenants
        # sharing an id actually WORK instead of erroring on every sync.
        "DROP CONSTRAINT case_sf_id IF EXISTS",
        "CREATE CONSTRAINT case_sf_id_tenant IF NOT EXISTS "
        "FOR (c:Case) REQUIRE (c.sf_id, c.tenant_id) IS UNIQUE",
        "DROP CONSTRAINT reply_case_sf_id IF EXISTS",
        "CREATE CONSTRAINT reply_case_sf_id_tenant IF NOT EXISTS "
        "FOR (r:Reply) REQUIRE (r.case_sf_id, r.tenant_id) IS UNIQUE",
        "DROP CONSTRAINT message_id IF EXISTS",
        "CREATE CONSTRAINT message_id_tenant IF NOT EXISTS "
        "FOR (mm:Message) REQUIRE (mm.id, mm.tenant_id) IS UNIQUE",
        "DROP CONSTRAINT account_sf_id IF EXISTS",
        "CREATE CONSTRAINT account_sf_id_tenant IF NOT EXISTS "
        "FOR (a:Account) REQUIRE (a.sf_id, a.tenant_id) IS UNIQUE",
    ):
        try:
            driver.execute_query(cypher, database_=NEO4J_DATABASE)
        except Exception as e:  # noqa: BLE001 — an older Aura / existing dupes
            log.warning("constraint skipped (%s): %s", cypher.split()[2], e)
    log.info("Constraints ensured")


def sync_doc_nodes(driver, docs: list[dict]):
    cypher = """
    UNWIND $rows AS row
    MERGE (d:Doc {url: row.url})
    SET d.title = row.title,
        d.status = row.status,
        d.content_hash = row.content_hash,
        d.last_changed_at = row.last_changed_at,
        d.synced_at = datetime()
    """
    for batch in chunked(docs, BATCH_SIZE):
        driver.execute_query(cypher, rows=batch, database_=NEO4J_DATABASE)
    log.info(f"Upserted {len(docs)} Doc nodes")


def sync_sections(driver, docs: list[dict]):
    section_paths: set[str] = set()
    subsection_pairs: set[tuple[str, str]] = set()
    in_section: list[dict] = []

    for doc in docs:
        prefixes, pairs, deepest = section_hierarchy(doc["url"])
        section_paths.update(prefixes)
        subsection_pairs.update(pairs)
        if deepest is not None:
            in_section.append({"url": doc["url"], "path": deepest})

    for batch in chunked(sorted(section_paths), BATCH_SIZE):
        driver.execute_query(
            "UNWIND $paths AS p MERGE (:Section {path: p})",
            paths=batch,
            database_=NEO4J_DATABASE,
        )

    pair_rows = [{"child": c, "parent": p} for c, p in subsection_pairs]
    for batch in chunked(pair_rows, BATCH_SIZE):
        driver.execute_query(
            """
            UNWIND $rows AS row
            MATCH (c:Section {path: row.child})
            MATCH (p:Section {path: row.parent})
            MERGE (c)-[:SUBSECTION_OF]->(p)
            """,
            rows=batch,
            database_=NEO4J_DATABASE,
        )

    # Rebuild IN_SECTION wholesale each run -- cheap, and keeps it correct if a
    # doc's URL (and therefore its section) changes between runs.
    driver.execute_query(
        "MATCH (:Doc)-[r:IN_SECTION]->(:Section) DELETE r", database_=NEO4J_DATABASE
    )
    for batch in chunked(in_section, BATCH_SIZE):
        driver.execute_query(
            """
            UNWIND $rows AS row
            MATCH (d:Doc {url: row.url})
            MATCH (s:Section {path: row.path})
            MERGE (d)-[:IN_SECTION]->(s)
            """,
            rows=batch,
            database_=NEO4J_DATABASE,
        )
    log.info(
        f"Synced {len(section_paths)} Section nodes, "
        f"{len(subsection_pairs)} SUBSECTION_OF edges, {len(in_section)} IN_SECTION edges"
    )


def sync_links(driver, links: list[dict]):
    """Rebuild (:Doc)-[:LINKS_TO]->(:Doc) from doc_links. Targets that aren't
    ingested yet become stub Doc nodes (they get filled in on a later run)."""
    driver.execute_query(
        "MATCH (:Doc)-[r:LINKS_TO]->(:Doc) DELETE r", database_=NEO4J_DATABASE
    )
    rows = [{"src": l["source_url"], "tgt": l["target_url"]} for l in links]
    for batch in chunked(rows, BATCH_SIZE):
        driver.execute_query(
            """
            UNWIND $rows AS row
            MATCH (s:Doc {url: row.src})
            MERGE (t:Doc {url: row.tgt})
            MERGE (s)-[:LINKS_TO]->(t)
            """,
            rows=batch,
            database_=NEO4J_DATABASE,
        )
    log.info(f"Synced {len(rows)} LINKS_TO edges")


def run():
    sb = get_supabase()
    docs = fetch_docs(sb)
    if not docs:
        log.warning("No docs in Supabase -- run scraper.py first. Nothing to sync.")
        return
    links = fetch_links(sb)

    driver = get_neo4j_driver()
    try:
        driver.verify_connectivity()
        ensure_constraints(driver)
        sync_doc_nodes(driver, docs)
        sync_sections(driver, docs)
        sync_links(driver, links)
    finally:
        driver.close()
    log.info("Neo4j sync complete.")
    try:  # audit NEO-2 — a heartbeat health_check can watch for staleness
        from interpreter.health import beat
        beat("neo4j", {"docs": len(docs), "links": len(links)}, sb=sb, force=True)
    except Exception as e:  # noqa: BLE001
        log.warning("could not beat neo4j health: %s", e)


if __name__ == "__main__":
    try:
        run()
    except KeyError as e:
        log.error(f"Missing required env var: {e}. Check your .env file.")
        sys.exit(1)
