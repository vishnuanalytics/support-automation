"""Load resolved third-party cases into a workspace's Knowledge collection.

    python -m scripts.monitor_load_knowledge --tenant <id> --since 2026-08-01 --until 2026-11-01

Safe to rerun: a case that hasn't changed is skipped.
"""

from __future__ import annotations

import argparse
import logging
import sys


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    args = parser.parse_args()
    logging.disable(logging.INFO)

    from ingestion.scraper import get_supabase
    from interpreter.monitor import knowledge, service
    from interpreter.monitor.store import SupabaseStore

    sb = get_supabase()
    cfg = service.load_config(sb, args.tenant)
    reader = service._case_reader(sb, args.tenant, cfg)
    counts = knowledge.backfill(
        sb, args.tenant, reader, SupabaseStore(sb).partners(args.tenant), cfg,
        f"{args.since}T00:00:00Z", f"{args.until}T00:00:00Z",
        progress=lambda c: print(c, flush=True))
    print("done", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
