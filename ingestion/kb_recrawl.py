"""
Scheduled re-crawl for every KB collection with a known crawl history.

`api/worker.py::_crawl_site` now records each crawled URL onto its source's
`config.crawl_urls` (2026-09-05) — this script is the belt-and-braces
scheduled path that reads that list back and re-enqueues a `crawl_site` job
per (source, url), same as `daily-sync.yml` already does for
`case_memory_sync`/`case_graph_sync`. Cheap on a re-run: `_crawl_site`
itself skips re-embedding any page whose markdown didn't change, and this
script's own job dedupe_key means a run that fires while a previous run's
jobs are still queued is a no-op, not a pile-up.

    python -m ingestion.kb_recrawl
    python -m ingestion.kb_recrawl --dry-run
"""

from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import jobs  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingestion.kb_recrawl")


def _crawl_targets(sb) -> list[dict]:
    """One row per (source, url) with a known crawl history, active
    sources only -- an archived/deleted collection is never re-crawled."""
    rows = (sb.table("sources").select("source_id, tenant_id, name, config")
            .eq("status", "active").execute().data or [])
    out = []
    for r in rows:
        for url in (r.get("config") or {}).get("crawl_urls") or []:
            out.append({"source_id": r["source_id"], "tenant_id": r["tenant_id"],
                       "collection_name": r.get("name", ""), "url": url})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.kb_recrawl")
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    sb = get_supabase()
    targets = _crawl_targets(sb)
    log.info("%d (source, url) pair(s) to re-crawl", len(targets))

    queued = deduped = 0
    for t in targets:
        if args.dry_run:
            log.info("[dry-run] would re-crawl %s (%s)", t["url"], t["collection_name"])
            continue
        job_id = jobs.enqueue("crawl_site", {
            "source_id": t["source_id"], "tenant_id": t["tenant_id"],
            "collection_name": t["collection_name"], "url": t["url"],
            "max_pages": args.max_pages,
        }, dedupe_key=f"crawl:{t['source_id']}:{t['url']}", sb=sb)
        if job_id:
            queued += 1
        else:
            deduped += 1
    log.info("%s: queued=%d deduped=%d", "would queue" if args.dry_run else "queued",
             queued if not args.dry_run else len(targets), deduped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
