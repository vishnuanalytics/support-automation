"""Score the GitHub issue reader against hand-labelled issues.

    python -m eval.monitor.issue_status --gold /path/to/gold.json --tenant <id>

Gold is a JSON object of {"<issue url>": status}. Issues are read from the last
sync's evidence, so run a sync first. It holds issue numbers from a private
repository, so keep it out of this one. The number that matters most is issues
shown further along than they are, because nobody would chase those.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--gold", required=True)
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()
    logging.disable(logging.WARNING)

    from supabase import create_client

    from interpreter.monitor import issue_status as st

    gold = json.load(open(args.gold))
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    rows = (sb.table("monitor_evidence").select("url, data").eq("tenant_id", args.tenant)
            .in_("url", list(gold)).execute().data or [])
    issues = [r["data"] for r in rows]
    missing = set(gold) - {ev["url"] for ev in issues}
    reads = st.read_all(args.tenant, issues, {}, {"llm_enabled": True})
    order = [st.NO_REPLY, st.INVESTIGATING, st.CAUSE_GIVEN, st.ANSWERED]
    right, ahead = 0, []
    for url, read in sorted(reads.items()):
        want, got = gold[url], read["status"]
        right += want == got
        if got in order and order.index(got) > order.index(want):
            ahead.append(url)
        if want != got:
            print(f"{url.rsplit('/', 1)[-1]}: {want} -> {got} ({read['method']}) {read['reason']}")
    print(f"exact {right}/{len(reads)} | shown too far along {len(ahead)} | not in evidence {len(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
