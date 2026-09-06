"""
Zendesk trigger (Phase 31 chunk 1) — the missing half of "automation" for
a `case_connector = 'zendesk'` tenant.

`sf_case_watch.py` polls new Salesforce Cases and enqueues a `run_flow`
job for each; there was no equivalent for Zendesk, so a Zendesk tenant
could *act* on tickets (the 8-action connector works) but nothing ever
*triggered* a run from an inbound ticket. This closes that.

Per tenant with `case_connector = 'zendesk'` AND an active `zendesk`
integration: poll `status:new` tickets updated in the lookback window,
normalise each to the interpreter's `case` shape, and enqueue one
`run_flow` job. Job dedupe (unique on kind + dedupe_key) means a ticket
is only enqueued once while a job for it is live, so the lookback can
overlap between runs — no watermark to persist, same as `sf_case_watch`.

    python -m ingestion.zendesk_ticket_watch --once
    python -m ingestion.zendesk_ticket_watch --tenant <uuid> --flow <flow-id>
    python -m ingestion.zendesk_ticket_watch --once --lookback 180

With no Zendesk-connector tenants it prints a notice and exits 0 (so a
cron doesn't error).
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

from interpreter import jobs, zendesk  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zendesk_ticket_watch")


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def _resolve_flow(sb, tenant_id: str) -> str | None:
    """The published flow this tenant's inbound tickets run through: the one
    marked as the case-system entry (`flows.sf_entry`, the editor toggle),
    else the tenant's sole published flow. Ambiguous -> None (skip + warn)."""
    rows = (sb.table("flows").select("flow_id, sf_entry")
            .eq("tenant_id", tenant_id).eq("status", "published")
            .execute().data or [])
    if not rows:
        return None
    entry = [r for r in rows if r.get("sf_entry")]
    if len(entry) == 1:
        return entry[0]["flow_id"]
    if len(rows) == 1:
        return rows[0]["flow_id"]
    return None


def _zendesk_tenants(sb, only: str | None) -> list[str]:
    try:
        trows = (sb.table("tenants").select("tenant_id")
                 .eq("case_connector", "zendesk").execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("tenants read: %s", e)
        return []
    tids = {r["tenant_id"] for r in trows if r.get("tenant_id")}
    try:
        irows = (sb.table("tenant_integrations").select("tenant_id")
                 .eq("kind", "zendesk").eq("status", "active").execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("tenant_integrations read: %s", e)
        return []
    active = tids & {r["tenant_id"] for r in irows if r.get("tenant_id")}
    return [t for t in active if (only is None or t == only)]


def tick(sb, *, tenant_id: str | None = None, flow_id: str | None = None,
         lookback_min: int = 120) -> int:
    tenants = _zendesk_tenants(sb, tenant_id)
    if not tenants:
        log.info("no active zendesk-connector tenants — nothing to watch")
        return 0

    total = 0
    for tid in tenants:
        fid = flow_id or _resolve_flow(sb, tid)
        if not fid:
            log.warning("tenant %s: no unambiguous published flow — skipped", tid)
            continue
        try:
            tickets = zendesk.list_new_tickets(tid, sb, lookback_min=lookback_min)
        except Exception as e:  # noqa: BLE001
            log.warning("tenant %s: list_new_tickets: %s", tid, e)
            continue
        enq = 0
        for t in tickets:
            case = zendesk.ticket_as_case(t, tid, sb)
            if not case.get("sf_id"):
                continue
            key = f"zd:{tid}:{case['sf_id']}"
            job_id = jobs.enqueue(
                "run_flow",
                {"flow_id": fid, "case": case, "idempotency_key": key,
                 "trigger": "zendesk_ticket"},
                dedupe_key=key, tenant_id=tid,
            )
            if job_id:
                enq += 1
        log.info("tenant %s: %d new ticket(s), %d enqueued (rest deduped)",
                 tid, len(tickets), enq)
        total += enq
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.zendesk_ticket_watch")
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

    log.info("watching new Zendesk tickets every %.0fs", args.interval)
    while True:
        try:
            tick(sb, tenant_id=args.tenant, flow_id=args.flow, lookback_min=args.lookback)
        except Exception as e:  # noqa: BLE001
            log.warning("tick failed: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
