"""Phase 19b / 19c -- offline tests for LLM-assisted flow authoring.

These run against the deterministic `llm` stub (no GROQ_API_KEY needed); a
real-Groq check lives in the integration set.
"""

from __future__ import annotations

import os

import pytest

from interpreter.builder import build_graph
from interpreter.flows.assist import assist_edit, assist_generate
from interpreter.flows.flow_candidate import assemble_candidate
from interpreter.flows.flow_diff import diff_graphs
from interpreter.flows.validate_flow import Flow, check_flow

_DEFAULTS = {
    "confidence_gate": {"default_threshold": 0.5,
                        "tier_overrides": {"basic": 0.5, "premium": 0.55, "enterprise": 0.6}},
    "retrieve": {"top_k": 5},
}


@pytest.fixture
def stub_llm(monkeypatch):
    """Force the deterministic `llm` stub -- importing `interpreter` pulls in
    `.env`, so a local GROQ_API_KEY would otherwise make these hit the network."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _flow(res):
    return {
        "flow_id": "f", "tenant_id": "t", "team": "support", "name": "n",
        "version": 1, "status": "draft", "nodes": res["nodes"], "edges": res["edges"],
    }


# ── assemble_candidate ────────────────────────────────────────────────
def test_assemble_coerces_unknown_type_and_flags_it():
    res = assemble_candidate(
        [{"key": "a", "type": "make_believe", "label": "Weird"},
         {"key": "b", "type": "auto_reply", "label": "Send"}],
        [{"source": "a", "target": "b"}],
    )
    assert res["nodes"][0]["type"] == "draft"
    assert any("not a known node type" in w for w in res["warnings"])
    assert res["errors"] == []


def test_assemble_drops_dangling_edges_with_a_warning():
    res = assemble_candidate(
        [{"key": "a", "type": "retrieve"}],
        [{"source": "a", "target": "ghost"}],
    )
    assert res["edges"] == []
    assert any("isn't in the graph" in w for w in res["warnings"])


def test_assemble_merges_defaults_under_supplied_config():
    res = assemble_candidate(
        [{"key": "g", "type": "confidence_gate", "config": {"default_threshold": 0.9}}],
        [],
        defaults=_DEFAULTS,
    )
    cfg = res["nodes"][0]["config"]
    assert cfg["default_threshold"] == 0.9          # supplied wins
    assert cfg["tier_overrides"]["enterprise"] == 0.6  # default filled in


def test_assemble_flags_missing_and_multiple_start_nodes():
    two_roots = assemble_candidate(
        [{"key": "a", "type": "retrieve"}, {"key": "b", "type": "identify"},
         {"key": "c", "type": "classify"}],
        [{"source": "a", "target": "c"}, {"source": "b", "target": "c"}],
    )
    assert two_roots["errors"] == []
    assert any("start node" in w for w in two_roots["warnings"])

    no_root = assemble_candidate(
        [{"key": "a", "type": "retrieve"}, {"key": "b", "type": "classify"}],
        [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
    )
    assert any("cycle" in e or "start node" in e for e in no_root["errors"])


# ── assist_generate (stub) ───────────────────────────────────────────
def test_generate_produces_a_compilable_flow(stub_llm):
    res = assist_generate("triage support cases and auto-reply when confident",
                          defaults=_DEFAULTS)
    assert res["errors"] == []
    assert res["diff"] is None
    types = {n["type"] for n in res["nodes"]}
    assert {"retrieve", "classify", "draft", "confidence_gate"} <= types
    flow = _flow(res)
    assert check_flow(Flow.model_validate(flow), require_expected_types=False) == []
    build_graph(flow)  # must not raise
    # branch out of the gate is a real conditional edge
    assert any((e["condition"] or {}).get("if") for e in res["edges"])


# ── assist_edit (stub echoes the current graph) ──────────────────────
def test_edit_returns_a_valid_graph_and_a_diff(stub_llm):
    base = assist_generate("basic triage flow", defaults=_DEFAULTS)
    current = _flow(base)
    res = assist_edit(current, "route enterprise tier straight to handover",
                      defaults=_DEFAULTS)
    assert res["errors"] == []
    assert set(res["diff"]) == {
        "added_nodes", "removed_nodes", "changed_nodes", "added_edges", "removed_edges"}
    # node identity is preserved across a no-op edit
    assert {n["node_id"] for n in res["nodes"]} == {n["node_id"] for n in current["nodes"]}


def test_diff_graphs_spots_add_remove_change():
    before = {
        "nodes": [{"node_id": "1", "type": "retrieve", "label": "R", "config": {}},
                  {"node_id": "2", "type": "draft", "label": "D", "config": {"max_tokens": 500}}],
        "edges": [{"source_node_id": "1", "target_node_id": "2", "condition": {}}],
    }
    after = {
        "nodes": [{"node_id": "1", "type": "retrieve", "label": "R", "config": {}},
                  {"node_id": "2", "type": "draft", "label": "D", "config": {"max_tokens": 900}},
                  {"node_id": "3", "type": "handover", "label": "H", "config": {}}],
        "edges": [{"source_node_id": "1", "target_node_id": "2", "condition": {}},
                  {"source_node_id": "2", "target_node_id": "3", "condition": {}}],
    }
    d = diff_graphs(before, after)
    assert d["added_nodes"] == ["H"] and d["removed_nodes"] == []
    assert d["changed_nodes"] == ["D"]
    assert d["added_edges"] == 1 and d["removed_edges"] == 0


# ── integration: a real Groq call ────────────────────────────────────
@pytest.mark.integration
def test_generate_with_real_groq_compiles():
    if not os.environ.get("GROQ_API_KEY"):
        pytest.skip("no GROQ_API_KEY")
    res = assist_generate(
        "Retrieve docs, classify the case, draft a reply, and auto-send only "
        "when the confidence gate passes, otherwise ask a human.",
        defaults=_DEFAULTS,
    )
    assert res["errors"] == [], res["errors"]
    build_graph(_flow(res))
