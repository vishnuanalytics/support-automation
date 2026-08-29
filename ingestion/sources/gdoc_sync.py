"""
Phase 15 — re-export linked Google Docs whose Drive `modifiedTime` moved.

For every active `kb_entries` row with origin='gdoc', per tenant: check the
doc's modifiedTime; if it's newer than what we last embedded, re-fetch +
re-chunk + re-embed and stamp the row. Auth failures set `sync_error` and
leave the entry's current content in place.

    python -m ingestion.sources.gdoc_sync            # loop (not really needed)
    python -m ingestion.sources.gdoc_sync --once     # one pass (cron)

Needs Supabase (.env) + a tenant that has connected Google
(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET set). Runs the local embedder.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
import time

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from ingestion.sources.kb_common import embed_entry  # noqa: E402
from interpreter import gdrive  # noqa: E402


def _collection_name(sb, source_id: str) -> str:
    rows = sb.table("sources").select("name").eq("source_id", source_id).execute().data
    return rows[0]["name"] if rows else ""


def sync_once(sb) -> dict[str, int]:
    if not gdrive.available():
        print("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set — nothing to sync")
        return {"checked": 0, "updated": 0, "errors": 0}

    rows = (sb.table("kb_entries").select("*")
            .eq("origin", "gdoc").eq("status", "active").execute().data or [])
    checked = updated = errors = 0
    for e in rows:
        checked += 1
        tid, did = e["tenant_id"], e["gdoc_id"]
        try:
            mtime = gdrive.get_modified_time(tid, did, sb)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            sb.table("kb_entries").update({"sync_error": str(exc)[:500]}) \
                .eq("entry_id", e["entry_id"]).execute()
            print(f"  {e['title']!r}: modifiedTime check failed — {exc}")
            continue
        if mtime == e.get("gdoc_modified"):
            continue
        try:
            fetched = gdrive.fetch_doc(tid, did, sb)
            url = f"kb://{e['source_id']}/{e['entry_id']}"
            n = embed_entry(sb, source_id=e["source_id"], url=url,
                            title=fetched["title"], body_md=fetched["markdown"],
                            section=_collection_name(sb, e["source_id"]))
            sb.table("kb_entries").update({
                "title": fetched["title"], "body_md": fetched["markdown"],
                "gdoc_modified": fetched["modified_time"], "synced_at": "now()",
                "sync_error": None, "chunk_count": n,
                "embed_hash": hashlib.md5(fetched["markdown"].encode()).hexdigest(),
                "embedded_at": "now()",
            }).eq("entry_id", e["entry_id"]).execute()
            updated += 1
            print(f"  {fetched['title']!r}: re-synced ({n} chunks)")
        except Exception as exc:  # noqa: BLE001
            errors += 1
            sb.table("kb_entries").update({"sync_error": str(exc)[:500]}) \
                .eq("entry_id", e["entry_id"]).execute()
            print(f"  {e['title']!r}: re-sync failed — {exc}")
    print(f"gdoc sync: checked {checked}, updated {updated}, errors {errors}")
    return {"checked": checked, "updated": updated, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingestion.sources.gdoc_sync")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=900.0)
    args = ap.parse_args(argv)
    sb = get_supabase()
    if args.once:
        sync_once(sb)
        return 0
    while True:
        sync_once(sb)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
