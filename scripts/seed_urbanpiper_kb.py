"""
Crawl UrbanPiper's public docs into the gunner workspace's KB so the
`retrieve` node in its flow has real domain content to ground on.

    python -m scripts.seed_urbanpiper_kb                # crawl + embed
    python -m scripts.seed_urbanpiper_kb --pages 30     # per-site page budget
    python -m scripts.seed_urbanpiper_kb --list         # show what's stored

Creates one `sources` row (`urbanpiper-docs`, tenant = gunner) and embeds
each crawled page via `ingestion.sources.kb_common.embed_entry` — the same
chunk+embed path the KB API uses, tagged with that `source_id` so
`retrieval.resolve_sources` scopes it to this tenant automatically (no flow
change needed — a name-less `retrieve` already pulls the tenant's own
sources).
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from ingestion.sources.kb_common import embed_entry  # noqa: E402
from ingestion.webcrawl import crawl  # noqa: E402

TENANT = "ee4102db-ab47-4deb-b28b-9efc5a02575b"  # gunner
SOURCE_NAME = "urbanpiper-docs"

SITES = [
    ("https://help.urbanpiper.com/", "UrbanPiper Help Centre"),
    ("https://www.urbanpiper.com/", "UrbanPiper"),
    ("https://api-docs.urbanpiper.com/downstream/api", "UrbanPiper API (downstream)"),
]


def _source_id(sb) -> str:
    rows = (sb.table("sources").select("source_id")
            .eq("tenant_id", TENANT).eq("name", SOURCE_NAME).execute().data)
    if rows:
        return rows[0]["source_id"]
    return (sb.table("sources").insert({
        "kind": "internal_kb", "tenant_id": TENANT, "name": SOURCE_NAME,
        "status": "active",
        "config": {"origin": "crawl",
                   "note": "UrbanPiper public docs (help centre, site, API)"},
    }).execute().data[0]["source_id"])


def seed(pages: int) -> None:
    sb = get_supabase()
    sid = _source_id(sb)
    print(f"source {SOURCE_NAME} = {sid}")
    total = 0
    for start, section in SITES:
        print(f"\ncrawling {start}  (<= {pages} pages)")
        try:
            docs = crawl(start, max_pages=pages)
        except Exception as e:  # noqa: BLE001
            print(f"  ! crawl failed: {e}")
            continue
        print(f"  {len(docs)} page(s)")
        for pg in docs:
            md = (pg.get("markdown") or "").strip()
            if len(md) < 120:            # skip near-empty shells / redirects
                continue
            try:
                n = embed_entry(
                    sb, source_id=sid, url=pg["url"],
                    title=(pg.get("title") or pg["url"])[:200],
                    body_md=md, section=section,
                    crumb=f"{section} > {pg.get('title') or pg['url']}",
                )
                total += 1
                print(f"    + {n:>3} chunks  {pg['url']}")
            except Exception as e:  # noqa: BLE001
                print(f"    ! embed failed for {pg['url']}: {e}")
    print(f"\ndone: {total} page(s) embedded into {SOURCE_NAME} for tenant {TENANT}")


def show() -> None:
    sb = get_supabase()
    rows = (sb.table("sources").select("source_id")
            .eq("tenant_id", TENANT).eq("name", SOURCE_NAME).execute().data)
    if not rows:
        print("(no urbanpiper-docs source yet)")
        return
    sid = rows[0]["source_id"]
    docs = (sb.table("zapier_docs").select("url, title, status")
            .eq("source_id", sid).execute().data or [])
    chunks = (sb.table("doc_chunks").select("doc_url", count="exact")
              .eq("source_id", sid).execute())
    print(f"{SOURCE_NAME} ({sid}): {len(docs)} doc(s), {chunks.count} chunk(s)")
    for d in sorted(docs, key=lambda r: r["url"]):
        flag = "" if d["status"] == "active" else f" [{d['status']}]"
        print(f"  {d['title'][:70]:<70}{flag}  {d['url']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=40, help="per-site page budget (max 50)")
    ap.add_argument("--list", action="store_true", help="show stored docs and exit")
    args = ap.parse_args()
    if args.list:
        show()
        return
    seed(max(1, min(args.pages, 50)))


if __name__ == "__main__":
    main()
