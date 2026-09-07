"""P7c — the bounded generic web crawler."""

from __future__ import annotations

import pathlib
import socket
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ingestion import webcrawl
from interpreter import net_safety

_PAGE_A = """<html><head><title>Docs Home</title></head><body>
<nav>skip me</nav>
<main><h1>Getting started</h1><p>This is a longer intro paragraph with clearly enough words in it to be well over the minimum length the crawler keeps.</p>
<ul><li>point one here</li><li>point two here</li></ul>
<a href="/docs/guide">Guide</a> <a href="/docs/guide">dup</a>
<a href="https://other.example/docs/guide">off-host</a>
<a href="/pricing">outside prefix</a>
<a href="/docs/manual.pdf">asset</a>
</main><footer>legal</footer></body></html>"""

_PAGE_B = """<html><head><title>The Guide</title></head><body>
<article><h2>Step one</h2><p>Do the first thing, which needs a decent number of words here so the page is kept by the length filter and not discarded as empty.</p></article>
</body></html>"""


class _Resp:
    def __init__(self, text, *, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.text = text
        self.is_redirect = status_code in (301, 302, 303, 307, 308)
        self.is_permanent_redirect = status_code in (301, 308)


@pytest.fixture(autouse=True)
def _resolve_test_hosts_publicly(monkeypatch):
    """help.acme.com etc. are fake test domains -- is_public_http_url does a
    real DNS lookup, so every test host needs a resolved (public) address.
    IP-literal hosts (127.0.0.1, 169.254.169.254, ...) resolve instantly
    without a network call and must NOT be faked -- those are exactly what
    the private/loopback/link-local tests below need resolved for real."""
    real = socket.getaddrinfo

    def fake(host, port):
        try:
            return real(host, port)
        except OSError:
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(net_safety.socket, "getaddrinfo", fake)


@pytest.fixture
def crawl_net(monkeypatch):
    pages = {
        "https://help.acme.com/docs": _PAGE_A,
        "https://help.acme.com/docs/guide": _PAGE_B,
    }

    class _S:
        headers: dict = {}
        def get(self, url, **kw):
            if url in ("https://help.acme.com/sitemap.xml", "https://help.acme.com/robots.txt"):
                return _Resp("", status_code=404)
            if url not in pages:
                raise AssertionError(f"unexpected fetch {url}")
            return _Resp(pages[url])

    import requests
    monkeypatch.setattr(requests, "Session", lambda: _S())
    monkeypatch.setattr(webcrawl, "RobotFileParser", lambda: None)
    monkeypatch.setattr(webcrawl.time, "sleep", lambda *_: None)


def test_crawl_ignores_robots_when_the_fetch_is_blocked(monkeypatch):
    """A WAF (Cloudflare et al.) that 403s the robots.txt request must NOT
    turn into a site-wide 'disallow all' — the pre-fix code used
    RobotFileParser.read() (urllib UA -> 403 -> disallow_all=True) and
    silently crawled zero pages of an otherwise-open site."""
    real_rp = webcrawl.RobotFileParser  # not the fixture's stub — the real class

    class _S:
        headers: dict = {}
        def get(self, url, **kw):
            if url == "https://help.acme.com/robots.txt":
                return _Resp("<html>Attention Required! | Cloudflare</html>",
                             status_code=403, headers={"content-type": "text/html"})
            if url == "https://help.acme.com/sitemap.xml":
                return _Resp("", status_code=404)
            if url == "https://help.acme.com/docs":
                return _Resp(_PAGE_A)
            if url == "https://help.acme.com/docs/guide":
                return _Resp(_PAGE_B)
            raise AssertionError(f"unexpected fetch {url}")

    import requests
    monkeypatch.setattr(requests, "Session", lambda: _S())
    monkeypatch.setattr(webcrawl, "RobotFileParser", real_rp)
    monkeypatch.setattr(webcrawl.time, "sleep", lambda *_: None)

    out = webcrawl.crawl("https://help.acme.com/docs", max_pages=10)
    assert {p["url"] for p in out} == {
        "https://help.acme.com/docs", "https://help.acme.com/docs/guide",
    }


def test_crawl_raises_when_the_start_url_itself_is_blocked(monkeypatch):
    """A crawl that captures nothing because its start URL 403s should be a
    loud failure, not a silent 0-page 'success' the caller can't diagnose."""
    class _S:
        headers: dict = {}
        def get(self, url, **kw):
            if url == "https://help.acme.com/robots.txt":
                return _Resp("", status_code=404)
            if url == "https://help.acme.com/sitemap.xml":
                return _Resp("", status_code=404)
            return _Resp("<html>blocked</html>", status_code=403,
                         headers={"content-type": "text/html"})

    import requests
    monkeypatch.setattr(requests, "Session", lambda: _S())
    monkeypatch.setattr(webcrawl, "RobotFileParser", lambda: None)
    monkeypatch.setattr(webcrawl.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="403"):
        webcrawl.crawl("https://help.acme.com/docs", max_pages=10)


def test_ok_host_blocks_private_and_non_http():
    assert webcrawl._ok_host("https://help.acme.com/x")
    assert not webcrawl._ok_host("http://localhost:8000/x")
    assert not webcrawl._ok_host("http://169.254.169.254/latest/meta-data")
    assert not webcrawl._ok_host("ftp://acme.com/x")


def test_get_no_ssrf_refuses_a_redirect_to_a_private_host(monkeypatch):
    """Security fix — `allow_redirects=True` never re-checked the host a
    redirect landed on. A page that 302s to link-local/internal metadata
    must be refused, not silently fetched and indexed into the KB."""
    calls = []

    class _S:
        def get(self, url, **kw):
            calls.append(url)
            if url == "https://help.acme.com/docs":
                return _Resp("", status_code=302,
                            headers={"location": "http://169.254.169.254/latest/meta-data"})
            raise AssertionError(f"should never fetch the redirect target: {url}")

    import requests
    with pytest.raises(requests.RequestException):
        webcrawl._get_no_ssrf(_S(), "https://help.acme.com/docs", timeout=5)
    assert calls == ["https://help.acme.com/docs"]  # never followed the redirect


def test_get_no_ssrf_follows_a_safe_redirect(monkeypatch):
    class _S:
        def __init__(self):
            self.n = 0
        def get(self, url, **kw):
            self.n += 1
            if self.n == 1:
                return _Resp("", status_code=302,
                            headers={"location": "https://help.acme.com/docs/guide"})
            return _Resp(_PAGE_B)

    r = webcrawl._get_no_ssrf(_S(), "https://help.acme.com/docs", timeout=5)
    assert r.status_code == 200 and "Step one" in r.text


def test_clean_markdown_strips_chrome_and_keeps_structure():
    title, md = webcrawl._clean_markdown(_PAGE_A)
    assert title == "Docs Home"
    assert md.startswith("# Getting started")
    assert "- point one here" in md
    assert "skip me" not in md and "legal" not in md


def test_links_resolves_relative_and_skips_assets():
    ls = webcrawl._links(_PAGE_A, "https://help.acme.com/docs")
    assert "https://help.acme.com/docs/guide" in ls
    assert not any(l.endswith(".pdf") for l in ls)


def test_crawl_stays_on_host_and_under_prefix(crawl_net):
    pages = webcrawl.crawl("https://help.acme.com/docs", max_pages=10, max_depth=2)
    urls = {p["url"] for p in pages}
    assert urls == {"https://help.acme.com/docs", "https://help.acme.com/docs/guide"}
    guide = next(p for p in pages if p["url"].endswith("/guide"))
    assert guide["title"] == "The Guide" and "Step one" in guide["markdown"]


# ── sitemap-first discovery ──────────────────────────────────────────────
_SITEMAP_URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://help.acme.com/docs/orphan</loc></url>
  <url><loc>https://help.acme.com/docs/guide</loc></url>
  <url><loc>https://help.acme.com/pricing</loc></url>
  <url><loc>https://other.example/docs/x</loc></url>
  <url><loc>https://help.acme.com/docs/manual.pdf</loc></url>
</urlset>"""

_SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://help.acme.com/sitemap-docs.xml</loc></sitemap>
</sitemapindex>"""


def test_sitemap_urls_filters_to_host_and_prefix_and_skips_assets(monkeypatch):
    class _S:
        def get(self, url, **kw):
            assert url == "https://help.acme.com/sitemap.xml"
            return _Resp(_SITEMAP_URLSET)

    out = webcrawl._sitemap_urls(_S(), "https://help.acme.com", "help.acme.com", "/docs", timeout=5)
    assert out == ["https://help.acme.com/docs/orphan", "https://help.acme.com/docs/guide"]


def test_sitemap_urls_follows_one_level_of_sitemap_index(monkeypatch):
    class _S:
        def get(self, url, **kw):
            if url == "https://help.acme.com/sitemap.xml":
                return _Resp(_SITEMAP_INDEX)
            assert url == "https://help.acme.com/sitemap-docs.xml"
            return _Resp(_SITEMAP_URLSET)

    out = webcrawl._sitemap_urls(_S(), "https://help.acme.com", "help.acme.com", "/docs", timeout=5)
    assert "https://help.acme.com/docs/orphan" in out


def test_sitemap_urls_missing_or_malformed_never_raises(monkeypatch):
    class _S404:
        def get(self, url, **kw):
            return _Resp("", status_code=404)

    class _SBad:
        def get(self, url, **kw):
            return _Resp("not xml at all")

    class _SDown:
        def get(self, url, **kw):
            raise __import__("requests").RequestException("down")

    for s in (_S404(), _SBad(), _SDown()):
        assert webcrawl._sitemap_urls(s, "https://help.acme.com", "help.acme.com", "/docs", timeout=5) == []


def test_crawl_finds_an_orphan_page_via_sitemap_that_bfs_never_would(monkeypatch):
    pages = {
        "https://help.acme.com/docs": _PAGE_A,
        "https://help.acme.com/docs/guide": _PAGE_B,
        "https://help.acme.com/docs/orphan": """<html><head><title>Orphan</title></head>
        <body><main><h1>Orphan page</h1><p>Nothing on the crawled path links here, only the sitemap knows
        about this page, which is exactly the gap sitemap-first discovery closes for real sites.</p>
        </main></body></html>""",
    }

    class _S:
        headers: dict = {}
        def get(self, url, **kw):
            if url == "https://help.acme.com/sitemap.xml":
                return _Resp(_SITEMAP_URLSET)
            if url not in pages:
                raise AssertionError(f"unexpected fetch {url}")
            return _Resp(pages[url])

    import requests
    monkeypatch.setattr(requests, "Session", lambda: _S())
    monkeypatch.setattr(webcrawl, "RobotFileParser", lambda: None)
    monkeypatch.setattr(webcrawl.time, "sleep", lambda *_: None)

    pages_out = webcrawl.crawl("https://help.acme.com/docs", max_pages=10, max_depth=2)
    urls = {p["url"] for p in pages_out}
    assert "https://help.acme.com/docs/orphan" in urls
    orphan = next(p for p in pages_out if p["url"].endswith("/orphan"))
    assert orphan["title"] == "Orphan"


def test_crawl_refuses_a_private_start():
    with pytest.raises(ValueError):
        webcrawl.crawl("http://127.0.0.1/docs")


# ── interpreter/kb_connectors: the public_url connector's sync() ──────────
# The worker-side diff/upsert/embed/archive dance moved to the generic
# _sync_kb_connection driver (tests/test_kb_connectors.py); this connector is
# now just a pure producer of KBDocuments.
from interpreter import kb_connectors  # noqa: E402


def _ctx():
    return kb_connectors.SyncCtx(tenant_id="t", sb=None, collection_name="c")


def test_public_url_sync_makes_one_document_per_page(monkeypatch):
    monkeypatch.setattr("ingestion.webcrawl.crawl",
                        lambda url, **k: [{"url": "https://x/a", "title": "P1", "markdown": "x" * 50},
                                          {"url": "https://x/b", "title": "P2", "markdown": "y" * 50}])
    res = kb_connectors._sync_public_url({"url": "https://x", "max_pages": 20}, None, _ctx())
    assert [d.external_id for d in res.documents] == ["P1", "P2"]
    assert res.documents[0].body_md == "<!-- https://x/a -->\n\n" + "x" * 50
    assert res.documents[0].origin == "crawl"
    assert res.exhaustive is True


def test_public_url_sync_not_exhaustive_when_truncated(monkeypatch):
    monkeypatch.setattr("ingestion.webcrawl.crawl",
                        lambda url, **k: [{"url": "https://x/a", "title": "P1", "markdown": "x" * 50}])
    res = kb_connectors._sync_public_url({"url": "https://x", "max_pages": 1}, None, _ctx())
    assert res.exhaustive is False   # pages == max_pages -> can't tell if anything is gone


def test_public_url_normalize_rejects_non_http_and_clamps_pages():
    spec = kb_connectors.get_kb_connector("public_url")
    assert spec.normalize_config({"url": "https://x", "max_pages": 999})["max_pages"] == 50
    with pytest.raises(ValueError):
        spec.normalize_config({"url": "ftp://x"})
