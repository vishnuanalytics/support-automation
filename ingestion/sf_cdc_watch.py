"""
Salesforce → automation push via Change Data Capture (Phase 20l).

Streams `Case` + `EmailMessage` change events off the Salesforce Pub/Sub
API and enqueues a `run_flow` job for:

  * a new Case                         -> trigger "case_created"
  * a new inbound EmailMessage on it   -> trigger "inbound_email"
  * a Case whose OwnerId (queue) moved -> trigger "case_owner_changed"

Durable: the last processed `replay_id` per topic is stored in
`sf_cdc_state`, so a restart resumes where it stopped (72h retention).

    python -m ingestion.sf_cdc_watch                 # run forever
    python -m ingestion.sf_cdc_watch --max-events 5  # drain a few, exit
    python -m ingestion.sf_cdc_watch --topics /data/CaseChangeEvent

Prereqs: Setup → Change Data Capture → add `Case` and `EmailMessage` to
Selected Entities; Salesforce creds in `.env` (same JWT bearer app the
rest of the pipeline uses). Without creds this exits 0 with a notice so a
docker/CI run doesn't crash-loop.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import salesforce  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ingestion.sf_cdc_watch")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.sf_cdc_watch")
    ap.add_argument("--topics", default=None,
                    help="comma-separated CDC channels (default: Case + EmailMessage)")
    ap.add_argument("--max-events", type=int, default=None,
                    help="process this many events then exit (smoke test)")
    args = ap.parse_args(argv)

    if not salesforce.available():
        log.warning("no Salesforce creds in the env — CDC subscriber idle (set SF_USERNAME / "
                    "SF_CONSUMER_KEY / SF_PRIVATE_KEY_FILE to enable).")
        return 0

    from ingestion.sf_pubsub.subscriber import DEFAULT_TOPICS, PubSubSubscriber

    topics = (tuple(t.strip() for t in args.topics.split(",") if t.strip())
              if args.topics else DEFAULT_TOPICS)
    sub = PubSubSubscriber(get_supabase(), topics=topics)
    n = sub.run(max_events=args.max_events)
    log.info("stopped after %d event(s)", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
