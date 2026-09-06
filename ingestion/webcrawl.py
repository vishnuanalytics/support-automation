"""
P7c — a small, bounded generic web crawler for KB ingestion.

    crawl("https://help.acme.com/docs")
      -> [{"url", "title", "markdown"}, ...]   # BFS, same host + path prefix

Bounded: `max_pages` (default 20), `max_depth` (2), one host, only under the
start path. HTTP(S) only; obvious private / loopback hosts are refused (SSRF).
Best-effort robots.txt `Disallow`. Text-only — headings + paragraphs + list
items become light markdown; scripts/nav/footer stripped.

Sitemap-first discovery (2026-09-05): before the BFS, `crawl()` best-effort
fetches `/sitemap.xml` (following one level of a sitemap-index's own
`<sitemap>` entries) and seeds every in-scope URL it finds straight into the
queue. A pure link-follow BFS misses orphan pages (real content nothing on
the crawled path links to) — a sitemap is the site's own index of what
exists, cheaper and more complete than hoping BFS stumbles onto everything.
Best-effort like robots.txt: a missing/malformed/blocked sitemap never fails
the crawl, it just falls back to link-discovery alone.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests

from interpreter.net_safety import is_public_http_url

log = logging.getLogger("ingestion.webcrawl")

_UA = "Mozilla/5.0 (compatible; SupportAutomationKBBot/1.0)"
_SKIP_EXT = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp4",
             ".css", ".js", ".ico", ".woff", ".woff2")
_MAX_REDIRECTS = 5
_SITEMAP_MAX_URLS = 500     # a pre-filter cap; max_pages still bounds actual fetches
_SITEMAP_MAX_SUBMAPS = 10   # a sitemap index rarely needs more to find in-scope urls


def _sitemap_urls(session: requests.Session, base: str, netloc: str, prefix: str,
                  *, timeout: float) -> list[str]:
    """Best-effort `sitemap.xml` discovery. Handles both a plain `<urlset>`
    and a `<sitemapindex>` (one level of sub-sitemaps, capped) -- never
    raises; a missing/malformed/blocked sitemap just yields nothing."""
    def _fetch_xml(url: str) -> ElementTree.Element | None:
        try:
            if not _ok_host(url):
                return None
            r = session.get(url, timeout=timeout)
            if r.status_code != 200:
                return None
            return ElementTree.fromstring(r.content if hasattr(r, "content") else r.text)
        except Exception as e:  # noqa: BLE001 -- best-effort, same discipline as robots.txt
            log.info("sitemap fetch %s: %s", url, e)
            return None

    root = _fetch_xml(urljoin(base, "/sitemap.xml"))
    if root is None:
        return []

    def _locs(el: ElementTree.Element) -> list[str]:
        # namespace-agnostic -- sitemaps almost always declare the standard
        # xmlns, but tolerate one that doesn't rather than silently finding zero
        return [loc.text.strip() for loc in el.iter()
                if loc.tag.rsplit("}", 1)[-1] == "loc" and loc.text]

    tag = root.tag.rsplit("}", 1)[-1]
    urls: list[str] = []
    if tag == "sitemapindex":
        for sm in _locs(root)[:_SITEMAP_MAX_SUBMAPS]:
            sub = _fetch_xml(sm)
            if sub is not None:
                urls.extend(_locs(sub))
    else:
        urls.extend(_locs(root))

    out, seen = [], set()
    for u in urls[:_SITEMAP_MAX_URLS * 2]:   # trim before the prefix filter, not after
        u = u.split("#", 1)[0]
        if u in seen:
            continue
        seen.add(u)
        p = urlparse(u)
        under = prefix == "/" or p.path == prefix or p.path.startswith(prefix + "/")
        if p.netloc == netloc and under and not u.lower().endswith(_SKIP_EXT):
            out.append(u)
        if len(out) >= _SITEMAP_MAX_URLS:
            break
    return out


def _ok_host(url: str) -> bool:
    ok, _ = is_public_http_url(url)
    return ok


def _get_no_ssrf(session: requests.Session, url: str, *, timeout: float) -> requests.Response:
    """`requests.get(..., allow_redirects=True)` never re-checks the host a
    redirect lands on -- a page can 30x to an internal address (or one that
    only resolves privately after the initial DNS-rebinding-safe check) and
    `requests` will happily follow it. Follow redirects by hand, validating
    the resolved IP before every hop, same guard as the pre-queue check."""
    for _ in range(_MAX_REDIRECTS + 1):
        if not _ok_host(url):
            raise requests.RequestException(f"refusing to fetch {url!r} (not a public host)")
        r = session.get(url, timeout=timeout, allow_redirects=False)
        if r.is_redirect or r.is_permanent_redirect:
            nxt = r.headers.get("location")
            if not nxt:
                return r
            url = urljoin(url, nxt)
            continue
        return r
    raise requests.RequestException(f"too many redirects fetching {url!r}")


def _clean_markdown(html: str) -> tuple[str, str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    title = (soup.title.string if soup.title and soup.title.string else "") or ""
    root = soup.find("main") or soup.find("article") or soup.body or soup
    lines: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        txt = " ".join(el.get_text(" ", strip=True).split())
        if not txt:
            continue
        name = el.name
        if name in ("h1", "h2", "h3", "h4"):
            lines.append(("#" * int(name[1])) + " " + txt)
        elif name == "li":
            lines.append("- " + txt)
        else:
            lines.append(txt)
    if not title:
        h1 = root.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
    return title.strip()[:200] or "(untitled)", "\n\n".join(lines).strip()


def _links(html: str, base: str) -> list[str]:
    from bs4 import BeautifulSoup

    out = []
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        u = urljoin(base, a["href"]).split("#", 1)[0]
        if u.lower().endswith(_SKIP_EXT):
            continue
        out.append(u)
    return out


def crawl(start_url: str, *, max_pages: int = 20, max_depth: int = 2,
          delay: float = 0.3, timeout: float = 15.0) -> list[dict]:
    if not _ok_host(start_url):
        raise ValueError(f"refusing to crawl {start_url!r} (must be a public http(s) URL)")
    start = urlparse(start_url)
    prefix = (start.path or "/").rstrip("/") or "/"    # "this section": /docs, /docs/x — not /docs-other

    rp = RobotFileParser()
    try:
        rp.set_url(f"{start.scheme}://{start.netloc}/robots.txt")
        rp.read()
    except Exception:  # noqa: BLE001
        rp = None

    seen: set[str] = set()
    q: deque[tuple[str, int]] = deque([(start_url.split("#", 1)[0], 0)])
    pages: list[dict] = []
    s = requests.Session()
    s.headers["User-Agent"] = _UA

    for u in _sitemap_urls(s, f"{start.scheme}://{start.netloc}", start.netloc, prefix,
                           timeout=timeout):
        if u not in seen:
            q.append((u, 0))

    while q and len(pages) < max_pages:
        url, depth = q.popleft()
        if url in seen:
            continue
        seen.add(url)
        if rp is not None and not rp.can_fetch(_UA, url):
            continue
        try:
            r = _get_no_ssrf(s, url, timeout=timeout)
        except requests.RequestException as e:
            log.warning("crawl %s: %s", url, e)
            continue
        if r.status_code != 200 or "html" not in (r.headers.get("content-type") or "").lower():
            continue
        title, md = _clean_markdown(r.text)
        if len(md) >= 80:                        # skip near-empty pages
            pages.append({"url": url, "title": title, "markdown": md})
        if depth < max_depth:
            for nxt in _links(r.text, url):
                p = urlparse(nxt)
                under = prefix == "/" or p.path == prefix or p.path.startswith(prefix + "/")
                if (p.netloc == start.netloc and under
                        and nxt not in seen and _ok_host(nxt)):
                    q.append((nxt, depth + 1))
        time.sleep(delay)
    return pages
