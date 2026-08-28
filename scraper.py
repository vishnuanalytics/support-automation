"""
Phase 1: daily scraper + diff sync for docs.zapier.com.

Flow:
  1. Pull the sitemap -> list of doc URLs Zapier currently has.
  2. For each URL: fetch, extract clean text, hash it.
     - New URL                -> insert + chunk + embed.
     - Existing, hash changed -> re-chunk + re-embed, update history.
     - Existing, hash same    -> just bump last_seen_at.
  3. Any URL in the DB but NOT in today's sitemap -> soft-delete
     (missed_runs += 1; mark 'deleted' after N consecutive misses,
     so a transient sitemap hiccup doesn't nuke content).

Run daily via cron:
  0 3 * * * /path/to/venv/bin/python scraper.py >> /var/log/zapier_sync.log 2>&1

Env vars required (put in .env):
  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""

import os
import sys
import time
import hashlib
import logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from sentence_transformers import SentenceTransformer
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

SITEMAP_URL = "https://docs.zapier.com/sitemap.xml"
USER_AGENT = "Mozilla/5.0 (compatible; PortfolioRAGBot/0.1; +https://github.com/yourusername)"
REQUEST_DELAY_SECONDS = 1.0        # be polite -- don't hammer their servers
MISSED_RUNS_BEFORE_DELETE = 3      # soft-delete threshold
CHUNK_SIZE_CHARS = 1200
CHUNK_OVERLAP_CHARS = 150

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zapier_sync")

_embedder = None  # lazy-loaded, local, free (no API calls)


def get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        log.info("Loading local embedding model (all-MiniLM-L6-v2)...")
        _embedder = SentenceTransformer("all-MiniLM-L6-v2")
    return _embedder


def get_supabase():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]  # service role -- bypasses RLS, this is a trusted backend job
    return create_client(url, key)


def fetch_sitemap_urls() -> list[str]:
    resp = requests.get(SITEMAP_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "lxml-xml")
    urls = [loc.text.strip() for loc in soup.find_all("loc")]
    log.info(f"Sitemap returned {len(urls)} URLs")
    return urls


def fetch_page_text(url: str) -> tuple[str, str] | None:
    """Returns (title, clean_text) or None if the page couldn't be read."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    title = soup.title.text.strip() if soup.title else url

    # strip nav/script/style noise -- keep the actual doc content
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.body
    text = main.get_text(separator="\n", strip=True) if main else ""
    return title, text


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(text: str) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE_CHARS
        chunks.append(text[start:end])
        start = end - CHUNK_OVERLAP_CHARS
    return [c for c in chunks if c.strip()]


def upsert_doc_and_chunks(sb, url: str, title: str, text: str, h: str):
    now = datetime.now(timezone.utc).isoformat()
    sb.table("zapier_docs").upsert({
        "url": url,
        "title": title,
        "content_hash": h,
        "raw_text": text,
        "last_seen_at": now,
        "last_changed_at": now,
        "status": "active",
        "missed_runs": 0,
    }).execute()

    sb.table("doc_chunks").delete().eq("doc_url", url).execute()
    chunks = chunk_text(text)
    if not chunks:
        return
    embeddings = get_embedder().encode(chunks).tolist()
    rows = [
        {"doc_url": url, "chunk_index": i, "chunk_text": c, "embedding": e}
        for i, (c, e) in enumerate(zip(chunks, embeddings))
    ]
    sb.table("doc_chunks").insert(rows).execute()
    log.info(f"  -> {len(chunks)} chunks embedded and stored")


def touch_unchanged_doc(sb, url: str):
    sb.table("zapier_docs").update({
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "missed_runs": 0,
    }).eq("url", url).execute()


def mark_missing_docs(sb, seen_urls: set[str]):
    existing = sb.table("zapier_docs").select("url, missed_runs, status").neq("status", "deleted").execute()
    for row in existing.data:
        if row["url"] in seen_urls:
            continue
        missed = row["missed_runs"] + 1
        new_status = "deleted" if missed >= MISSED_RUNS_BEFORE_DELETE else "stale"
        sb.table("zapier_docs").update({"missed_runs": missed, "status": new_status}).eq("url", row["url"]).execute()
        log.info(f"  {row['url']} not seen this run (missed_runs={missed}, status={new_status})")


def run():
    sb = get_supabase()
    sitemap_urls = fetch_sitemap_urls()
    seen = set()

    existing_hashes = {
        row["url"]: row["content_hash"]
        for row in sb.table("zapier_docs").select("url, content_hash").execute().data
    }

    for url in sitemap_urls:
        seen.add(url)
        page = fetch_page_text(url)
        time.sleep(REQUEST_DELAY_SECONDS)
        if page is None:
            continue
        title, text = page
        if not text.strip():
            continue

        h = content_hash(text)
        if url not in existing_hashes:
            log.info(f"NEW: {url}")
            upsert_doc_and_chunks(sb, url, title, text, h)
        elif existing_hashes[url] != h:
            log.info(f"CHANGED: {url}")
            upsert_doc_and_chunks(sb, url, title, text, h)
        else:
            touch_unchanged_doc(sb, url)

    mark_missing_docs(sb, seen)
    log.info(f"Sync complete. {len(seen)} URLs processed.")


if __name__ == "__main__":
    try:
        run()
    except KeyError as e:
        log.error(f"Missing required env var: {e}. Check your .env file.")
        sys.exit(1)
