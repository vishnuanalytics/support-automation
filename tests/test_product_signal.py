"""Phase 30 chunk 5 — the `product_signal` flow node (registry.h_product_signal).
No Neo4j: the driver is faked. Also covers the h_draft fold-in + the
builder._context exposure."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import case_memory
from interpreter.builder import _context
from interpreter.registry import h_product_signal

_NID = {"_node_id": "n1"}


class _Driver:
    def __init__(self, rows):
        self._rows = rows

    def execute_query(self, cypher, **kw):
        self._cypher, self._kw = cypher, kw
        return ([type("R", (), {"data": staticmethod(lambda r=r: r)})() for r in self._rows],
                None, [])


def _row(**over):
    base = {"identity_match": "email", "last_seen_at": "2026-09-05T10:00:00",
            "events_30d": 42, "active_days_30d": 9, "usage_trend": "down",
            "features": [{"name": "api_key_invalid", "last_ts": "2026-09-05T09:00:00", "count": 4},
                         {"name": "export_started", "last_ts": "2026-09-01T00:00:00", "count": 1},
                         None],
            "account_id": "001ACME", "account_active_users_30d": 12,
            "account_usage_trend": "flat"}
    base.update(over)
    return base


def _state(**over):
    s = {"tenant_id": "T1", "case": {"contact": {"email": "Dana@Acme.com"}}}
    s.update(over)
    return s


# ── no-op paths (never block a run) ─────────────────────────────────
def test_no_sender_email_is_unavailable():
    out = h_product_signal({"tenant_id": "T1", "case": {}}, _NID)
    assert out["product_signal"] == {"available": False, "reason": "no sender email"}


def test_no_neo4j_is_unavailable(monkeypatch):
    monkeypatch.delenv("NEO4J_URI", raising=False)
    out = h_product_signal(_state(), _NID)
    assert out["product_signal"]["available"] is False


def test_no_contact_row_is_unavailable(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://x")
    monkeypatch.setattr(case_memory, "_driver_or_none", lambda: _Driver([]))
    out = h_product_signal(_state(), _NID)
    assert out["product_signal"] == {"available": False,
                                     "reason": "no product activity for this user",
                                     "email": "dana@acme.com"}


def test_contact_without_rollup_is_unavailable(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://x")
    monkeypatch.setattr(case_memory, "_driver_or_none",
                        lambda: _Driver([_row(last_seen_at=None)]))
    out = h_product_signal(_state(), _NID)
    assert out["product_signal"]["available"] is False


def test_query_error_is_swallowed(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://x")

    class _Boom:
        def execute_query(self, *a, **k):
            raise RuntimeError("neo4j down")

    monkeypatch.setattr(case_memory, "_driver_or_none", lambda: _Boom())
    out = h_product_signal(_state(), _NID)
    assert out["product_signal"]["available"] is False


# ── the happy path ─────────────────────────────────────────────────
def test_assembles_the_signal(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://x")
    drv = _Driver([_row()])
    monkeypatch.setattr(case_memory, "_driver_or_none", lambda: drv)

    out = h_product_signal(_state(), _NID)
    p = out["product_signal"]
    assert p["available"] is True
    assert p["email"] == "dana@acme.com"                 # lower-cased
    assert p["events_30d"] == 42 and p["usage_trend"] == "down"
    # None feature filtered, sorted newest-first
    assert [f["name"] for f in p["recent_features"]] == ["api_key_invalid", "export_started"]
    assert p["account"] == {"account_id": "001ACME",
                            "active_users_30d": 12, "usage_trend": "flat"}
    # the query was tenant-scoped
    assert drv._kw["tenant_id"] == "T1" and drv._kw["email"] == "dana@acme.com"


def test_prefers_sender_email_over_case_contact(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://x")
    drv = _Driver([_row()])
    monkeypatch.setattr(case_memory, "_driver_or_none", lambda: drv)
    h_product_signal(_state(sender={"email": "boss@acme.com"}), _NID)
    assert drv._kw["email"] == "boss@acme.com"


def test_no_account_edge_gives_null_account(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://x")
    monkeypatch.setattr(case_memory, "_driver_or_none",
                        lambda: _Driver([_row(account_id=None)]))
    out = h_product_signal(_state(), _NID)
    assert out["product_signal"]["account"] is None


# ── wiring ─────────────────────────────────────────────────────────
def test_context_exposes_product_signal():
    ctx = _context({"product_signal": {"available": True, "usage_trend": "down"}})
    assert ctx["product_signal"] == {"available": True, "usage_trend": "down"}
    assert _context({})["product_signal"] == {}


def test_draft_folds_the_signal_into_the_prompt(monkeypatch):
    from interpreter import registry

    captured = {}
    monkeypatch.setattr(registry.llm, "complete",
                        lambda **kw: captured.update(kw) or '{"reply": "hi", "confidence": 0.6}')
    monkeypatch.setattr(registry.groundedness, "check", lambda *a, **k: {"score": 0.5, "backend": "x", "unsupported": []})
    monkeypatch.setattr(registry.integrity, "check_many",
                        lambda *a, **k: {"draft": {"relation": "neutral", "flagged": False, "backend": "x"},
                                         "inbound": {"relation": "neutral", "flagged": False, "backend": "x"}})
    state = {"case": {"subject": "s", "body": "b"}, "retrieval": [],
             "product_signal": {"available": True, "last_seen_at": "2026-09-05",
                                "events_30d": 42, "active_days_30d": 9, "usage_trend": "down",
                                "recent_features": [{"name": "api_key_invalid", "count": 4}],
                                "account": {"active_users_30d": 12, "usage_trend": "flat"}}}
    registry.h_draft(state, {"_node_id": "n"})
    assert "Product activity for this user" in captured["user"]
    assert "api_key_invalid (x4)" in captured["user"]


def test_draft_ignores_an_unavailable_signal(monkeypatch):
    from interpreter import registry
    captured = {}
    monkeypatch.setattr(registry.llm, "complete",
                        lambda **kw: captured.update(kw) or '{"reply": "hi", "confidence": 0.6}')
    monkeypatch.setattr(registry.groundedness, "check", lambda *a, **k: {"score": 0.5, "backend": "x", "unsupported": []})
    monkeypatch.setattr(registry.integrity, "check_many",
                        lambda *a, **k: {"draft": {"relation": "neutral", "flagged": False, "backend": "x"},
                                         "inbound": {"relation": "neutral", "flagged": False, "backend": "x"}})
    registry.h_draft({"case": {"subject": "s", "body": "b"}, "retrieval": [],
                      "product_signal": {"available": False}}, {"_node_id": "n"})
    assert "Product activity for this user" not in captured["user"]
