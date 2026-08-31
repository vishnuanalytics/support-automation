"""
Fast local loop for testing the email -> Salesforce pipeline. One process,
one log stream: poll the mailbox, run the flow, and print exactly what
landed on each Case (owner queue, priority, module, topic, outcome).

    python scripts/email_dev.py                       # loop: poll + drain every 10s
    python scripts/email_dev.py --from you@gmail.com  # only YOUR test mail (skip inbox noise)
    python scripts/email_dev.py --interval 5          # poll faster
    python scripts/email_dev.py --once                # a single cycle
    python scripts/email_dev.py --once --dry-run      # show what WOULD be picked up

Needs SUPABASE + Groq + SF creds in .env and an active email channel
(scripts re-create it if it's gone). Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from api import worker  # noqa: E402
from ingestion import email_watch  # noqa: E402
from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import salesforce as sfmod  # noqa: E402

C = {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "d": "\033[2m", "0": "\033[0m"}


def _summarise_run(sb, sf, run_id: str) -> None:
    r = sb.table("runs").select(
        "subject, outcome, tier, confidence, case_payload, trace"
    ).eq("run_id", run_id).execute().data
    if not r:
        return
    r = r[0]
    cp = r.get("case_payload") or {}
    sf_id = cp.get("sf_id") or cp.get("id")
    steps = " → ".join(t["type"] for t in (r.get("trace") or []))
    print(f"  {C['d']}{steps}{C['0']}")
    line = (f"  outcome={C['y']}{r['outcome']}{C['0']}  tier={r['tier']}  "
            f"conf={r['confidence']}  \"{(r.get('subject') or '')[:50]}\"")
    if sf_id and sfmod.available():
        try:
            row = sf.query(
                "SELECT CaseNumber, Owner.Name, Owner.Type, Priority, Module__c, "
                "SubModule__c, Topic__c FROM Case WHERE Id = '%s'" % sf_id
            )["records"][0]
            o = row.get("Owner") or {}
            queue = o["Name"] if o.get("Type") == "Queue" else f"(unassigned — {o.get('Name')})"
            print(line)
            print(f"  → Case {C['g']}{row['CaseNumber']}{C['0']}  owner={C['g']}{queue}{C['0']}  "
                  f"Priority={row['Priority']}  Module={row['Module__c']}/{row['SubModule__c']}  "
                  f"Topic={row['Topic__c']}")
        except Exception as e:  # noqa: BLE001
            print(line + f"  {C['r']}(SF read failed: {e}){C['0']}")
    else:
        print(line)


def cycle(sb, sf, *, only_from, lookback, dry_run) -> None:
    stamp = time.strftime("%H:%M:%S")
    before = {r["run_id"] for r in sb.table("runs").select("run_id")
              .order("created_at", desc=True).limit(50).execute().data}
    n = email_watch.tick(tenant=None, lookback_days=lookback, limit=25,
                         dry_run=dry_run, only_from=only_from)
    if dry_run:
        print(f"{C['d']}{stamp}{C['0']}  dry-run: {n} message(s) would be enqueued")
        return
    drained = 0
    while worker.process_one(sb):
        drained += 1
    if n == 0 and drained == 0:
        print(f"{C['d']}{stamp}  idle{C['0']}")
        return
    print(f"{C['d']}{stamp}{C['0']}  enqueued {n}, ran {drained}")
    after = sb.table("runs").select("run_id, created_at").order(
        "created_at", desc=True).limit(50).execute().data
    for r in reversed(after):
        if r["run_id"] not in before:
            _summarise_run(sb, sf, r["run_id"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="only_from", help="only process mail from this sender (comma-sep)")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--lookback", type=int, default=3)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    only_from = ({a.strip().lower() for a in args.only_from.split(",") if a.strip()}
                if args.only_from else None)

    sb = get_supabase()
    sf = sfmod._client() if sfmod.available() else None
    ch = sb.table("tenant_integrations").select("status").eq("kind", "email").execute().data
    if not ch:
        sys.exit("no email channel configured — run scripts/sf_seed_teams.py notes / recreate it first")
    print(f"email channel status={ch[0]['status']}  "
          f"filter={sorted(only_from) if only_from else 'ALL inbound'}  "
          f"interval={args.interval}s\n")

    if args.once:
        cycle(sb, sf, only_from=only_from, lookback=args.lookback, dry_run=args.dry_run)
        return 0
    try:
        while True:
            cycle(sb, sf, only_from=only_from, lookback=args.lookback, dry_run=args.dry_run)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
