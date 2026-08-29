"""
Salesforce trigger (Phase 10) — the missing half of "automation".

Polls for recently-touched `New` Cases and enqueues a `run_flow` job for
each. Job dedupe (unique on kind+dedupe_key) means a Case is only ever
enqueued once while a job for it is live, so the lookback window can safely
overlap between runs — no watermark to persist.

    python -m ingestion.sf_case_watch --once                 # cron: check + exit
    python -m ingestion.sf_case_watch --flow <id> --lookback 90
    python -m ingestion.sf_case_watch                        # loop (dev)

Needs Salesforce creds + Supabase in .env. With no SF creds it prints a
notice and exits 0 (so a cron doesn't error).

Intended cron (GitHub Actions or similar), alongside a running worker:
    */15 * * * *  python -m ingestion.sf_case_watch --once
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from interpreter import jobs, salesforce  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sf_case_watch")

ACME_SUPPORT = "11111111-1111-1111-1111-111111111111"


def _new_case_ids(lookback_min: int) -> list[str]:
    sf = salesforce._client()
    soql = (
        "SELECT Id FROM Case "
        f"WHERE Status = 'New' AND LastModifiedDate >= {_iso_minutes_ago(lookback_min)} "
        "ORDER BY LastModifiedDate DESC LIMIT 200"
    )
    return [r["Id"] for r in sf.query(soql).get("records", [])]


def _iso_minutes_ago(minutes: int) -> str:
    import datetime as _dt

    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def tick(flow_id: str, lookback_min: int) -> int:
    ids = _new_case_ids(lookback_min)
    enqueued = 0
    for cid in ids:
        case = salesforce.get_case(cid)
        job_id = jobs.enqueue(
            "run_flow",
            {"flow_id": flow_id, "case": case, "idempotency_key": cid},
            dedupe_key=cid,
        )
        if job_id:
            enqueued += 1
            log.info("enqueued %s for Case %s", job_id, cid)
    log.info("%d new Case(s) seen, %d enqueued (rest deduped)", len(ids), enqueued)
    return enqueued


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.sf_case_watch")
    ap.add_argument("--flow", default=ACME_SUPPORT, help="flow to run new Cases through")
    ap.add_argument("--lookback", type=int, default=60, help="minutes")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=300.0)
    args = ap.parse_args(argv)

    if not salesforce.available():
        log.info("no Salesforce creds — nothing to watch. exiting.")
        return 0

    if args.once:
        tick(args.flow, args.lookback)
        return 0

    log.info("watching new Cases every %.0fs", args.interval)
    while True:
        try:
            tick(args.flow, args.lookback)
        except Exception as e:  # noqa: BLE001
            log.warning("tick failed: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
