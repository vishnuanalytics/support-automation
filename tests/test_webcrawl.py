"""P7c — the bounded generic web crawler."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ingestion import webcrawl

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
    def __init__(self, text):
        self.status_code = 200
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.text = text


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
