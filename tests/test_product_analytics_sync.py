"""Phase 30 chunk 4 — ingestion/product_analytics_sync.py: PostHog rollups
-> Neo4j (:Contact) + identity resolution. No network; the PostHog fetch
and the Neo4j driver are faked."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ingestion import product_analytics_sync as pas
from interpreter import posthog as ph


# ── fakes ────────────────────────────────────────────────────────────
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
        self.store.setdefault("_upserts", []).append((self.name, row))
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


class _Driver:
    def __init__(self, identity_return=None):
        self.calls = []
        self._identity_return = identity_return or []

    def execute_query(self, cypher, **params):
        self.calls.append((cypher, params))
        rows = self._identity_return if "ct.identity_match AS m" in cypher else []
        return (rows, None, ["m"])


def _rollup(email, trend="flat", ms=None):
    return ph.PersonRollup(email=email, last_seen_at="2026-09-06T00:00:00",
                           events_30d=10, active_days_30d=3, usage_trend=trend,
                           milestones=ms or [])


# ── _active_posthog_tenants ─────────────────────────────────────────
def test_active_tenants_filters_by_kind_and_status():
    sb = _SB({"tenant_integrations": [
        {"tenant_id": "T1", "kind": "posthog", "status": "active"},
        {"tenant_id": "T2", "kind": "posthog", "status": "inactive"},   # dropped by .eq
        {"tenant_id": "T3", "kind": "slack", "status": "active"},        # dropped by .eq
    ]})
    # the fake applies both .eq filters, so only T1 comes back
    assert pas._active_posthog_tenants(sb, None) == ["T1"]
    assert pas._active_posthog_tenants(sb, "T1") == ["T1"]
    assert pas._active_posthog_tenants(sb, "TX") == []


# ── _sync_one ──────────────────────────────────────────────────────
def test_sync_one_merges_rollups_and_computes_coverage(monkeypatch):
    monkeypatch.setattr(ph, "fetch_person_rollups",
                        lambda tid, sb, **kw: [
                            _rollup("a@x.com", ms=[{"name": "m1", "last_ts": "t", "count": 2}]),
                            _rollup("b@y.com"),
                            _rollup("c@z.com"),
                        ])
    sb = _SB()
    drv = _Driver(identity_return=[{"m": "email"}, {"m": "domain"}, {"m": "none"}])
    res = pas._sync_one(sb, drv, "T1", dry=False)

    assert res["contacts"] == 3
    assert res["coverage_pct"] == round(100 * 2 / 3, 1)      # 2 of 3 matched
    assert res["by_match"] == {"email": 1, "domain": 1, "none": 1}

    # three Cypher passes: rollup MERGE, identity, account rollup
    kinds = [c[0] for c in drv.calls]
    assert any("MERGE (ct:Contact {email: r.email" in c for c in kinds)
    assert any("ct.identity_match" in c for c in kinds)
    assert any("[:AT_ACCOUNT]-(ct:Contact)" in c for c in kinds)
    # rollup payload carried the milestone through
    rollup_call = next(c for c in drv.calls if "r.milestones" in c[0])
    assert rollup_call[1]["rollups"][0]["milestones"] == [{"name": "m1", "last_ts": "t", "count": 2}]

    # state upserted with the high-water mark + coverage
    up = [r for (n, r) in sb.store["_upserts"] if n == "graph_sync_state"]
    assert up and up[0]["scope"] == "product_analytics:T1"
    assert up[0]["coverage_pct"] == res["coverage_pct"]
    assert up[0]["last_modified"] == "2026-09-06T00:00:00"
    assert up[0]["contacts_synced"] == 3


def test_sync_one_dry_run_touches_no_graph(monkeypatch):
    monkeypatch.setattr(ph, "fetch_person_rollups",
                        lambda tid, sb, **kw: [_rollup("a@x.com")])
    sb = _SB()
    res = pas._sync_one(sb, None, "T1", dry=True)
    assert res["dry_run"] is True and res["contacts"] == 1
    assert "_upserts" not in sb.store          # no state write on a dry run


def test_sync_one_no_rollups_is_a_noop(monkeypatch):
    monkeypatch.setattr(ph, "fetch_person_rollups", lambda tid, sb, **kw: [])
    res = pas._sync_one(_SB(), _Driver(), "T1", dry=False)
    assert res["contacts"] == 0 and res["note"] == "no rollups"


# ── sync (orchestrator) ────────────────────────────────────────────
def test_sync_isolates_a_failing_tenant(monkeypatch):
    monkeypatch.setattr(pas, "_sb", lambda: _SB({"tenant_integrations": [
        {"tenant_id": "OK", "kind": "posthog", "status": "active"},
        {"tenant_id": "BAD", "kind": "posthog", "status": "active"},
    ]}))
    monkeypatch.setattr(pas, "_driver", lambda: _Driver())

    def _one(sb, driver, tid, *, dry):
        if tid == "BAD":
            raise RuntimeError("posthog 500")
        return {"tenant_id": tid, "contacts": 1}

    monkeypatch.setattr(pas, "_sync_one", _one)
    out = pas.sync()
    assert {r["tenant_id"] for r in out} == {"OK", "BAD"}
    assert any(r.get("error") for r in out if r["tenant_id"] == "BAD")


def test_sync_noop_when_no_posthog_tenants(monkeypatch):
    monkeypatch.setattr(pas, "_sb", lambda: _SB({"tenant_integrations": []}))
    assert pas.sync() == []


# ── sweep wrapper ─────────────────────────────────────────────────
def test_sweep_wrapper_delegates(monkeypatch):
    from interpreter import sweeps
    monkeypatch.setattr("ingestion.product_analytics_sync.sync",
                        lambda **kw: [{"tenant_id": "T1", "contacts": 4}])
    out = sweeps.product_analytics_sync(_SB(), dry_run=True)
    assert out["tenants"] == 1 and out["results"][0]["contacts"] == 4
