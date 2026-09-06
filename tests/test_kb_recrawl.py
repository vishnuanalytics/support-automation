"""Scheduled refresh: reads every active `kb_source_connections` row
(migration 091) and re-enqueues one `kb_sync` job per connection. Offline:
Supabase and the job queue are both mocked."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ingestion import kb_recrawl


class _FakeSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "kb_source_connections"
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, _k, _v):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


def _conns():
    return [
        {"connection_id": "c1", "source_id": "s1", "connector": "public_url", "label": "docs"},
        {"connection_id": "c2", "source_id": "s1", "connector": "gsheets", "label": "faq"},
        {"connection_id": "c3", "source_id": "s2", "connector": "gdocs", "label": "policy"},
    ]


def test_main_enqueues_one_kb_sync_job_per_connection(monkeypatch):
    sb = _FakeSB(_conns())
    monkeypatch.setattr(kb_recrawl, "get_supabase", lambda: sb)
    calls = []
    monkeypatch.setattr(kb_recrawl.jobs, "enqueue",
                        lambda kind, payload, **kw: calls.append((kind, payload, kw)) or "job1")

    rc = kb_recrawl.main([])
    assert rc == 0
    assert [k for k, _, _ in calls] == ["kb_sync", "kb_sync", "kb_sync"]
    assert [p["connection_id"] for _, p, _ in calls] == ["c1", "c2", "c3"]
    assert [kw["dedupe_key"] for _, _, kw in calls] == [
        "kb_sync:c1", "kb_sync:c2", "kb_sync:c3",
    ]


def test_main_dry_run_enqueues_nothing(monkeypatch):
    sb = _FakeSB(_conns())
    monkeypatch.setattr(kb_recrawl, "get_supabase", lambda: sb)
    calls = []
    monkeypatch.setattr(kb_recrawl.jobs, "enqueue", lambda *a, **k: calls.append(1) or "job1")

    rc = kb_recrawl.main(["--dry-run"])
    assert rc == 0
    assert calls == []


def test_main_counts_deduped_jobs(monkeypatch):
    sb = _FakeSB(_conns())
    monkeypatch.setattr(kb_recrawl, "get_supabase", lambda: sb)
    monkeypatch.setattr(kb_recrawl.jobs, "enqueue", lambda *a, **k: None)  # already queued
    rc = kb_recrawl.main([])
    assert rc == 0


def test_main_no_connections_is_a_noop(monkeypatch):
    sb = _FakeSB([])
    monkeypatch.setattr(kb_recrawl, "get_supabase", lambda: sb)
    calls = []
    monkeypatch.setattr(kb_recrawl.jobs, "enqueue", lambda *a, **k: calls.append(1))
    assert kb_recrawl.main([]) == 0
    assert calls == []
