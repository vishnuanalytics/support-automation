"""
Nightly trim of `jobs` / `runs` (audit C3). Calls the `purge_old(jobs_days,
runs_days)` SQL function (migration 059). Safe to run repeatedly.

    python -m scripts.purge_old                 # 7d jobs / 60d runs
    python -m scripts.purge_old --jobs-days 3 --runs-days 30
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.purge_old")
    ap.add_argument("--jobs-days", type=int, default=7)
    ap.add_argument("--runs-days", type=int, default=60)
    a = ap.parse_args(argv)

    sb = get_supabase()
    res = sb.rpc("purge_old", {"jobs_days": a.jobs_days, "runs_days": a.runs_days}).execute()
    row = (res.data or [{}])[0]
    print(f"purged: jobs={row.get('jobs_deleted', 0)} runs={row.get('runs_deleted', 0)} "
          f"(jobs>{a.jobs_days}d, runs>{a.runs_days}d)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
