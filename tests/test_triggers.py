"""P5b — trigger/webhook adapters + the `trigger` entry node."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import triggers
from interpreter.builder import build_graph, initial_state
from interpreter.registry import h_trigger


# ── webhook_context ────────────────────────────────────────────────────
def test_webhook_context_wraps_and_stamps():
    out = triggers.webhook_context({"plan": "free", "email": "a@b.com"})
    ctx = out["context"]
    assert ctx["plan"] == "free" and ctx["email"] == "a@b.com"
    assert ctx["_trigger"] == "webhook" and ctx["_received_at"]


def test_webhook_context_clips_huge_and_deep_payloads():
    ctx = triggers.webhook_context({"big": "x" * 20000, "deep": {"a": {"b": {"c": {"d": {}}}}}})["context"]
    assert len(ctx["big"]) == 8000
    assert isinstance(ctx["deep"], dict)


def test_webhook_context_tolerates_a_non_dict_body():
    assert triggers.webhook_context(None)["context"]["_trigger"] == "webhook"


# ── h_trigger node ────────────────────────────────────────────────────
def test_h_trigger_maps_defaults_and_flags_missing():
    state = {"context": {"customer_email": "a@b.com", "_trigger": "webhook"}}
    out = h_trigger(state, {"_node_id": "t", "map": {"customer_email": "email"},
                            "defaults": {"plan": "free"}, "required": ["email", "account_id"]})
    ctx = out["context"]
    assert ctx["email"] == "a@b.com" and "customer_email" not in ctx
    assert ctx["plan"] == "free"
    assert ctx["_missing"] == ["account_id"]
    assert "MISSING" in out["trace"][0]["summary"]


def test_h_trigger_passthrough_when_complete():
    out = h_trigger({"context": {"email": "a@b.com", "plan": "pro"}},
                    {"_node_id": "t", "required": ["email"]})
    assert "_missing" not in out["context"]


# ── P5c: retrieve / classify / draft read the generic payload ─────────
def test_run_text_prefers_case_then_context():
    from interpreter.registry import _run_text
    # a real Case wins
    assert _run_text({"case": {"subject": "S", "body": "B"},
                      "context": {"query": "ignored"}}) == ("S", "B")
    # no Case -> the generic payload, common key names
    assert _run_text({"context": {"title": "T", "text": "hello"}}) == ("T", "hello")
    assert _run_text({"context": {"question": "how do webhooks work"}})[1] == "how do webhooks work"
    # last resort: dump non-underscore fields
    s, b = _run_text({"context": {"plan": "free", "region": "EU", "_trigger": "webhook"}})
    assert "plan: free" in b and "region: EU" in b and "_trigger" not in b


def test_h_retrieve_builds_its_query_from_context(monkeypatch):
    from interpreter import registry
    seen = {}
    monkeypatch.setattr(registry, "hybrid_retrieve",
                        lambda q, **kw: (seen.__setitem__("q", q) or ([], 0.0)))
    out = registry.h_retrieve({"context": {"query": "reset my api token"}},
                              {"_node_id": "r", "source": ["supabase"]})
    assert seen["q"] == "reset my api token"
    assert out["query"] == "reset my api token"


# ── end to end: a Case-less flow driven by a webhook ──────────────────
def _webhook_flow():
    return {
        "flow_id": "f", "tenant_id": "t", "team": "support", "name": "wh",
        "version": 1, "status": "draft",
        "nodes": [
            {"node_id": "t", "type": "trigger", "label": "in",
             "config": {"required": ["email"], "defaults": {"plan": "free"}}},
            {"node_id": "bad", "type": "_t_end", "label": "rejected", "config": {}},
            {"node_id": "ok", "type": "_t_end", "label": "accepted", "config": {}},
        ],
        "edges": [
            {"edge_id": "e1", "source_node_id": "t", "target_node_id": "bad",
             "condition": {"if": "input._missing"}},
            {"edge_id": "e2", "source_node_id": "t", "target_node_id": "ok", "condition": {}},
        ],
    }


def test_webhook_flow_routes_on_the_trigger_result():
    import tests.test_interpreter  # noqa: F401 — registers _t_end
    g = build_graph(_webhook_flow())

    good = triggers.webhook_context({"email": "a@b.com"})["context"]
    f1 = g.invoke(initial_state(_webhook_flow(), context=good))
    assert f1["outcome"]["action"] == "accepted"
    assert f1["context"]["plan"] == "free"          # default applied by h_trigger

    bad = triggers.webhook_context({"name": "no email"})["context"]
    f2 = g.invoke(initial_state(_webhook_flow(), context=bad))
    assert f2["outcome"]["action"] == "rejected"
    assert f2["context"]["_missing"] == ["email"]


# ── worker + API wiring ──────────────────────────────────────────────
def test_worker_run_flow_threads_context(monkeypatch):
    from api import worker
    seen = {}

    class _G:
        def invoke(self, st):
            seen["state"] = st
            return {"context": st["context"], "outcome": {"action": "done"}, "case": {}}

    monkeypatch.setattr(worker, "load_flow",
                        lambda **k: {"flow_id": "f", "tenant_id": "t", "team": "support"})
    monkeypatch.setattr(worker, "build_graph", lambda flow: _G())
    monkeypatch.setattr(worker, "record_run", lambda *a, **k: "run-1")
    out = worker._run_flow({"flow_id": "f", "context": {"plan": "free"}}, sb=None)
    assert seen["state"]["context"]["plan"] == "free"
    assert seen["state"]["case"] == {}
    assert out["run_id"] == "run-1"
