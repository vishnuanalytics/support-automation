"""interpreter/case_cluster.py — RootCause-label → Issue clustering.
Offline: the Neo4j driver is a fake that captures the MERGE call."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import case_cluster as cc, case_memory


class _FakeDriver:
    def __init__(self, labels):
        self._labels = labels
        self.calls = []

    def execute_query(self, cypher, **params):
        self.calls.append((cypher, params))
        if "RETURN DISTINCT rc.label" in cypher:
            return type("R", (), {"records": [{"label": x} for x in self._labels]})()
        return type("R", (), {"records": [{"issues": 2, "links": 7}]})()


def test_issue_key_is_stable_and_tenant_scoped():
    a = cc.issue_key("T1", "Salesforce sync failure")
    assert a == cc.issue_key("T1", "Salesforce sync failure")     # stable across runs
    assert a != cc.issue_key("T2", "Salesforce sync failure")     # per-tenant
    assert a.startswith("auto:") and len(a) == len("auto:") + 16


def test_cluster_builds_pairs_and_merges(monkeypatch):
    fake = _FakeDriver(["Salesforce sync failure", "webhook timeout", None])
    monkeypatch.setattr(case_memory, "_driver_or_none", lambda: fake)
    out = cc.cluster_issues("T1", min_cases=2)
    assert out == {"issues": 2, "links": 7}

    merge = next(c for c in fake.calls if "MERGE (iss:Issue" in c[0])
    pairs = merge[1]["pairs"]
    assert {p["label"] for p in pairs} == {"Salesforce sync failure", "webhook timeout"}
    assert all(p["issue_key"] == cc.issue_key("T1", p["label"]) for p in pairs)
    assert merge[1]["min_cases"] == 2
    for cy, params in fake.calls:
        assert "tenant_id: $tenant_id" in cy and params["tenant_id"] == "T1"


def test_cluster_no_graph_is_zero(monkeypatch):
    monkeypatch.setattr(case_memory, "_driver_or_none", lambda: None)
    assert cc.cluster_issues("T1") == {"issues": 0, "links": 0}


def test_cluster_no_labels_skips_the_merge(monkeypatch):
    fake = _FakeDriver([])
    monkeypatch.setattr(case_memory, "_driver_or_none", lambda: fake)
    assert cc.cluster_issues("T1") == {"issues": 0, "links": 0}
    assert not any("MERGE (iss:Issue" in c[0] for c in fake.calls)


def test_cluster_swallows_driver_errors(monkeypatch):
    class _Boom:
        def execute_query(self, *a, **k):
            raise RuntimeError("neo4j down")

    monkeypatch.setattr(case_memory, "_driver_or_none", lambda: _Boom())
    assert cc.cluster_issues("T1") == {"issues": 0, "links": 0}
