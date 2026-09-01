"""
Email trigger (Phase 20b) -- the inbound half of the email channel.

For every tenant whose email channel is switched on (`tenant_integrations`
`kind='email'`, `status='active'`), this polls the mailbox for mail past
the channel's saved **cursor** (highest IMAP UID / Gmail internalDate
handled — NOT read-state, so a message a human opens in the mail client is
still picked up), drops auto-responders / list mail / the mailbox's own
messages, and enqueues one `run_flow` job per real message against the
tenant's published flow for the channel's team. The job is also keyed on
the message's `Message-ID` (belt-and-braces: a redelivery within the same
cursor window never double-enqueues). Processed messages are additionally
marked read (IMAP `\\Seen` / Gmail `UNREAD` removed) as a courtesy to
humans; never deleted.

    python -m ingestion.email_watch --once                 # cron: check + exit
    python -m ingestion.email_watch --once --dry-run       # show, enqueue nothing
    python -m ingestion.email_watch --tenant <uuid> --lookback 7
    python -m ingestion.email_watch                        # loop (dev)

Needs Supabase in .env. With no active channel it prints a notice and
exits 0 (so a cron doesn't error). Runs alongside `python -m api.worker`.

    */5 * * * *  python -m ingestion.email_watch --once
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import jobs, mailbox  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("email_watch")


def _published_flow_id(sb, tenant_id: str, team: str) -> str | None:
    rows = (sb.table("flows").select("flow_id")
            .eq("tenant_id", tenant_id).eq("team", team).eq("status", "published")
            .limit(1).execute().data or [])
    return rows[0]["flow_id"] if rows else None


def poll_channel(sb, cfg: "mailbox.MailboxConfig", *, lookback_days: int,
                 limit: int, dry_run: bool, only_from: "set[str] | None" = None) -> int:
    flow_id = _published_flow_id(sb, cfg.tenant_id, cfg.team)
    if not flow_id:
        msg = f"no published '{cfg.team}' flow for tenant {cfg.tenant_id}"
        log.warning("%s -- skipping", msg)
        mailbox.set_status(cfg.tenant_id, sb, "error", error=msg)
        return 0

    try:
        fetched = mailbox.fetch_new(cfg, lookback_days=lookback_days, limit=limit)
    except Exception as e:  # noqa: BLE001
        log.warning("tenant %s: fetch failed: %s", cfg.tenant_id, e)
        mailbox.set_status(cfg.tenant_id, sb, "error", error=f"fetch: {e}")
        return 0

    enqueued = 0
    processed_refs: list[str] = []
    for fm in fetched:
        try:
            case = mailbox.parse_message(fm.raw)
        except Exception as e:  # noqa: BLE001
            log.warning("tenant %s: unparseable message %s: %s", cfg.tenant_id, fm.ref, e)
            processed_refs.append(fm.ref)          # don't re-fetch a broken message
            continue

        if only_from and (case.get("from") or "").lower() not in only_from:
            # test / triage aid — leave everyone else's mail untouched (unread)
            log.info("skip %s (sender not in --from filter)", case.get("message_id", fm.ref))
            continue

        ok, reason = mailbox.should_process(case, cfg)
        if not ok:
            log.info("skip %s (%s): %s", case.get("message_id", fm.ref), reason,
                     case.get("subject", "")[:60])
            processed_refs.append(fm.ref)
            continue

        mid = case["message_id"] or f"noid:{fm.ref}"
        case["tenant_id"] = cfg.tenant_id
        case["team"] = cfg.team
        case["case_id"] = mailbox.thread_key(case) or mid

        if dry_run:
            log.info("[dry-run] would enqueue run_flow for %s (%s)", mid,
                     case.get("subject", "")[:60])
            enqueued += 1
            continue

        job_id = jobs.enqueue(
            "run_flow",
            {"flow_id": flow_id, "case": case, "idempotency_key": mid},
            dedupe_key=f"email:{mid}", sb=sb,
        )
        processed_refs.append(fm.ref)
        if job_id:
            enqueued += 1
            log.info("enqueued %s for %s (%s)", job_id, mid, case.get("subject", "")[:60])
        else:
            log.info("already queued/seen: %s", mid)

    if not dry_run:
        if processed_refs:
            try:
                mailbox.mark_processed(cfg, processed_refs)
            except Exception as e:  # noqa: BLE001
                log.warning("tenant %s: mark-processed failed: %s", cfg.tenant_id, e)
        # advance the poll cursor past everything this tick saw (handled or
        # skipped alike — we never want to see it again), so a message a
        # human reads in the mail client isn't silently dropped next run.
        # `--from` runs are targeted tests: leave the cursor alone.
        if not only_from:
            top = max((fm.sort_key for fm in fetched), default=0)
            key = "internal_date_ms" if cfg.provider == "gmail" else "imap_uid"
            if top > int((cfg.cursor or {}).get(key) or 0):
                try:
                    mailbox.set_cursor(cfg.tenant_id, sb, {**(cfg.cursor or {}), key: top})
                except Exception as e:  # noqa: BLE001
                    log.warning("tenant %s: cursor update failed: %s", cfg.tenant_id, e)
        mailbox.set_status(cfg.tenant_id, sb, "active", error=None)

    log.info("tenant %s: %d fetched, %d enqueued", cfg.tenant_id, len(fetched), enqueued)
    return enqueued


def tick(*, tenant: str | None, lookback_days: int, limit: int, dry_run: bool,
         only_from: "set[str] | None" = None) -> int:
    if not mailbox.poller_is_intake():
        if not getattr(tick, "_idle_logged", False):
            log.info("email poller idle: SF_INTAKE_MODE=%s — Salesforce Email-to-Case is "
                     "the intake (SF-1). Set SF_INTAKE_MODE=poller (or =both) to enable.",
                     mailbox.intake_mode())
            tick._idle_logged = True
        return 0
    sb = get_supabase()
    channels = mailbox.list_pollable_channels(sb)   # active + errored-and-due (auto-recovery)
    if tenant:
        channels = [c for c in channels if c.tenant_id == tenant]
    if not channels:
        log.info("no active email channel%s -- nothing to poll",
                 f" for tenant {tenant}" if tenant else "")
        return 0
    total = 0
    for cfg in channels:
        try:
            total += poll_channel(sb, cfg, lookback_days=lookback_days, limit=limit,
                                  dry_run=dry_run, only_from=only_from)
        except Exception as e:  # noqa: BLE001
            log.warning("tenant %s: poll failed: %s", cfg.tenant_id, e)
    return total


def main(argv: list[str] | None = None) -> int:
    from interpreter.config import validate_env
    validate_env()
    ap = argparse.ArgumentParser(prog="ingestion.email_watch")
    ap.add_argument("--tenant", help="only poll this tenant's channel")
    ap.add_argument("--lookback", type=int, default=3, help="days (bounds a first run)")
    ap.add_argument("--limit", type=int, default=50, help="max messages per tenant per tick")
    ap.add_argument("--dry-run", action="store_true", help="show, enqueue nothing, mark nothing")
    ap.add_argument("--from", dest="only_from", default=None,
                    help="only process mail from this sender (comma-separated); "
                         "other mail is left untouched — for a clean live test")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=300.0)
    args = ap.parse_args(argv)

    only_from = ({a.strip().lower() for a in args.only_from.split(",") if a.strip()}
                 if args.only_from else None)

    if args.once:
        tick(tenant=args.tenant, lookback_days=args.lookback, limit=args.limit,
             dry_run=args.dry_run, only_from=only_from)
        return 0

    from interpreter.health import beat

    log.info("watching mailboxes every %.0fs", args.interval)
    while True:
        try:
            tick(tenant=args.tenant, lookback_days=args.lookback, limit=args.limit,
                 dry_run=args.dry_run, only_from=only_from)
            beat("poller")
        except Exception as e:  # noqa: BLE001
            log.warning("tick failed: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
