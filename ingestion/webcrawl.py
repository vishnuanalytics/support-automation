"""
P7c — a small, bounded generic web crawler for KB ingestion.

    crawl("https://help.acme.com/docs")
      -> [{"url", "title", "markdown"}, ...]   # BFS, same host + path prefix

Bounded: `max_pages` (default 20), `max_depth` (2), one host, only under the
start path. HTTP(S) only; obvious private / loopback hosts are refused (SSRF).
Best-effort robots.txt `Disallow`. Text-only — headings + paragraphs + list
items become light markdown; scripts/nav/footer stripped.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from interpreter.net_safety import is_public_http_url

log = logging.getLogger("ingestion.webcrawl")

_UA = "Mozilla/5.0 (compatible; SupportAutomationKBBot/1.0)"
_SKIP_EXT = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp4",
             ".css", ".js", ".ico", ".woff", ".woff2")
_MAX_REDIRECTS = 5


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
