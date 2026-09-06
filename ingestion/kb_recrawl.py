"""
Scheduled refresh for every active KB source connection.

Each connected feed (a crawl root, a Google Sheet/Doc, later Linear/Discourse/
Nolt) is a row in `kb_source_connections` (migration 091). This script is the
scheduled path: it reads every `status='active'` connection and enqueues one
`kb_sync` job per connection — same as `daily-sync.yml` already does for
`case_memory_sync` / `case_graph_sync`. Cheap on a re-run: the generic
`_sync_kb_connection` driver skips re-embedding anything unchanged, and this
script's per-connection `dedupe_key` means a run that fires while a previous
run's jobs are still queued is a no-op, not a pile-up.

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


def _active_connections(sb) -> list[dict]:
    return (sb.table("kb_source_connections")
            .select("connection_id, source_id, connector, label")
            .eq("status", "active").execute().data or [])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.kb_recrawl")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    sb = get_supabase()
    conns = _active_connections(sb)
    log.info("%d active KB source connection(s) to refresh", len(conns))

    queued = deduped = 0
    for cn in conns:
        cid = cn["connection_id"]
        if args.dry_run:
            log.info("[dry-run] would re-sync %s connection %s (%s)",
                     cn["connector"], cid, cn.get("label"))
            continue
        job_id = jobs.enqueue("kb_sync", {"connection_id": cid},
                              dedupe_key=f"kb_sync:{cid}", sb=sb)
        queued, deduped = (queued + 1, deduped) if job_id else (queued, deduped + 1)

    log.info("%s: queued=%d deduped=%d",
             "would queue" if args.dry_run else "queued",
             len(conns) if args.dry_run else queued, deduped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
