"""
HubSpot trigger — the missing half of "automation" for a
`case_connector = 'hubspot'` tenant, mirroring `ingestion/zendesk_ticket_watch.py`
exactly (see that module's own docstring for the design rationale).

Per tenant with `case_connector = 'hubspot'` AND an active `hubspot`
integration: poll tickets in the "New" pipeline stage updated in the
lookback window, normalise each to the interpreter's `case` shape, and
enqueue one `run_flow` job. Job dedupe (unique on kind + dedupe_key) means a
ticket is only enqueued once while a job for it is live, so the lookback
can overlap between runs — no watermark to persist, same as `sf_case_watch`/
`zendesk_ticket_watch`.

    python -m ingestion.hubspot_ticket_watch --once
    python -m ingestion.hubspot_ticket_watch --tenant <uuid> --flow <flow-id>
    python -m ingestion.hubspot_ticket_watch --once --lookback 180

With no HubSpot-connector tenants it prints a notice and exits 0 (so a cron
doesn't error). Not wired into `docker-compose.yml` yet — Zendesk's own
equivalent isn't either; both are standalone CLI tools until this project's
"no always-on host" gap is addressed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from interpreter import hubspot, jobs  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hubspot_ticket_watch")


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def _resolve_flow(sb, tenant_id: str) -> str | None:
    """Thin wrapper — the real logic is `hubspot.resolve_entry_flow`, shared
    with the `/webhooks/hubspot/{tenant_id}` push receiver so both triggers
    resolve the same flow the same way."""
    return hubspot.resolve_entry_flow(tenant_id, sb)


def _hubspot_tenants(sb, only: str | None) -> list[str]:
    return hubspot.active_connector_tenants(sb, only)


def tick(sb, *, tenant_id: str | None = None, flow_id: str | None = None,
         lookback_min: int = 120) -> int:
    tenants = _hubspot_tenants(sb, tenant_id)
    if not tenants:
        log.info("no active hubspot-connector tenants — nothing to watch")
        return 0

    total = 0
    for tid in tenants:
        fid = flow_id or _resolve_flow(sb, tid)
        if not fid:
            log.warning("tenant %s: no unambiguous published flow — skipped", tid)
            continue
        try:
            tickets = hubspot.list_new_tickets(tid, sb, lookback_min=lookback_min)
        except Exception as e:  # noqa: BLE001
            log.warning("tenant %s: list_new_tickets: %s", tid, e)
            continue
        enq = 0
        for t in tickets:
            case = hubspot.ticket_as_case(t, tid, sb)
            if not case.get("sf_id"):
                continue
            key = f"hs:{tid}:{case['sf_id']}"
            job_id = jobs.enqueue(
                "run_flow",
                {"flow_id": fid, "case": case, "idempotency_key": key,
                 "trigger": "hubspot_ticket"},
                dedupe_key=key, tenant_id=tid,
            )
            if job_id:
                enq += 1
        log.info("tenant %s: %d new ticket(s), %d enqueued (rest deduped)",
                 tid, len(tickets), enq)
        total += enq
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.hubspot_ticket_watch")
    ap.add_argument("--tenant", help="only this tenant id")
    ap.add_argument("--flow", help="force this flow id (else resolved per tenant)")
    ap.add_argument("--lookback", type=int, default=120, help="minutes")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=300.0)
    args = ap.parse_args(argv)

    sb = _sb()
    if args.once:
        tick(sb, tenant_id=args.tenant, flow_id=args.flow, lookback_min=args.lookback)
        return 0

    log.info("watching new HubSpot tickets every %.0fs", args.interval)
    while True:
        try:
            tick(sb, tenant_id=args.tenant, flow_id=args.flow, lookback_min=args.lookback)
        except Exception as e:  # noqa: BLE001
            log.warning("tick failed: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
