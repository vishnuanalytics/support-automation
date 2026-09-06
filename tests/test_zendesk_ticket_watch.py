"""Phase 31 chunk 1 — ingestion/zendesk_ticket_watch.py: poll new Zendesk
tickets for `case_connector=zendesk` tenants and enqueue run_flow. No
network; zendesk.* and jobs.enqueue are faked."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ingestion import zendesk_ticket_watch as ztw


class _Tbl:
    def __init__(self, rows):
        self._rows = rows
        self._f = {}

    def select(self, *a, **k):
        return self

    def eq(self, k, v):
        self._f[k] = v
        return self

    def execute(self):
        out = [r for r in self._rows if all(r.get(k) == v for k, v in self._f.items())]
        return type("R", (), {"data": out})()


class _SB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Tbl(list(self.tables.get(name, [])))


def _tables(**over):
    t = {
        "tenants": [{"tenant_id": "T1", "case_connector": "zendesk"},
                    {"tenant_id": "T2", "case_connector": "salesforce"}],
        "tenant_integrations": [{"tenant_id": "T1", "kind": "zendesk", "status": "active"}],
        "flows": [{"flow_id": "F1", "tenant_id": "T1", "status": "published", "sf_entry": True}],
    }
    t.update(over)
    return t


# ── tenant / flow resolution ───────────────────────────────────────
def test_only_zendesk_connector_tenants_with_an_active_integration():
    sb = _SB(_tables())
    assert ztw._zendesk_tenants(sb, None) == ["T1"]
    # T2 is salesforce; a T1 with no active integration drops out too
    sb2 = _SB(_tables(tenant_integrations=[{"tenant_id": "T1", "kind": "zendesk",
                                            "status": "inactive"}]))
    assert ztw._zendesk_tenants(sb2, None) == []


def test_resolve_flow_prefers_sf_entry_then_sole_published():
    assert ztw._resolve_flow(_SB(_tables()), "T1") == "F1"
    # no sf_entry, one published -> that one
    sb = _SB(_tables(flows=[{"flow_id": "F9", "tenant_id": "T1", "status": "published",
                             "sf_entry": False}]))
    assert ztw._resolve_flow(sb, "T1") == "F9"
    # two published, none marked -> ambiguous -> None
    sb = _SB(_tables(flows=[
        {"flow_id": "A", "tenant_id": "T1", "status": "published", "sf_entry": False},
        {"flow_id": "B", "tenant_id": "T1", "status": "published", "sf_entry": False},
    ]))
    assert ztw._resolve_flow(sb, "T1") is None


# ── tick ───────────────────────────────────────────────────────────
def test_tick_enqueues_one_run_flow_per_ticket(monkeypatch):
    monkeypatch.setattr(ztw.zendesk, "list_new_tickets",
                        lambda tid, sb, **kw: [{"id": 1}, {"id": 2}])
    monkeypatch.setattr(ztw.zendesk, "ticket_as_case",
                        lambda t, tid, sb: {"sf_id": str(t["id"]), "subject": "s",
                                            "body": "b", "channel": "zendesk"})
    enq = []
    monkeypatch.setattr(ztw.jobs, "enqueue",
                        lambda kind, payload, **kw: enq.append((kind, payload, kw)) or f"job-{payload['case']['sf_id']}")

    n = ztw.tick(_SB(_tables()), lookback_min=90)
    assert n == 2
    assert {e[0] for e in enq} == {"run_flow"}
    p0 = enq[0][1]
    assert p0["flow_id"] == "F1" and p0["trigger"] == "zendesk_ticket"
    assert p0["idempotency_key"] == "zd:T1:1"
    assert enq[0][2]["dedupe_key"] == "zd:T1:1" and enq[0][2]["tenant_id"] == "T1"


def test_tick_skips_a_tenant_with_an_ambiguous_flow(monkeypatch):
    sb = _SB(_tables(flows=[
        {"flow_id": "A", "tenant_id": "T1", "status": "published", "sf_entry": False},
        {"flow_id": "B", "tenant_id": "T1", "status": "published", "sf_entry": False},
    ]))
    called = []
    monkeypatch.setattr(ztw.zendesk, "list_new_tickets",
                        lambda *a, **k: called.append(1) or [])
    assert ztw.tick(sb) == 0
    assert called == []          # never even polled — no flow to run through


def test_tick_no_zendesk_tenants_is_zero(monkeypatch):
    sb = _SB(_tables(tenants=[{"tenant_id": "T2", "case_connector": "salesforce"}]))
    assert ztw.tick(sb) == 0


def test_tick_dedupe_returns_none_from_enqueue(monkeypatch):
    monkeypatch.setattr(ztw.zendesk, "list_new_tickets", lambda *a, **k: [{"id": 1}])
    monkeypatch.setattr(ztw.zendesk, "ticket_as_case",
                        lambda t, tid, sb: {"sf_id": "1"})
    monkeypatch.setattr(ztw.jobs, "enqueue", lambda *a, **k: None)   # already queued
    assert ztw.tick(_SB(_tables())) == 0
