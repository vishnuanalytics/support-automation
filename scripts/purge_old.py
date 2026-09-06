"""
Nightly trim of `jobs` / `runs` (audit C3). Calls the `purge_old(jobs_days,
runs_days)` SQL function (migration 059). Safe to run repeatedly.

Also trims `flow_versions` (Phase 28 step 2, migration 077) — keeps the
last `--fv-keep-last` versions per flow and anything newer than
`--fv-min-age-days` unconditionally; never touches a flow's currently
published version.

Also trims `case_events` / `audit_log` (age-only, default 365d) and
`reasoning_sessions` (terminal states only — `sent`/`abandoned` — aged off
`updated_at`; an open session is never touched regardless of age) —
migration 087, the only append-only tables that had no retention policy.

    python -m scripts.purge_old                 # 7d jobs / 60d runs / 20 versions kept, 90d
    python -m scripts.purge_old --jobs-days 3 --runs-days 30
    python -m scripts.purge_old --fv-keep-last 10 --fv-min-age-days 30
    python -m scripts.purge_old --case-events-days 180 --audit-log-days 180 --reasoning-days 180
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
    ap.add_argument("--fv-keep-last", type=int, default=20)
    ap.add_argument("--fv-min-age-days", type=int, default=90)
    ap.add_argument("--case-events-days", type=int, default=365)
    ap.add_argument("--audit-log-days", type=int, default=365)
    ap.add_argument("--reasoning-days", type=int, default=365)
    a = ap.parse_args(argv)

    sb = get_supabase()
    res = sb.rpc("purge_old", {"jobs_days": a.jobs_days, "runs_days": a.runs_days}).execute()
    row = (res.data or [{}])[0]
    print(f"purged: jobs={row.get('jobs_deleted', 0)} runs={row.get('runs_deleted', 0)} "
          f"(jobs>{a.jobs_days}d, runs>{a.runs_days}d)")

    fv_res = sb.rpc("purge_old_flow_versions", {
        "keep_last": a.fv_keep_last, "min_age_days": a.fv_min_age_days,
    }).execute()
    fv_row = (fv_res.data or [{}])[0]
    print(f"purged: flow_versions={fv_row.get('versions_deleted', 0)} "
          f"(keep_last={a.fv_keep_last}, older_than={a.fv_min_age_days}d, "
          f"never the published version)")

    ce_res = sb.rpc("purge_old_case_events", {"retain_days": a.case_events_days}).execute()
    ce_row = (ce_res.data or [{}])[0]
    print(f"purged: case_events={ce_row.get('events_deleted', 0)} "
          f"(older_than={a.case_events_days}d)")

    al_res = sb.rpc("purge_old_audit_log", {"retain_days": a.audit_log_days}).execute()
    al_row = (al_res.data or [{}])[0]
    print(f"purged: audit_log={al_row.get('events_deleted', 0)} "
          f"(older_than={a.audit_log_days}d)")

    rs_res = sb.rpc("purge_old_reasoning_sessions", {"retain_days": a.reasoning_days}).execute()
    rs_row = (rs_res.data or [{}])[0]
    print(f"purged: reasoning_sessions={rs_row.get('sessions_deleted', 0)} "
          f"(terminal states only, older_than={a.reasoning_days}d)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
