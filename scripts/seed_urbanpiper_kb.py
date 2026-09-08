"""
Crawl UrbanPiper's public docs into the gunner workspace's KB so the
`retrieve` node in its flow has real domain content to ground on.

    python -m scripts.seed_urbanpiper_kb                # crawl + embed (skips junk)
    python -m scripts.seed_urbanpiper_kb --prune        # archive thin / marketing pages
    python -m scripts.seed_urbanpiper_kb --list         # show what's stored

Per-site page budgets (help.urbanpiper.com is the deep one). Marketing /
locale / legal pages on www.urbanpiper.com are filtered out — they added
noise, not answers. Creates one `sources` row (`urbanpiper-docs`, tenant =
gunner) and embeds each kept page via `kb_common.embed_entry` — the same
chunk+embed path the KB API uses, tagged with that `source_id` so
`retrieval.resolve_sources` scopes it to this tenant automatically.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from ingestion.sources.kb_common import embed_entry  # noqa: E402
from ingestion.webcrawl import crawl  # noqa: E402

TENANT = "ee4102db-ab47-4deb-b28b-9efc5a02575b"  # gunner
SOURCE_NAME = "urbanpiper-docs"
MIN_CHARS = 200

# (start_url, section label, page budget)
SITES = [
    ("https://help.urbanpiper.com/", "UrbanPiper Help Centre", 150),
    ("https://api-docs.urbanpiper.com/downstream/api", "UrbanPiper API (downstream)", 60),
    ("https://www.urbanpiper.com/", "UrbanPiper", 40),
]

# www.urbanpiper.com is a marketing site — keep only substantive product pages.
_JUNK_URL = re.compile(
    r"urbanpiper\.com/(?:"
    r"(?:[a-z]{2}(?:-[a-z]{2})?)(?:/|$)"           # locale roots: /es, /ar-ae, /en-sa ...
    r"|legal/|blog(?:/|$)|thank-you|free-trial|partner-with-us|contact"
    r"|about-us|about\b|careers|press|newsroom|customers|case-stud|webinar|events"
    r"|pricing$|request-demo|book-a-demo|sitemap"
    r")",
    re.I,
)


def _is_junk(url: str) -> bool:
    return "www.urbanpiper.com" in url and bool(_JUNK_URL.search(url))


def _source_id(sb) -> str:
    rows = (sb.table("sources").select("source_id")
            .eq("tenant_id", TENANT).eq("name", SOURCE_NAME).execute().data)
    if rows:
        return rows[0]["source_id"]
    return (sb.table("sources").insert({
        "kind": "internal_kb", "tenant_id": TENANT, "name": SOURCE_NAME,
        "status": "active",
        "config": {"origin": "crawl",
                   "note": "UrbanPiper public docs (help centre + downstream API)"},
    }).execute().data[0]["source_id"])


def seed() -> None:
    sb = get_supabase()
    sid = _source_id(sb)
    print(f"source {SOURCE_NAME} = {sid}")
    kept = skipped = 0
    for start, section, budget in SITES:
        print(f"\ncrawling {start}  (<= {budget} pages)")
        try:
            docs = crawl(start, max_pages=budget)
        except Exception as e:  # noqa: BLE001
            print(f"  ! crawl failed: {e}")
            continue
        print(f"  {len(docs)} page(s) fetched")
        for pg in docs:
            url = pg["url"]
            md = (pg.get("markdown") or "").strip()
            if _is_junk(url) or len(md) < MIN_CHARS:
                skipped += 1
                continue
            try:
                n = embed_entry(
                    sb, source_id=sid, url=url,
                    title=(pg.get("title") or url)[:200],
                    body_md=md, section=section,
                    crumb=f"{section} > {pg.get('title') or url}",
                )
                kept += 1
                print(f"    + {n:>3} chunks  {url}")
            except Exception as e:  # noqa: BLE001
                print(f"    ! embed failed for {url}: {e}")
    print(f"\ndone: {kept} page(s) embedded, {skipped} skipped (junk / too thin)")


def prune() -> None:
    """Archive marketing / locale / legal pages and near-empty shells already
    in the source, so they stop diluting retrieval."""
    sb = get_supabase()
    rows = (sb.table("sources").select("source_id")
            .eq("tenant_id", TENANT).eq("name", SOURCE_NAME).execute().data)
    if not rows:
        print("(no urbanpiper-docs source)")
        return
    sid = rows[0]["source_id"]
    docs = (sb.table("zapier_docs").select("url, title")
            .eq("source_id", sid).eq("status", "active").execute().data or [])
    n = 0
    for d in docs:
        cnt = (sb.table("doc_chunks").select("doc_url", count="exact")
               .eq("doc_url", d["url"]).execute()).count or 0
        if _is_junk(d["url"]) or cnt < 2:
            sb.table("doc_chunks").delete().eq("doc_url", d["url"]).execute()
            sb.table("zapier_docs").update({"status": "deleted"}).eq("url", d["url"]).execute()
            n += 1
            print(f"  archived  {d['title'][:60]:<60}  {d['url']}")
    print(f"\npruned {n} doc(s)")


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
    active = [d for d in docs if d["status"] == "active"]
    chunks = (sb.table("doc_chunks").select("doc_url", count="exact")
              .eq("source_id", sid).execute())
    print(f"{SOURCE_NAME} ({sid}): {len(active)} active doc(s) / "
          f"{len(docs)} total, {chunks.count} chunk(s)")
    for d in sorted(active, key=lambda r: r["url"]):
        print(f"  {d['title'][:70]:<70}  {d['url']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prune", action="store_true", help="archive thin / marketing pages and exit")
    ap.add_argument("--list", action="store_true", help="show stored docs and exit")
    args = ap.parse_args()
    if args.list:
        show()
    elif args.prune:
        prune()
    else:
        seed()


if __name__ == "__main__":
    main()
