"""Response Quality Feedback Loop chunk E — interpreter/gate_tuning.py
(find_proposals / raise_proposal / apply_threshold_change) and
interpreter/sweeps.py::gate_tuning_sweep."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import gate_tuning, sweeps


# ── find_proposals ───────────────────────────────────────────────────────
class _RunsQuery:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_a):
        return self

    def gte(self, *_a):
        return self

    def order(self, *_a, **_kw):
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    @property
    def not_(self):
        return self

    def is_(self, *_a):
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


class _RunsSB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "runs"
        return _RunsQuery(list(self.rows))


def _gate_trace(*, score, threshold, tier="basic", passed=False, forced=None, node_id="n1"):
    data = {"score": score, "threshold": threshold, "tier": tier, "pass": passed}
    if forced:
        data["forced_escalation"] = forced
    return [{"node_id": node_id, "type": "confidence_gate", "summary": "x", "data": data}]


def _run(run_id, *, category="policy", severity=0.6, score=0.4, threshold=0.5,
        tier="basic", passed=False, forced=None, tenant_id="t1", flow_id="f1", node_id="n1"):
    return {
        "run_id": run_id, "tenant_id": tenant_id, "flow_id": flow_id,
        "trace": _gate_trace(score=score, threshold=threshold, tier=tier,
                             passed=passed, forced=forced, node_id=node_id),
        "correction_analysis": {"category": category, "severity": severity, "summary": "x"},
    }


def test_find_proposals_query_failure_returns_empty():
    class _Broken:
        def table(self, name):
            class _Q:
                def select(self, *_a): return self
                @property
                def not_(self): return self
                def is_(self, *_a): return self
                def gte(self, *_a): return self
                def order(self, *_a, **_kw): return self
                def limit(self, *_a): return self
                def execute(self): raise RuntimeError("db down")
            return _Q()
    assert gate_tuning.find_proposals(_Broken()) == []


def test_find_proposals_skips_unknown_category():
    rows = [_run(f"r{i}", category="unknown", severity=0.9) for i in range(6)]
    assert gate_tuning.find_proposals(_RunsSB(rows)) == []


def test_find_proposals_skips_passed_or_forced_gates():
    rows = ([_run(f"pass{i}", passed=True) for i in range(6)]
            + [_run(f"forced{i}", forced="topic 'billing'") for i in range(6)])
    assert gate_tuning.find_proposals(_RunsSB(rows)) == []


def test_find_proposals_requires_min_samples():
    rows = [_run(f"r{i}", category="none", severity=0.0) for i in range(3)]
    out = gate_tuning.find_proposals(_RunsSB(rows), min_samples=5)
    assert out == []


def test_find_proposals_requires_majority_unnecessary():
    # 3 "unnecessary" (none/low severity) + 4 genuinely significant -> < 50%
    rows = ([_run(f"u{i}", category="none", severity=0.0) for i in range(3)]
            + [_run(f"s{i}", category="policy", severity=0.9) for i in range(4)])
    out = gate_tuning.find_proposals(_RunsSB(rows), min_samples=5)
    assert out == []


def test_find_proposals_proposes_a_bounded_lower_threshold():
    rows = [_run(f"r{i}", category="none", severity=0.0, score=0.45, threshold=0.5)
            for i in range(6)]
    out = gate_tuning.find_proposals(_RunsSB(rows), min_samples=5)
    assert len(out) == 1
    p = out[0]
    assert p["tenant_id"] == "t1" and p["flow_id"] == "f1" and p["node_id"] == "n1"
    assert p["tier"] == "basic"
    assert p["current_threshold"] == 0.5
    assert p["suggested_threshold"] < 0.5
    assert p["suggested_threshold"] >= 0.5 - gate_tuning.MAX_STEP - 1e-9
    assert p["sample_size"] == 6 and p["unnecessary_count"] == 6
    assert len(p["evidence_run_ids"]) <= 5


def test_find_proposals_never_goes_below_floor_threshold():
    # a huge average gap would otherwise blow past the floor
    rows = [_run(f"r{i}", category="none", severity=0.0, score=0.01, threshold=0.22)
            for i in range(6)]
    out = gate_tuning.find_proposals(_RunsSB(rows), min_samples=5)
    assert len(out) == 1
    assert out[0]["suggested_threshold"] >= gate_tuning.FLOOR_THRESHOLD


def test_find_proposals_buckets_separately_per_tenant_flow_node_tier():
    rows = ([_run(f"a{i}", category="none", severity=0.0, tenant_id="t1", tier="basic")
            for i in range(6)]
            + [_run(f"b{i}", category="none", severity=0.0, tenant_id="t2", tier="basic")
               for i in range(6)])
    out = gate_tuning.find_proposals(_RunsSB(rows), min_samples=5)
    assert {p["tenant_id"] for p in out} == {"t1", "t2"}
    assert len(out) == 2


# ── raise_proposal ───────────────────────────────────────────────────────
class _ARQuery:
    def __init__(self, store, existing_rows):
        self.store = store
        self.rows = existing_rows
        self._insert_payload = None
        self._update_fields = None

    def select(self, *_a):
        return self

    def eq(self, field, value):
        self.rows = [r for r in self.rows if r.get(field) == value]
        return self

    def insert(self, payload):
        self._insert_payload = payload
        return self

    def update(self, fields):
        self._update_fields = fields
        return self

    def execute(self):
        if self._insert_payload is not None:
            row = {"id": f"ar{len(self.store)+1}", **self._insert_payload}
            self.store.append(row)
            return type("R", (), {"data": [row]})()
        if self._update_fields is not None:
            for r in self.rows:
                r.update(self._update_fields)
            return type("R", (), {"data": self.rows})()
        return type("R", (), {"data": self.rows})()


class _ARSB:
    def __init__(self, existing=None):
        self.store = list(existing or [])

    def table(self, name):
        assert name == "action_requests"
        return _ARQuery(self.store, list(self.store))


_PROPOSAL = {"tenant_id": "t1", "flow_id": "f1", "node_id": "n1", "tier": "basic",
            "current_threshold": 0.5, "suggested_threshold": 0.45,
            "sample_size": 6, "unnecessary_count": 6, "evidence_run_ids": ["r1"]}


def test_raise_proposal_inserts_with_expected_payload_shape():
    sb = _ARSB()
    ar = gate_tuning.raise_proposal(sb, _PROPOSAL)
    assert ar is not None
    p = ar["payload"]
    assert p["title"].startswith("Lower the basic confidence-gate threshold")
    assert "0.50" in p["title"] and "0.45" in p["title"]
    assert "LOWERING" in p["rationale"]
    assert "Tier: basic" in p["body_md"]
    assert ar["kind"] == "gate_tuning" and ar["status"] == "pending"


def test_raise_proposal_dedupes_against_a_pending_match():
    existing = [{"id": "ar1", "tenant_id": "t1", "kind": "gate_tuning", "status": "pending",
                "payload": {"flow_id": "f1", "node_id": "n1", "tier": "basic"}}]
    sb = _ARSB(existing)
    assert gate_tuning.raise_proposal(sb, _PROPOSAL) is None
    assert len(sb.store) == 1   # nothing new inserted


def test_raise_proposal_query_failure_returns_none():
    class _Broken:
        def table(self, name):
            class _Q:
                def select(self, *_a): return self
                def eq(self, *_a): return self
                def execute(self): raise RuntimeError("db down")
            return _Q()
    assert gate_tuning.raise_proposal(_Broken(), _PROPOSAL) is None


# ── apply_threshold_change ───────────────────────────────────────────────
class _FlowNodesQuery:
    def __init__(self, node):
        self.node = node
        self._update = None

    def select(self, *_a):
        return self

    def eq(self, field, value):
        if self.node and self.node.get(field) != value:
            self.node = None
        return self

    def update(self, fields):
        self._update = fields
        return self

    def execute(self):
        if self._update is not None:
            if self.node:
                self.node.update(self._update)
            return type("R", (), {"data": None})()
        return type("R", (), {"data": [self.node] if self.node else []})()


class _ApplySB:
    def __init__(self, node=None, ar_store=None):
        self.node = node
        self.ar_store = ar_store if ar_store is not None else []

    def table(self, name):
        if name == "flow_nodes":
            return _FlowNodesQuery(self.node)
        assert name == "action_requests"
        return _ARQuery(self.ar_store, list(self.ar_store))


def _ar_row(*, status="approved", result=None, tier="basic", suggested=0.45, node_id="n1"):
    return {"id": "ar1", "status": status, "result": result,
            "payload": {"node_id": node_id, "tier": tier, "suggested_threshold": suggested}}


def test_apply_threshold_change_skips_when_not_approved():
    out = gate_tuning.apply_threshold_change(_ApplySB(), _ar_row(status="pending"))
    assert out == {"skipped": "status=pending"}


def test_apply_threshold_change_is_idempotent_when_already_applied():
    out = gate_tuning.apply_threshold_change(
        _ApplySB(), _ar_row(result={"applied": True, "new_threshold": 0.45}))
    assert out["idempotent_skip"] is True and out["new_threshold"] == 0.45


def test_apply_threshold_change_errors_cleanly_when_node_is_gone():
    out = gate_tuning.apply_threshold_change(_ApplySB(node=None), _ar_row())
    assert out == {"error": "node gone"}


def test_apply_threshold_change_merges_into_existing_tier_overrides():
    node = {"node_id": "n1", "config": {"default_threshold": 0.35,
                                        "tier_overrides": {"enterprise": 0.6}}}
    sb = _ApplySB(node=node)
    out = gate_tuning.apply_threshold_change(sb, _ar_row(tier="basic", suggested=0.45))
    assert out == {"applied": True, "node_id": "n1", "tier": "basic", "new_threshold": 0.45}
    assert node["config"]["tier_overrides"] == {"enterprise": 0.6, "basic": 0.45}
    assert node["config"]["default_threshold"] == 0.35   # untouched


def test_apply_threshold_change_stamps_result_on_action_request():
    node = {"node_id": "n1", "config": {}}
    ar_store = [{"id": "ar1", "tenant_id": "t1", "kind": "gate_tuning"}]
    sb = _ApplySB(node=node, ar_store=ar_store)
    gate_tuning.apply_threshold_change(sb, _ar_row())
    assert ar_store[0].get("result", {}).get("applied") is True


# ── gate_tuning_sweep ─────────────────────────────────────────────────────
def test_gate_tuning_sweep_raises_each_qualifying_proposal(monkeypatch):
    monkeypatch.setattr(gate_tuning, "find_proposals", lambda sb, **k: [_PROPOSAL, {**_PROPOSAL, "tier": "premium"}])
    raised = []
    monkeypatch.setattr(gate_tuning, "raise_proposal", lambda sb, p: raised.append(p) or {"id": "arX"})
    out = sweeps.gate_tuning_sweep(object(), dry_run=False)
    assert out == {"proposals_found": 2, "raised": 2, "dry_run": False}
    assert len(raised) == 2


def test_gate_tuning_sweep_dry_run_raises_nothing(monkeypatch):
    monkeypatch.setattr(gate_tuning, "find_proposals", lambda sb, **k: [_PROPOSAL])
    def _boom(sb, p):
        raise AssertionError("dry run must not raise a proposal")
    monkeypatch.setattr(gate_tuning, "raise_proposal", _boom)
    out = sweeps.gate_tuning_sweep(object(), dry_run=True)
    assert out == {"proposals_found": 1, "raised": 0, "dry_run": True}


def test_gate_tuning_sweep_counts_only_successful_raises(monkeypatch):
    monkeypatch.setattr(gate_tuning, "find_proposals", lambda sb, **k: [_PROPOSAL, _PROPOSAL])
    monkeypatch.setattr(gate_tuning, "raise_proposal", lambda sb, p: None)   # both deduped
    out = sweeps.gate_tuning_sweep(object(), dry_run=False)
    assert out == {"proposals_found": 2, "raised": 0, "dry_run": False}
