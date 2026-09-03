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
            if url not in pages:
                raise AssertionError(f"unexpected fetch {url}")
            return _Resp(pages[url])

    import requests
    monkeypatch.setattr(requests, "Session", lambda: _S())
    monkeypatch.setattr(webcrawl, "RobotFileParser", lambda: None)
    monkeypatch.setattr(webcrawl.time, "sleep", lambda *_: None)


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


def test_crawl_refuses_a_private_start():
    with pytest.raises(ValueError):
        webcrawl.crawl("http://127.0.0.1/docs")


def test_worker_crawl_site_enqueues_one_embed_per_page(monkeypatch):
    from api import worker

    monkeypatch.setattr("ingestion.webcrawl.crawl",
                        lambda url, **k: [{"url": "u1", "title": "P1", "markdown": "x" * 200},
                                          {"url": "u2", "title": "P2", "markdown": "y" * 200}])
    enq = []
    monkeypatch.setattr("interpreter.jobs.enqueue",
                        lambda kind, payload, **kw: enq.append((kind, payload["entry_id"])))

    class _T:
        def __init__(s): s.n = 0
        def select(s, *a, **k): return s
        def eq(s, *a, **k): return s
        def limit(s, n): return s
        def insert(s, row): s._row = row; return s
        def update(s, row): s._row = row; return s
        def execute(s):
            s.n += 1
            return type("R", (), {"data": [{"entry_id": f"e{s.n}"}]})

    sb = type("SB", (), {"table": lambda self, n: _T()})()
    out = worker._crawl_site({"source_id": "s", "tenant_id": "t", "collection_name": "c",
                              "url": "https://x", "max_pages": 5}, sb)
    assert out["entries"] == 2
    assert [k for k, _ in enq] == ["embed_kb_entry", "embed_kb_entry"]
