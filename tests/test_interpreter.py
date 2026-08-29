"""
Offline unit tests for the Phase 2 interpreter — no DB, no network, no LLM.

Run:  python -m tests.test_interpreter      (or: pytest tests/)
Covers: safe condition eval, flow validation, and graph wiring/routing.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import conditions, feedback, groundedness, salesforce
from interpreter.builder import FlowBuildError, FlowRoutingError, build_graph
from interpreter.flows.validate_flow import Flow, check_flow
from interpreter.registry import _norm_tier, h_ask_human, h_confidence_gate, h_sf_writeback, register
from interpreter.runs import build_row

_HERMETIC = ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
             "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE", "GROQ_API_KEY")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """A populated .env (or another test module's load_dotenv) must not turn
    these unit tests into live SF/LLM calls. Runs before every test here."""
    for k in _HERMETIC:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(salesforce, "_client_obj", None, raising=False)
    assert not salesforce.available()


# also clear at import time for the `python -m tests.test_interpreter` path
for _k in _HERMETIC:
    os.environ.pop(_k, None)
salesforce._client_obj = None


# --------------------------------------------------------------------------
# conditions.py
# --------------------------------------------------------------------------
def test_condition_basic_and_keyword_attr():
    ctx = {"tier": "enterprise", "confidence_gate": {"pass": True}}
    assert conditions.evaluate("tier == 'enterprise'", ctx) is True
    assert conditions.evaluate("confidence_gate.pass and tier != 'enterprise'", ctx) is False
    ctx["tier"] = "basic"
    assert conditions.evaluate("confidence_gate.pass and tier != 'enterprise'", ctx) is True
    assert conditions.evaluate("not confidence_gate.pass", ctx) is False


def test_condition_membership_and_missing_attr():
    ctx = {"classification": {"urgency": "high"}, "confidence_gate": {}}
    assert conditions.evaluate("classification.urgency in ('high', 'critical')", ctx) is True
    # missing attr resolves to None -> falsy, does not raise
    assert conditions.evaluate("confidence_gate.pass", ctx) is False


def test_condition_rejects_unsafe_and_unknown_name():
    for expr in ("__import__('os').system('x')", "(lambda: 1)()", "a + b"):
        try:
            conditions.evaluate(expr, {"a": 1, "b": 2})
        except conditions.ConditionError:
            pass
        else:
            raise AssertionError(f"expected ConditionError for {expr!r}")
    try:
        conditions.evaluate("nope == 1", {})
    except conditions.ConditionError:
        pass
    else:
        raise AssertionError("expected ConditionError for unknown name")


# --------------------------------------------------------------------------
# validate_flow.check_flow (reused by loader)
# --------------------------------------------------------------------------
def _flow(nodes, edges, **kw):
    base = dict(
        flow_id="f", tenant_id="t", team="support",
        name="n", version=1, status="draft", nodes=nodes, edges=edges,
    )
    base.update(kw)
    return Flow.model_validate(base)


def test_check_flow_catches_cycle_and_dangling_edge():
    nodes = [{"node_id": "a", "type": "retrieve"}, {"node_id": "b", "type": "classify"}]
    cyc = _flow(nodes, [
        {"source_node_id": "a", "target_node_id": "b"},
        {"source_node_id": "b", "target_node_id": "a"},
    ])
    assert any("cycle" in e for e in check_flow(cyc, require_expected_types=False))

    dangling = _flow(nodes, [{"source_node_id": "a", "target_node_id": "ghost"}])
    assert any("ghost" in e for e in check_flow(dangling, require_expected_types=False))


def test_check_flow_allows_a_gateless_flow_for_the_interpreter():
    # the Phase 4 offboarding flow: retrieve -> classify -> draft -> handover,
    # no confidence_gate. loader.load_flow validates with
    # require_expected_types=False, so this must be accepted.
    nodes = [
        {"node_id": "r", "type": "retrieve"}, {"node_id": "c", "type": "classify"},
        {"node_id": "d", "type": "draft"}, {"node_id": "h", "type": "handover"},
    ]
    edges = [
        {"source_node_id": "r", "target_node_id": "c"},
        {"source_node_id": "c", "target_node_id": "d"},
        {"source_node_id": "d", "target_node_id": "h"},
    ]
    assert check_flow(_flow(nodes, edges), require_expected_types=False) == []
    # ...but the strict CLI check still flags the missing confidence_gate
    assert any("confidence_gate" in e for e in check_flow(_flow(nodes, edges)))


# --------------------------------------------------------------------------
# builder.py — wiring + routing
# --------------------------------------------------------------------------
@register("_t_gate")
def _h_gate(state, config):
    # reads test inputs from `case` (a declared CaseState channel); writes
    # only declared channels so LangGraph keeps them.
    case = state["case"]
    return {
        "tier": case["tier"],
        "confidence_gate": {"pass": case["score"] >= 0.5},
        "trace": [],
    }


@register("_t_end")
def _h_end(state, config):
    return {"outcome": {"action": config["_label"]}, "trace": []}


def _routing_flow():
    return {
        "flow_id": "f", "tenant_id": "t", "team": "support", "name": "t",
        "version": 1, "status": "draft",
        "nodes": [
            {"node_id": "g", "type": "_t_gate", "label": "g", "config": {}},
            {"node_id": "yes", "type": "_t_end", "label": "auto", "config": {}},
            {"node_id": "no", "type": "_t_end", "label": "human", "config": {}},
            {"node_id": "ent", "type": "_t_end", "label": "handover", "config": {}},
        ],
        "edges": [
            {"edge_id": "e2", "source_node_id": "g", "target_node_id": "yes",
             "condition": {"if": "confidence_gate.pass and tier != 'enterprise'"}},
            {"edge_id": "e3", "source_node_id": "g", "target_node_id": "ent",
             "condition": {"if": "tier == 'enterprise'"}},
            {"edge_id": "e4", "source_node_id": "g", "target_node_id": "no", "condition": {}},
        ],
    }


def test_build_and_route_all_three_branches():
    graph = build_graph(_routing_flow())
    cases = [
        ({"tier": "basic", "score": 0.9}, "auto"),
        ({"tier": "basic", "score": 0.1}, "human"),      # falls through to default edge
        ({"tier": "enterprise", "score": 0.9}, "handover"),
    ]
    for case, want in cases:
        final = graph.invoke({"case": case, "trace": []})
        assert final["outcome"]["action"] == want, (case, final["outcome"])


def test_build_rejects_two_entry_points():
    flow = _routing_flow()
    # give 'yes' its own inbound-free sibling so there are two roots
    flow["nodes"].append({"node_id": "g2", "type": "_t_gate", "label": "g2", "config": {}})
    try:
        build_graph(flow)
    except FlowBuildError:
        pass
    else:
        raise AssertionError("expected FlowBuildError for >1 entry node")


def test_build_rejects_unknown_node_type():
    flow = _routing_flow()
    flow["nodes"][0]["type"] = "does_not_exist"
    try:
        build_graph(flow)
    except FlowBuildError:
        pass
    else:
        raise AssertionError("expected FlowBuildError for unknown node type")


def test_router_raises_when_no_branch_matches():
    flow = _routing_flow()
    # keep only the enterprise branch; a non-enterprise case matches nothing
    # and there is no default edge -> FlowRoutingError
    flow["edges"] = [e for e in flow["edges"] if e["edge_id"] == "e3"]
    flow["nodes"] = [n for n in flow["nodes"] if n["node_id"] in ("g", "ent")]
    graph = build_graph(flow)
    try:
        graph.invoke({"case": {"tier": "basic", "score": 0.1}, "trace": []})
    except FlowRoutingError:
        pass
    else:
        raise AssertionError("expected FlowRoutingError when nothing matches and no default")


# --------------------------------------------------------------------------
# Phase 3 — sf_writeback + Chatter ask_human (offline / dry-run)
# --------------------------------------------------------------------------
_SFW_CFG = {
    "_node_id": "sfw", "_label": "sf",
    "field_map": {"urgency": "Priority", "topic": "Module__c", "region": "Region__c"},
    "value_maps": {"Priority": {"critical": "High", "high": "High",
                                "normal": "Medium", "low": "Low"}},
    "append": {"Description": "summary"},
}


def test_sf_writeback_maps_fields_dry_run():
    state = {
        "case": {"sf_id": "500XXXXXXXXXXXXXXX"},
        "tier": "premium", "region": "EMEA",
        "classification": {"urgency": "high", "topic": "billing", "summary": "charge looks wrong"},
    }
    out = h_sf_writeback(state, _SFW_CFG)["sf_writeback"]
    assert out["dry_run"] is True
    planned = out["planned"]
    assert planned["Priority"] == "High"          # value-mapped
    assert planned["Module__c"] == "billing"
    assert planned["Region__c"] == "EMEA"
    assert "Description" in planned               # summary append planned


def test_sf_writeback_no_sf_id_is_a_skip_not_an_error():
    state = {"case": {}, "tier": "basic", "classification": {"urgency": "low", "topic": "x"}}
    res = h_sf_writeback(state, _SFW_CFG)
    assert res["sf_writeback"]["target"] is None
    assert res["sf_writeback"]["written"] == {}
    assert res["trace"][0]["type"] == "sf_writeback"


def test_ask_human_posts_chatter_dry_run_when_sf_id_present():
    state = {"case": {"sf_id": "500XXXXXXXXXXXXXXX"}, "confidence": 0.3, "draft": "try this"}
    outcome = h_ask_human(state, {"_node_id": "ah", "channel": "salesforce_chatter"})["outcome"]
    assert outcome["action"] == "ask_human"
    assert outcome["chatter"]["dry_run"] is True


def test_ask_human_without_sf_id_just_records_channel():
    state = {"case": {}, "confidence": 0.3, "draft": "try this"}
    outcome = h_ask_human(state, {"_node_id": "ah", "channel": "salesforce_chatter"})["outcome"]
    assert "chatter" not in outcome


# --------------------------------------------------------------------------
# Phase 6 — runs.build_row (offline shaping; no DB)
# --------------------------------------------------------------------------
def test_build_row_shapes_a_runs_record():
    flow = {"flow_id": "f1", "tenant_id": "t1", "team": "support"}
    final = {
        "tier": "premium", "region": "EMEA", "confidence": 0.32,
        "confidence_gate": {"pass": False, "threshold": 0.45, "score": 0.32},
        "outcome": {"action": "ask_human", "channel": "salesforce_chatter"},
        "trace": [{"node_id": "g", "type": "confidence_gate", "summary": "FAIL", "data": {}}],
        "retrieval": [
            {"doc_url": "https://x/a", "heading_path": "A", "rerank_score": 7.1, "chunk_text": "big"},
        ],
        "sf_writeback": {"target": None, "status": "no sf_id on case"},
    }
    row = build_row(flow, final, case={"case_id": "C-1", "subject": "hi"}, source="cli")
    assert row["flow_id"] == "f1" and row["tenant_id"] == "t1" and row["team"] == "support"
    assert row["source"] == "cli" and row["case_id"] == "C-1" and row["subject"] == "hi"
    assert row["outcome"] == "ask_human" and row["tier"] == "premium"
    assert row["confidence"] == 0.32 and row["gate"]["threshold"] == 0.45
    # retrieval is slimmed — no chunk_text
    assert row["retrieval"] == [{"doc_url": "https://x/a", "heading_path": "A", "rerank_score": 7.1}]
    assert "chunk_text" not in row["retrieval"][0]


# --------------------------------------------------------------------------
# Phase 7 — fail-closed tier, groundedness, gate weighting
# --------------------------------------------------------------------------
def test_norm_tier_fails_closed_on_unknown():
    assert _norm_tier("enterprise") == "enterprise"
    assert _norm_tier("Professional") == "premium"
    assert _norm_tier("free") == "basic"
    # unknown -> strictest, not "basic"
    assert _norm_tier("platinum-plus") == "enterprise"
    assert _norm_tier(None) == "enterprise"


def test_groundedness_lexical_flags_offcorpus_draft():
    chunks = [{"chunk_text": "Configure the webhook URL in the trigger settings and send a test event."}]
    good = groundedness.check("Set the webhook URL in the trigger settings, then send a test event.", chunks)
    bad = groundedness.check("Contact your account manager about the quantum blockchain refund policy.", chunks)
    assert good["backend"] == "lexical" and good["score"] > bad["score"]
    assert bad["score"] < 0.5


def test_confidence_gate_groundedness_weight_pulls_score_down():
    base_state = {"tier": "basic", "retrieval_score": 0.9, "draft_confidence": 0.9,
                  "groundedness": {"score": 0.0}}
    cfg = {"_node_id": "g", "default_threshold": 0.5,
           "tier_overrides": {"basic": 0.5}, "retrieval_weight": 0.5}
    no_w = h_confidence_gate(base_state, {**cfg, "groundedness_weight": 0.0})["confidence_gate"]
    with_w = h_confidence_gate(base_state, {**cfg, "groundedness_weight": 0.5})["confidence_gate"]
    assert no_w["score"] == 0.9 and no_w["pass"] is True
    # 0.5*(0.5*0.9 + 0.5*0.9) + 0.5*0.0 = 0.45 -> below the 0.5 bar
    assert with_w["score"] == 0.45 and with_w["pass"] is False


# --------------------------------------------------------------------------
# llm provider routing (Groq + Anthropic)
# --------------------------------------------------------------------------
def test_llm_provider_routing_and_stub_fallback(monkeypatch):
    from interpreter import llm

    assert llm.provider("llama-3.1-8b-instant") == "groq"
    assert llm.provider("claude-haiku-4-5") == "anthropic"
    assert llm.provider("claude-sonnet-5") == "anthropic"

    for k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert llm.available() is False
    # a Claude model with no ANTHROPIC key -> deterministic stub, no exception
    out = llm.complete("classify", "billing dispute about an invoice",
                       model="claude-sonnet-5", json_object=True)
    assert '"_stub": true' in out and '"topic": "billing"' in out

    # a key for the *other* provider doesn't make a claude call "available"
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert llm.available("claude-sonnet-5") is False
    assert llm.available("llama-3.1-8b-instant") is True
    assert llm.available() is True

    with pytest.raises(ValueError):
        llm.complete("s", "u", model="gpt-4o")   # not in the roster


def test_llm_recovers_from_groq_json_validate_failed(monkeypatch):
    """Groq 400s a truncated JSON reply with code `json_validate_failed`.
    complete() must not propagate it — salvage the partial or retry free-form."""
    from interpreter import llm

    class _BadRequestError(Exception):
        def __init__(self, body):
            super().__init__(str(body))
            self.code = body["error"]["code"]
            self.body = body

    monkeypatch.setattr(llm, "_groq_complete", llm._groq_complete)  # keep real
    monkeypatch.setenv("GROQ_API_KEY", "x")
    import groq
    monkeypatch.setattr(groq, "BadRequestError", _BadRequestError, raising=False)

    calls = {"n": 0}

    def fake_call(model, system, user, max_tokens, temperature, *, response_format):
        calls["n"] += 1
        if response_format:
            raise _BadRequestError({"error": {
                "code": "json_validate_failed",
                "failed_generation": '{"reply": "do the thing", "confidence": 0.7',  # truncated
            }})
        # free-form retry path (not reached here — partial salvages first)
        return type("R", (), {"choices": [type("C", (), {"message": type(
            "M", (), {"content": '{"reply": "retry", "confidence": 0.5}'})()})()],
            "usage": None})()

    monkeypatch.setattr(llm, "_groq_call", fake_call)
    out = llm.complete("sys", "user", model="openai/gpt-oss-120b",
                       json_object=True, max_tokens=200)
    import json as _j
    parsed = _j.loads(out)
    assert parsed["reply"] and "confidence" in parsed
    assert calls["n"] == 2   # first (JSON) 400'd, free-form retry recovered


# --------------------------------------------------------------------------
# Phase 12 — source scoping (cross-tenant isolation)
# --------------------------------------------------------------------------
class _FakeSources:
    """Minimal stub of the supabase query builder for `sources`."""
    _ROWS = [
        {"source_id": "s-public", "name": "zapier-public", "tenant_id": None},
        {"source_id": "s-globex", "name": "globex-sop", "tenant_id": "GLOBEX"},
        {"source_id": "s-acme", "name": "acme-kb", "tenant_id": "ACME"},
    ]

    def table(self, _): return self
    def select(self, *_): return self
    def eq(self, *_): return self
    def execute(self):
        return type("R", (), {"data": list(self._ROWS)})()


def test_resolve_sources_never_leaks_another_tenants_kb():
    from interpreter.retrieval import resolve_sources
    sb = _FakeSources()
    # Acme flow, no names -> shared + acme only
    assert set(resolve_sources(None, sb, "ACME")) == {"s-public", "s-acme"}
    # Globex flow naming its own + shared
    assert set(resolve_sources(["globex-sop", "zapier-public"], sb, "GLOBEX")) == {"s-globex", "s-public"}
    # Acme flow *naming* globex-sop -> falls back to Acme's legitimate scope, no leak
    assert "s-globex" not in resolve_sources(["globex-sop"], sb, "ACME")
    # no tenant -> shared only
    assert resolve_sources(None, sb, None) == ["s-public"]


# --------------------------------------------------------------------------
# Phase 11 — feedback.classify_edit
# --------------------------------------------------------------------------
def test_classify_edit_buckets():
    draft = "Thanks for reaching out. To set up a webhook trigger, open the Zap editor, add a Webhooks by Zapier trigger, copy the URL, and send a test event."
    assert feedback.classify_edit(draft, draft) == ("sent_as_is", 0.0)
    assert feedback.classify_edit(draft, draft + " Let me know if that helps!")[0] in ("sent_as_is", "edited")
    lightly = draft.replace("Thanks for reaching out.", "Hi there,").replace("send a test event", "fire a test event")
    assert feedback.classify_edit(draft, lightly)[0] == "edited"
    assert feedback.classify_edit(draft, "We've escalated this to billing; someone will call you.")[0] == "rewrote"
    assert feedback.classify_edit(draft, "") == ("no_reply", 1.0)


def test_build_row_marks_pending_only_for_human_outcomes_on_real_cases():
    flow = {"flow_id": "f", "tenant_id": "t", "team": "support"}
    ask = {"outcome": {"action": "ask_human"}, "trace": [], "draft": "d"}
    assert build_row(flow, ask, case={"sf_id": "500X"}, source="api")["human_action"] == "pending"
    assert build_row(flow, ask, case={"case_id": "synthetic"}, source="api")["human_action"] is None
    auto = {"outcome": {"action": "auto_reply"}, "trace": [], "draft": "d"}
    assert build_row(flow, auto, case={"sf_id": "500X"}, source="api")["human_action"] is None


# --------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
