"""
Phase 11 — turn accepted drafts into eval gold cases.

Reads `runs` where the human kept the bot's draft (`sent_as_is`, or `edited`
with a small edit_distance) and prints them as `eval/e2e/cases.jsonl` lines
(gold_action = auto_reply — the human effectively sent what the bot wrote).
Review before appending; this doesn't write anything.

    python scripts/harvest_feedback.py [--max-distance 0.2] [--limit 50]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-distance", type=float, default=0.2)
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    sb = get_supabase()
    rows = (
        sb.table("runs")
        .select("subject, case_payload, human_action, edit_distance")
        .in_("human_action", ["sent_as_is", "edited"])
        .order("feedback_checked_at", desc=True)
        .limit(args.limit)
        .execute().data
        or []
    )
    kept = [
        r for r in rows
        if r["human_action"] == "sent_as_is"
        or (r.get("edit_distance") is not None and r["edit_distance"] <= args.max_distance)
    ]
    print(f"# {len(kept)} accepted-draft run(s) -> candidate eval/e2e cases "
          f"(review, then append)\n", file=sys.stderr)
    for i, r in enumerate(kept, 1):
        c = r.get("case_payload") or {}
        print(json.dumps({
            "id": f"hitl{i:02d}",
            "gold_action": "auto_reply",
            "why": f"human {r['human_action']} the bot draft (edit_distance {r.get('edit_distance')})",
            "subject": c.get("subject", r.get("subject") or ""),
            "body": c.get("body", ""),
            "account": c.get("account", {}),
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
