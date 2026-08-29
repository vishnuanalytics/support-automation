"""
Phase 16 — expire stale action_requests.

Any `action_requests` row still `pending` after APPROVAL_TTL_H hours
(default 24) is flipped to `expired` and its Slack message edited to say so.

    python -m scripts.expire_approvals            # one pass (cron)

Needs Supabase (.env). Slack edit is best-effort.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import slack as slackmod  # noqa: E402


def main() -> int:
    sb = get_supabase()
    ttl_h = int(os.environ.get("APPROVAL_TTL_H", "24"))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=ttl_h)).isoformat()

    stale = (sb.table("action_requests").select("*")
             .eq("status", "pending").lt("created_at", cutoff).execute().data or [])
    for ar in stale:
        sb.table("action_requests").update({"status": "expired"}).eq("id", ar["id"]).execute()
        try:
            if ar.get("slack_channel") and ar.get("slack_ts") and slackmod.available():
                slackmod.update_message(
                    ar["tenant_id"], ar["slack_channel"], ar["slack_ts"],
                    f":ghost: *{ar['payload'].get('title')}* — expired (no decision in {ttl_h}h).",
                    sb,
                )
        except Exception as e:  # noqa: BLE001
            print(f"  {ar['id']}: slack edit failed — {e}")
    print(f"expired {len(stale)} stale approval(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
