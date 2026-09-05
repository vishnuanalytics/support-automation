"""Scheduled refresh: reads sources.config.crawl_urls / config.gsheets
(recorded by api/worker.py::_crawl_site / _sync_gsheet) and re-enqueues one
job per known target. Offline: Supabase and the job queue are both
mocked."""

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


def test_gsheet_targets_flattens_one_row_per_source_sheet_pair():
    sb = _FakeSB([
        {"source_id": "s1", "tenant_id": "t1", "name": "FAQ Sheet",
         "config": {"gsheets": [{"sheet_id": "sheet1", "sheet_name": None},
                                {"sheet_id": "sheet2", "sheet_name": "Archive"}]}},
        {"source_id": "s2", "tenant_id": "t2", "name": "Crawl only",
         "config": {"crawl_urls": ["https://x"]}},
    ])
    out = kb_recrawl._gsheet_targets(sb)
    assert out == [
        {"source_id": "s1", "tenant_id": "t1", "collection_name": "FAQ Sheet",
         "sheet_id": "sheet1", "sheet_name": None},
        {"source_id": "s1", "tenant_id": "t1", "collection_name": "FAQ Sheet",
         "sheet_id": "sheet2", "sheet_name": "Archive"},
    ]


def test_main_enqueues_both_crawl_and_gsheet_jobs(monkeypatch):
    sb = _FakeSB([{"source_id": "s1", "tenant_id": "t1", "name": "Mixed",
                  "config": {"crawl_urls": ["https://help.acme.com/docs"],
                            "gsheets": [{"sheet_id": "sheet1", "sheet_name": None}]}}])
    monkeypatch.setattr(kb_recrawl, "get_supabase", lambda: sb)
    calls = []
    monkeypatch.setattr(kb_recrawl.jobs, "enqueue",
                        lambda kind, payload, **kw: calls.append((kind, payload, kw)) or "job1")
    rc = kb_recrawl.main([])
    assert rc == 0
    assert sorted(k for k, _, _ in calls) == ["crawl_site", "sync_gsheet"]
    sheet_call = next(c for c in calls if c[0] == "sync_gsheet")
    assert sheet_call[1]["sheet_id"] == "sheet1"
    assert sheet_call[2]["dedupe_key"] == "gsheet:s1:sheet1:"
