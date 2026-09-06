"""
Close the loop on automated KB write-backs to Google Docs
(docs/KB_SOURCE_CONNECTORS.md §2, chunk 2).

Each `gdoc_writeback` job (api/worker.py) opens a GitHub issue carrying the
old->new diff of the doc edit it made. This script polls those issues:

  * issue **closed**       -> the human confirmed -> `kb_doc_writebacks.status`
                              becomes `verified`;
  * a **`/revert` comment** -> undo each applied block in the doc, re-`kb_sync`
                              the mirror, status becomes `reverted`.

Standalone (not a worker sweep) for the same reason as
`ingestion/kb_recrawl.py`: there's no always-on worker host, so the daily
GitHub Actions workflow runs it. GitHub tokens come per-tenant from
`tenant_integrations` (kind='github'), falling back to `GITHUB_TOKEN`.

    python -m ingestion.kb_writeback_watch
    python -m ingestion.kb_writeback_watch --dry-run
"""

from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import kb_writeback  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingestion.kb_writeback_watch")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.kb_writeback_watch")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    sb = get_supabase()
    res = kb_writeback.watch_doc_writebacks(sb, dry_run=args.dry_run)
    log.info("checked=%d verified=%d reverted=%d errors=%d%s",
             res["checked"], res["verified"], res["reverted"], res["errors"],
             " (dry-run)" if res["dry_run"] else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
