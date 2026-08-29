"""
Phase 1: daily scraper + diff sync for docs.zapier.com.

docs.zapier.com runs on Mintlify, which publishes a clean Markdown twin of
every page at <url>.md, plus a sitemap with <lastmod>. This scraper uses both.

Flow:
  1. Pull the sitemap -> {url: lastmod}.
  2. Fetch <url>.md, but only when the page is new or its lastmod is newer
     than what we last stored. Falls back to HTML + trafilatura if .md 404s.
  3. Hash the normalised Markdown (SHA-256):
       new url        -> insert + chunk + embed + capture links
       hash changed   -> re-chunk + re-embed + re-capture links
       hash unchanged -> bump last_seen_at only
  4. Structure-aware chunking: split on Markdown headings, keep fenced code
     and tables whole, prepend a breadcrumb + heading-path context line to
     every chunk. Embed with BAAI/bge-small-en-v1.5 via fastembed
     (384-d, quantised ONNX, CPU-only, local, free -- no torch).
  5. Capture same-host links -> doc_links, for the Neo4j LINKS_TO graph.
  6. Soft-delete: a url (or a link) that disappears is not removed at once --
     missed_runs += 1, and 'deleted' only after 3 consecutive misses.

Run daily via cron:
  0 3 * * * /path/to/venv/bin/python scraper.py >> /var/log/zapier_sync.log 2>&1

Env vars required (put in .env):
  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""

import os
import re
import sys
import time
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin, urldefrag

import requests
from bs4 import BeautifulSoup
from fastembed import TextEmbedding
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

SITEMAP_URL = "https://docs.zapier.com/sitemap.xml"
DOCS_HOST = "docs.zapier.com"
USER_AGENT = "Mozilla/5.0 (compatible; PortfolioRAGBot/0.2; +https://github.com/yourusername)"
REQUEST_DELAY_SECONDS = 1.0          # be polite -- don't hammer their servers
MISSED_RUNS_BEFORE_DELETE = 3        # soft-delete threshold

EMBED_MODEL = "BAAI/bge-small-en-v1.5"   # 384-d, 512-token window; run via fastembed (quantised ONNX, CPU, no torch)
TARGET_CHARS = 1600                      # ~400 tokens: preferred chunk size
MAX_CHARS = 2200                         # hard cap for prose (code/tables stay whole)
MIN_CHARS = 240                          # don't flush a chunk smaller than this

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zapier_sync")

_embedder = None  # lazy-loaded, local, free (no API calls)


def get_embedder() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        log.info(f"Loading local embedding model ({EMBED_MODEL})...")
        # fastembed: quantised ONNX, CPU-only, no torch. bge-small output is
        # L2-normalised by the model's own post-processing, so the vectors are
        # numerically interchangeable with the sentence-transformers build
        # (verified cosine ~1.0) -- no re-embed needed for the swap.
        _embedder = TextEmbedding(model_name=EMBED_MODEL)
    return _embedder


def get_supabase():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]  # service role -- bypasses RLS, trusted backend job
    return create_client(url, key)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------
def fetch_sitemap() -> dict[str, str | None]:
    """{url: lastmod_iso_or_None} for every <url> in the sitemap."""
    resp = requests.get(SITEMAP_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "lxml-xml")
    out: dict[str, str | None] = {}
    for url_el in soup.find_all("url"):
        loc = url_el.find("loc")
        if not loc:
            continue
        lastmod = url_el.find("lastmod")
        out[loc.text.strip()] = lastmod.text.strip() if lastmod else None
    log.info(f"Sitemap returned {len(out)} URLs")
    return out


def fetch_markdown(url: str) -> tuple[str, str] | None:
    """(title, markdown) via <url>.md, HTML+trafilatura as fallback. None on failure."""
    md_url = url.rstrip("/") + ".md"
    try:
        r = requests.get(md_url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code == 200 and r.text.strip() and "<html" not in r.text[:200].lower():
            return (_md_title(r.text) or url), r.text
    except requests.RequestException as e:
        log.warning(f"  .md fetch failed for {url}: {e}")

    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"  html fetch failed for {url}: {e}")
        return None

    md = ""
    try:
        import trafilatura
        md = trafilatura.extract(
            r.text, output_format="markdown",
            include_tables=True, include_links=True, url=url,
        ) or ""
    except ImportError:
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.body
        md = main.get_text("\n", strip=True) if main else ""

    if not md.strip():
        return None
    soup = BeautifulSoup(r.text, "lxml")
    title = soup.title.text.strip() if soup.title else url
    return title, md


def _md_title(md: str) -> str | None:
    m = re.search(r'^\s*title:\s*["\']?(.+?)["\']?\s*$', md, re.M)   # YAML frontmatter
    if m:
        return m.group(1).strip()
    m = re.search(r'^\s*#\s+(.+?)\s*$', md, re.M)                    # first H1
    return m.group(1).strip() if m else None


def normalise(md: str) -> str:
    md = md.replace("\r\n", "\n")
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def breadcrumb(url: str) -> tuple[str, str]:
    """(section, 'Api Reference > Actions > Stored Actions') from the URL path."""
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    section = parts[0] if parts else ""
    pretty = " > ".join(p.replace("-", " ").title() for p in parts)
    return section, pretty


# --------------------------------------------------------------------------
# Structure-aware chunking
# --------------------------------------------------------------------------
_HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
_FENCE = re.compile(r'^\s*(```|~~~)')


def _blocks(md: str):
    """Yield (kind, text): 'heading' | 'code' | 'table' | 'prose'."""
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if _HEADING.match(line):
            yield ("heading", line)
            i += 1
        elif _FENCE.match(line):
            buf = [line]
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                buf.append(lines[i]); i += 1
            if i < len(lines):
                buf.append(lines[i]); i += 1
            yield ("code", "\n".join(buf))
        elif line.strip().startswith("|"):
            buf = [line]; i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                buf.append(lines[i]); i += 1
            yield ("table", "\n".join(buf))
        elif line.strip():
            buf = [line]; i += 1
            while i < len(lines) and lines[i].strip() \
                    and not _HEADING.match(lines[i]) and not _FENCE.match(lines[i]):
                buf.append(lines[i]); i += 1
            yield ("prose", "\n".join(buf).strip())
        else:
            i += 1


def chunk_markdown(md: str, crumb: str) -> list[dict]:
    """
    Split on heading boundaries; keep code/tables atomic; prepend a
    "> {crumb} - {H1 / H2 / H3}" context line to every chunk so each one
    is self-locating for retrieval.
    """
    heading_stack: list[tuple[int, str]] = []
    chunks: list[dict] = []
    cur: list[str] = []
    cur_len = 0

    def hpath() -> str:
        return " / ".join(name for _, name in heading_stack)

    def flush(force_type: str | None = None):
        nonlocal cur, cur_len
        body = "\n\n".join(b for b in cur if b.strip())
        cur, cur_len = [], 0
        if not body.strip():
            return
        prefix = f"> {crumb}" + (f" — {hpath()}" if hpath() else "")
        text = f"{prefix}\n\n{body}"
        chunks.append({
            "chunk_text": text,
            "heading_path": hpath() or None,
            "chunk_type": force_type or "prose",
            "token_count": max(1, len(text) // 4),
        })

    for kind, text in _blocks(md):
        if kind == "heading":
            m = _HEADING.match(text)
            level, name = len(m.group(1)), m.group(2).strip()
            if cur_len >= MIN_CHARS:
                flush()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, name))
        elif kind in ("code", "table"):
            if cur_len >= MIN_CHARS:
                flush()
            cur.append(text)
            flush(force_type=kind)
        else:  # prose
            if cur_len and cur_len + len(text) > MAX_CHARS:
                flush()
            cur.append(text)
            cur_len += len(text) + 2
            if cur_len >= TARGET_CHARS:
                flush()

    if cur:
        flush()
    return chunks


# --------------------------------------------------------------------------
# Link capture
# --------------------------------------------------------------------------
_MD_LINK = re.compile(r'\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
_AUTOLINK = re.compile(r'<(https?://[^>]+)>')


def extract_links(md: str, source_url: str) -> dict[str, str]:
    """{normalised_target_url: anchor_text} for same-host doc links, minus self."""
    out: dict[str, str] = {}

    def add(href: str, anchor: str):
        href = href.strip()
        if not href or href.startswith(("mailto:", "tel:", "#")):
            return
        absu, _ = urldefrag(urljoin(source_url, href))
        p = urlparse(absu)
        if p.netloc and p.netloc != DOCS_HOST:
            return
        path = p.path[:-3] if p.path.endswith(".md") else p.path
        path = path.rstrip("/")
        if not path:
            return
        out.setdefault(f"https://{DOCS_HOST}{path}", anchor.strip()[:200])

    for anchor, href in _MD_LINK.findall(md):
        add(href, anchor)
    for href in _AUTOLINK.findall(md):
        add(href, "")
    out.pop(source_url.rstrip("/"), None)
    return out


# --------------------------------------------------------------------------
# DB writes
# --------------------------------------------------------------------------
def upsert_doc(sb, url: str, title: str, md: str, h: str):
    now = _now()
    sb.table("zapier_docs").upsert({
        "url": url, "title": title, "content_hash": h, "raw_text": md,
        "last_seen_at": now, "last_changed_at": now,
        "status": "active", "missed_runs": 0,
    }).execute()


def replace_chunks(sb, url: str, md: str, crumb: str, section: str) -> int:
    sb.table("doc_chunks").delete().eq("doc_url", url).execute()
    chunks = chunk_markdown(md, crumb)
    if not chunks:
        return 0
    embeddings = [
        vec.tolist()
        for vec in get_embedder().embed(
            [c["chunk_text"] for c in chunks], batch_size=32,
        )
    ]
    rows = [
        {
            "doc_url": url, "chunk_index": i, "chunk_text": c["chunk_text"],
            "embedding": e, "heading_path": c["heading_path"],
            "chunk_type": c["chunk_type"], "token_count": c["token_count"],
            "section": section,
        }
        for i, (c, e) in enumerate(zip(chunks, embeddings))
    ]
    sb.table("doc_chunks").insert(rows).execute()
    log.info(f"  -> {len(rows)} chunks embedded and stored")
    return len(rows)


def sync_links(sb, source_url: str, links: dict[str, str]):
    now = _now()
    if links:
        sb.table("doc_links").upsert([
            {"source_url": source_url, "target_url": t, "anchor_text": a,
             "last_seen_at": now, "missed_runs": 0, "status": "active"}
            for t, a in links.items()
        ]).execute()
    existing = sb.table("doc_links").select("target_url, missed_runs, status") \
        .eq("source_url", source_url).neq("status", "deleted").execute().data
    for row in existing:
        if row["target_url"] in links:
            continue
        missed = row["missed_runs"] + 1
        status = "deleted" if missed >= MISSED_RUNS_BEFORE_DELETE else "stale"
        sb.table("doc_links").update({"missed_runs": missed, "status": status}) \
            .eq("source_url", source_url).eq("target_url", row["target_url"]).execute()


def touch_unchanged_doc(sb, url: str):
    sb.table("zapier_docs").update({"last_seen_at": _now(), "missed_runs": 0}) \
        .eq("url", url).execute()


def mark_missing_docs(sb, seen_urls: set[str]):
    existing = sb.table("zapier_docs").select("url, missed_runs, status") \
        .neq("status", "deleted").execute().data
    for row in existing:
        if row["url"] in seen_urls:
            continue
        missed = row["missed_runs"] + 1
        status = "deleted" if missed >= MISSED_RUNS_BEFORE_DELETE else "stale"
        sb.table("zapier_docs").update({"missed_runs": missed, "status": status}) \
            .eq("url", row["url"]).execute()
        log.info(f"  {row['url']} not seen this run (missed_runs={missed}, status={status})")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def run():
    sb = get_supabase()
    sitemap = fetch_sitemap()

    stored = {
        r["url"]: r
        for r in sb.table("zapier_docs")
        .select("url, content_hash, last_changed_at, status").execute().data
    }
    seen: set[str] = set()
    stats = {"new": 0, "changed": 0, "unchanged": 0, "skipped": 0, "failed": 0, "chunks": 0}

    for url, lastmod in sitemap.items():
        seen.add(url)
        prev = stored.get(url)

        # incremental skip: known, live, and the sitemap says it hasn't changed
        lm, lc = _parse_iso(lastmod), _parse_iso(prev["last_changed_at"]) if prev else None
        if prev and prev["status"] != "deleted" and lm and lc and lm <= lc:
            touch_unchanged_doc(sb, url)
            stats["skipped"] += 1
            continue

        page = fetch_markdown(url)
        time.sleep(REQUEST_DELAY_SECONDS)
        if page is None:
            stats["failed"] += 1
            continue
        title, md = page
        md = normalise(md)
        if not md:
            stats["failed"] += 1
            continue

        h = content_hash(md)
        section, crumb = breadcrumb(url)

        if prev is None or prev["content_hash"] != h:
            log.info(f"{'NEW' if prev is None else 'CHANGED'}: {url}")
            upsert_doc(sb, url, title, md, h)
            stats["chunks"] += replace_chunks(sb, url, md, crumb, section)
            sync_links(sb, url, extract_links(md, url))
            stats["new" if prev is None else "changed"] += 1
        else:
            touch_unchanged_doc(sb, url)
            stats["unchanged"] += 1

    mark_missing_docs(sb, seen)
    log.info(f"Sync complete: {stats}")


if __name__ == "__main__":
    try:
        run()
    except KeyError as e:
        log.error(f"Missing required env var: {e}. Check your .env file.")
        sys.exit(1)
