"""interpreter/posthog.py — the PostHog product-analytics connector
(Phase 30 chunk 2). Config hygiene, credential brokering, HogQL rollup
assembly. No network: `_query` / `requests` are stubbed."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import posthog as ph


# ── config hygiene ───────────────────────────────────────────────────
def test_clean_host_adds_scheme_and_trims():
    assert ph._clean_host("us.posthog.com") == "https://us.posthog.com"
    assert ph._clean_host("https://eu.posthog.com/") == "https://eu.posthog.com"
    assert ph._clean_host("") == ph._DEFAULT_HOST
    assert ph._clean_host(None) == ph._DEFAULT_HOST


def test_clean_milestones_validates_dedups_and_caps():
    got = ph._clean_milestones(
        "onboarding_completed, api_key_invalid , onboarding_completed, $pageview")
    assert got == ["onboarding_completed", "api_key_invalid", "$pageview"]
    # a name with a quote / newline / semicolon is rejected outright
    assert ph._clean_milestones(["ok_name", "bad'; DROP", "second\nline"]) == ["ok_name"]
    assert len(ph._clean_milestones([f"evt_{i}" for i in range(100)])) == 40


def test_trend_thresholds():
    assert ph._trend(2, 10) == "down"
    assert ph._trend(20, 10) == "up"
    assert ph._trend(10, 10) == "flat"
    assert ph._trend(5, 0) == "up"
    assert ph._trend(0, 0) == "flat"


# ── load / save / available (fake Supabase) ──────────────────────────
class _Tbl:
    def __init__(self, store, name):
        self.store, self.name, self._f = store, name, {}

    def select(self, *a, **k):
        return self

    def eq(self, k, v):
        self._f[k] = v
        return self

    def limit(self, *a, **k):
        return self

    def upsert(self, row, **k):
        self.store.setdefault(self.name, []).append(row)
        self._last = row
        return self

    def delete(self):
        self._del = True
        return self

    def execute(self):
        rows = self.store.get(self.name, [])
        out = [r for r in rows if all(r.get(k) == v for k, v in self._f.items())]
        return type("R", (), {"data": out})()


class _SB:
    def __init__(self, store=None):
        self.store = store or {}

    def table(self, name):
        return _Tbl(self.store, name)


def test_load_none_when_no_row():
    assert ph.load("T1", _SB()) is None


def test_save_then_load_roundtrip(monkeypatch):
    sb = _SB()
    monkeypatch.setattr(ph, "_key", lambda *a, **k: "phx_stored")
    puts = {}
    monkeypatch.setattr("interpreter.vault_secrets.put",
                        lambda t, kind, creds, **kw: puts.update(creds) or "vault-1")
    cfg = ph.PostHogConfig(tenant_id="T1", host="us.posthog.com", project_id="42",
                           milestone_events=["a", "b"], status="active")
    ph.save(cfg, sb, api_key="phx_new")
    assert puts == {"api_key": "phx_new"}

    got = ph.load("T1", sb)
    assert got.project_id == "42"
    assert got.host == "https://us.posthog.com"
    assert got.milestone_events == ["a", "b"]
    assert got.has_credentials is True


def test_available_gates_on_project_and_key(monkeypatch):
    sb = _SB({"tenant_integrations": [
        {"tenant_id": "T1", "kind": "posthog",
         "config": {"project_id": "", "host": "https://us.posthog.com"},
         "status": "active", "vault_secret_id": None},
    ]})
    monkeypatch.setattr(ph, "_key", lambda *a, **k: None)
    ok, why = ph.available("T1", sb)
    assert ok is False and "project id" in why.lower()


# ── test_connection (stub requests) ──────────────────────────────────
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = str(body)

    def json(self):
        return self._body


def test_test_connection_ok(monkeypatch):
    monkeypatch.setattr(ph, "_key", lambda *a, **k: "phx")
    monkeypatch.setattr("requests.post",
                        lambda *a, **k: _Resp(200, {"columns": ["ok"], "results": [[1]]}))
    r = ph.test_connection("T1", _SB(), host="us.posthog.com", project_id="9")
    assert r["ok"] is True


def test_test_connection_needs_project():
    r = ph.test_connection("T1", _SB(), project_id="")
    assert r["ok"] is False and "project id" in r["detail"].lower()


def test_test_connection_http_error(monkeypatch):
    monkeypatch.setattr(ph, "_key", lambda *a, **k: "phx")
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(403, "forbidden"))
    r = ph.test_connection("T1", _SB(), project_id="9")
    assert r["ok"] is False and "403" in r["detail"]


# ── fetch_person_rollups (stub _query) ───────────────────────────────
def _stub_queries(monkeypatch, base, trend=None, milestones=None):
    calls = {"n": 0}

    def fake_query(tenant_id, sb, hogql, **kw):
        calls["n"] += 1
        if "count(DISTINCT toStartOfDay" in hogql:
            return base
        if "countIf(timestamp" in hogql:
            return trend or []
        if "event IN (" in hogql:
            return milestones or []
        return []

    monkeypatch.setattr(ph, "_query", fake_query)
    return calls


def _cfg(monkeypatch, milestones=None):
    monkeypatch.setattr(ph, "load", lambda t, sb: ph.PostHogConfig(
        tenant_id="T1", host="https://us.posthog.com", project_id="1",
        milestone_events=milestones or []))


def test_fetch_rollups_assembles_people(monkeypatch):
    _cfg(monkeypatch)
    _stub_queries(monkeypatch, base=[
        {"email": "Dana@Acme.com", "last_seen_at": "2026-09-05T10:00:00",
         "events_30d": 40, "active_days_30d": 9},
        {"email": "", "last_seen_at": "2026-09-05T10:00:00",
         "events_30d": 1, "active_days_30d": 1},
    ], trend=[{"email": "dana@acme.com", "recent": 5, "prior": 40}])

    out = ph.fetch_person_rollups("T1", _SB())
    assert len(out) == 1
    p = out[0]
    assert p.email == "dana@acme.com"          # lower-cased
    assert p.events_30d == 40 and p.active_days_30d == 9
    assert p.usage_trend == "down"             # 5 << 40


def test_fetch_rollups_since_filters_stale_people(monkeypatch):
    _cfg(monkeypatch)
    _stub_queries(monkeypatch, base=[
        {"email": "fresh@x.com", "last_seen_at": "2026-09-06T00:00:00",
         "events_30d": 3, "active_days_30d": 2},
        {"email": "stale@x.com", "last_seen_at": "2026-08-01T00:00:00",
         "events_30d": 3, "active_days_30d": 2},
    ])
    out = ph.fetch_person_rollups("T1", _SB(), since="2026-09-01T00:00:00")
    assert [p.email for p in out] == ["fresh@x.com"]


def test_fetch_rollups_attaches_milestones(monkeypatch):
    _cfg(monkeypatch, milestones=["api_key_invalid"])
    _stub_queries(monkeypatch,
                  base=[{"email": "u@x.com", "last_seen_at": "2026-09-05T00:00:00",
                         "events_30d": 10, "active_days_30d": 4}],
                  milestones=[{"email": "u@x.com", "name": "api_key_invalid",
                               "last_ts": "2026-09-05T09:00:00", "cnt": 4}])
    out = ph.fetch_person_rollups("T1", _SB())
    assert out[0].milestones == [
        {"name": "api_key_invalid", "last_ts": "2026-09-05T09:00:00", "count": 4}]


def test_fetch_rollups_empty_when_no_project(monkeypatch):
    monkeypatch.setattr(ph, "load", lambda t, sb: ph.PostHogConfig(tenant_id="T1", project_id=""))
    assert ph.fetch_person_rollups("T1", _SB()) == []
