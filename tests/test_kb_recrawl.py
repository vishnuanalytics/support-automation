"""Scheduled re-crawl: reads sources.config.crawl_urls (recorded by
api/worker.py::_crawl_site) and re-enqueues one crawl_site job per
(source, url). Offline: Supabase and the job queue are both mocked."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ingestion import kb_recrawl


class _FakeSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "sources"
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, _k, _v):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


def test_crawl_targets_flattens_one_row_per_source_url_pair():
    sb = _FakeSB([
        {"source_id": "s1", "tenant_id": "t1", "name": "Help Center",
         "config": {"crawl_urls": ["https://help.acme.com/docs", "https://help.acme.com/faq"]}},
        {"source_id": "s2", "tenant_id": "t2", "name": "Manual upload only", "config": {}},
        {"source_id": "s3", "tenant_id": "t1", "name": "No config key at all"},
    ])
    out = kb_recrawl._crawl_targets(sb)
    assert out == [
        {"source_id": "s1", "tenant_id": "t1", "collection_name": "Help Center",
         "url": "https://help.acme.com/docs"},
        {"source_id": "s1", "tenant_id": "t1", "collection_name": "Help Center",
         "url": "https://help.acme.com/faq"},
    ]


def test_main_enqueues_one_job_per_target(monkeypatch):
    sb = _FakeSB([{"source_id": "s1", "tenant_id": "t1", "name": "Help Center",
                  "config": {"crawl_urls": ["https://help.acme.com/docs"]}}])
    monkeypatch.setattr(kb_recrawl, "get_supabase", lambda: sb)
    calls = []
    monkeypatch.setattr(kb_recrawl.jobs, "enqueue",
                        lambda kind, payload, **kw: calls.append((kind, payload, kw)) or "job1")
    rc = kb_recrawl.main([])
    assert rc == 0
    assert len(calls) == 1
    kind, payload, kw = calls[0]
    assert kind == "crawl_site"
    assert payload["url"] == "https://help.acme.com/docs"
    assert kw["dedupe_key"] == "crawl:s1:https://help.acme.com/docs"


def test_main_dry_run_enqueues_nothing(monkeypatch):
    sb = _FakeSB([{"source_id": "s1", "tenant_id": "t1", "name": "Help Center",
                  "config": {"crawl_urls": ["https://help.acme.com/docs"]}}])
    monkeypatch.setattr(kb_recrawl, "get_supabase", lambda: sb)
    calls = []
    monkeypatch.setattr(kb_recrawl.jobs, "enqueue", lambda *a, **k: calls.append(1) or "job1")
    rc = kb_recrawl.main(["--dry-run"])
    assert rc == 0
    assert calls == []


def test_main_counts_deduped_jobs(monkeypatch):
    sb = _FakeSB([{"source_id": "s1", "tenant_id": "t1", "name": "Help Center",
                  "config": {"crawl_urls": ["https://help.acme.com/docs"]}}])
    monkeypatch.setattr(kb_recrawl, "get_supabase", lambda: sb)
    monkeypatch.setattr(kb_recrawl.jobs, "enqueue", lambda *a, **k: None)  # already queued
    rc = kb_recrawl.main([])
    assert rc == 0
