"""
Scheduled refresh for every KB collection with a known crawl/sheet history.

`api/worker.py::_crawl_site` records each crawled URL onto its source's
`config.crawl_urls` (2026-09-05), and `_sync_gsheet` does the same for a
linked sheet's `config.gsheets` — this script is the belt-and-braces
scheduled path that reads both back and re-enqueues one job per known
target, same as `daily-sync.yml` already does for
`case_memory_sync`/`case_graph_sync`. Cheap on a re-run: both handlers
skip re-embedding anything unchanged, and this script's own job
dedupe_keys mean a run that fires while a previous run's jobs are still
queued is a no-op, not a pile-up.

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


def _active_sources(sb) -> list[dict]:
    return (sb.table("sources").select("source_id, tenant_id, name, config")
            .eq("status", "active").execute().data or [])


def _crawl_targets(sb) -> list[dict]:
    """One row per (source, url) with a known crawl history."""
    out = []
    for r in _active_sources(sb):
        for url in (r.get("config") or {}).get("crawl_urls") or []:
            out.append({"source_id": r["source_id"], "tenant_id": r["tenant_id"],
                       "collection_name": r.get("name", ""), "url": url})
    return out


def _gsheet_targets(sb) -> list[dict]:
    """One row per (source, sheet_id, sheet_name) with a known sync history."""
    out = []
    for r in _active_sources(sb):
        for g in (r.get("config") or {}).get("gsheets") or []:
            out.append({"source_id": r["source_id"], "tenant_id": r["tenant_id"],
                       "collection_name": r.get("name", ""),
                       "sheet_id": g.get("sheet_id"), "sheet_name": g.get("sheet_name")})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.kb_recrawl")
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    sb = get_supabase()
    crawl_targets = _crawl_targets(sb)
    sheet_targets = _gsheet_targets(sb)
    log.info("%d crawl url(s), %d sheet(s) to refresh", len(crawl_targets), len(sheet_targets))

    queued = deduped = 0
    for t in crawl_targets:
        if args.dry_run:
            log.info("[dry-run] would re-crawl %s (%s)", t["url"], t["collection_name"])
            continue
        job_id = jobs.enqueue("crawl_site", {
            "source_id": t["source_id"], "tenant_id": t["tenant_id"],
            "collection_name": t["collection_name"], "url": t["url"],
            "max_pages": args.max_pages,
        }, dedupe_key=f"crawl:{t['source_id']}:{t['url']}", sb=sb)
        queued, deduped = (queued + 1, deduped) if job_id else (queued, deduped + 1)

    for t in sheet_targets:
        if args.dry_run:
            log.info("[dry-run] would re-sync sheet %s (%s)", t["sheet_id"], t["collection_name"])
            continue
        job_id = jobs.enqueue("sync_gsheet", {
            "source_id": t["source_id"], "tenant_id": t["tenant_id"],
            "collection_name": t["collection_name"], "sheet_id": t["sheet_id"],
            "sheet_name": t["sheet_name"],
        }, dedupe_key=f"gsheet:{t['source_id']}:{t['sheet_id']}:{t['sheet_name'] or ''}", sb=sb)
        queued, deduped = (queued + 1, deduped) if job_id else (queued, deduped + 1)

    total = len(crawl_targets) + len(sheet_targets)
    log.info("%s: queued=%d deduped=%d", "would queue" if args.dry_run else "queued",
             queued if not args.dry_run else total, deduped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
