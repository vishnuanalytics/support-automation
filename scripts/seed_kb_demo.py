"""
Phase 14 demo seed — the `globex-billing-runbook` internal KB collection.

Creates the collection (a `sources` row, kind='internal_kb', tenant Globex)
and one entry, then chunks + embeds it into the shared content tables via
`ingestion.sources.kb_common.embed_entry` — the same path the KB API uses.
Idempotent: re-running replaces the entry's chunks.

    python -m scripts.seed_kb_demo

Pairs with db/migrations/023_seed_kb_checkpoint.sql, which wires a
`kb_lookup` node into the Globex flow's billing branch.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from ingestion.sources.kb_common import embed_entry  # noqa: E402

GLOBEX = "22222222-2222-2222-2222-222222222222"
COLLECTION = "globex-billing-runbook"
ENTRY_TITLE = "Refund & credit approval limits"
ENTRY_BODY = """\
# Refund & credit approval limits

Internal policy — do **not** quote these thresholds to the customer, just
apply them.

## Refunds
- **Under $200** — auto-approve. Agent can issue directly in Billing Admin.
- **$200 to $2,000** — a team lead must approve in the #billing-approvals
  channel before the credit is issued.
- **Over $2,000** — manager sign-off required, and a note must be added to
  the account's Salesforce record with the approval thread link.

## Proration on mid-cycle plan changes
Upgrades are charged a prorated amount immediately. Downgrades take effect
at the next renewal — never issue an immediate refund for a downgrade;
explain the credit will apply next cycle.

## Annual contracts
Annual plans are non-refundable after 30 days. Before day 30, refunds
follow the thresholds above. A true-up invoice covers seats added mid-term.
"""


def main() -> int:
    sb = get_supabase()

    rows = sb.table("sources").select("source_id").eq("name", COLLECTION).execute().data
    if rows:
        sid = rows[0]["source_id"]
    else:
        sid = sb.table("sources").insert({
            "kind": "internal_kb", "tenant_id": GLOBEX, "name": COLLECTION,
            "config": {"description": "Billing refund/credit thresholds (internal)"},
        }).execute().data[0]["source_id"]
    print(f"collection {COLLECTION} -> {sid}")

    existing = (sb.table("kb_entries").select("entry_id")
                .eq("source_id", sid).eq("title", ENTRY_TITLE).execute().data)
    if existing:
        eid = existing[0]["entry_id"]
        sb.table("kb_entries").update({"body_md": ENTRY_BODY}).eq("entry_id", eid).execute()
    else:
        eid = sb.table("kb_entries").insert({
            "source_id": sid, "tenant_id": GLOBEX, "title": ENTRY_TITLE,
            "body_md": ENTRY_BODY,
        }).execute().data[0]["entry_id"]

    n = embed_entry(sb, source_id=sid, url=f"kb://{sid}/{eid}", title=ENTRY_TITLE,
                    body_md=ENTRY_BODY, section=COLLECTION)
    sb.table("kb_entries").update({
        "chunk_count": n,
        "embed_hash": hashlib.md5(ENTRY_BODY.encode()).hexdigest(),
        "embedded_at": "now()",
    }).eq("entry_id", eid).execute()
    print(f"entry {ENTRY_TITLE!r} -> {eid}  ({n} chunks embedded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
