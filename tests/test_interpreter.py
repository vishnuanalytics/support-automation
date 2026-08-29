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
from interpreter import registry as _registry
from interpreter.registry import (
    _norm_tier, h_ask_human, h_clarify, h_confidence_gate, h_draft, h_extract,
    h_identify, h_kb_lookup, h_policy_gate, h_sf_writeback, h_task_dispatch, register,
)
from interpreter.runs import build_row

_HERMETIC = ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
             "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE", "GROQ_API_KEY",
             "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_TOKEN",
             "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "SLACK_SIGNING_SECRET")


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


def test_confidence_gate_explicit_weights_downweight_overconfident_draft():
    # real-LLM shape: draft self-confidence pinned high, retrieval found ~nothing
    state = {"tier": "premium", "retrieval_score": 0.02, "draft_confidence": 0.99,
             "groundedness": {"score": 0.75},
             "classification": {"topic": "product-usage"}}
    cfg = {"_node_id": "g", "tier_overrides": {"premium": 0.55},
           "weights": {"retrieval": 0.55, "draft": 0.1, "groundedness": 0.35}}
    g = h_confidence_gate(state, cfg)["confidence_gate"]
    # 0.55*0.02 + 0.1*0.99 + 0.35*0.75 = 0.0110 + 0.099 + 0.2625 = 0.3725
    assert g["score"] == 0.3725 and g["pass"] is False
    # the legacy 0.5/0.5 blend would have passed it (~0.5+)
    legacy = h_confidence_gate(state, {**cfg, "weights": None,
                                       "retrieval_weight": 0.5})["confidence_gate"]
    assert legacy["score"] > g["score"]


def test_confidence_gate_escalate_topics_forces_a_human():
    base = {"tier": "premium", "retrieval_score": 1.0, "draft_confidence": 0.99,
            "groundedness": {"score": 1.0}}
    cfg = {"_node_id": "g", "tier_overrides": {"premium": 0.55},
           "weights": {"retrieval": 0.6, "draft": 0.1, "groundedness": 0.3},
           "escalate_topics": ["billing", "refund", "pricing", "account-access"]}

    refund = h_confidence_gate({**base, "classification": {"topic": "refund-request"}},
                               cfg)["confidence_gate"]
    assert refund["score"] >= 0.55 and refund["pass"] is False  # score high, still escalated
    assert "forced_escalation" in refund

    # a genuine how-to topic is untouched
    howto = h_confidence_gate({**base, "classification": {"topic": "webhook-trigger"}},
                              cfg)["confidence_gate"]
    assert howto["pass"] is True and "forced_escalation" not in howto

    # partial word doesn't trip it: 'export-step' must not match 'data-export'
    cfg2 = {**cfg, "escalate_topics": ["data-export"]}
    ok = h_confidence_gate({**base, "classification": {"topic": "export-step-howto"}},
                           cfg2)["confidence_gate"]
    assert ok["pass"] is True


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


def test_context_block_caps_oversized_chunks():
    from interpreter.registry import CTX_TOTAL, _context_block

    huge = [{"doc_url": f"u{i}", "chunk_text": "x" * 30000} for i in range(5)]
    block = _context_block(huge)
    # total payload stays bounded regardless of raw chunk size
    assert len(block) <= CTX_TOTAL + 5 * 40   # + a little for the [n] url headers
    assert block.startswith("[1] u0")


# --------------------------------------------------------------------------
# Phase 14 — kb_lookup node + draft folding internal KB
# --------------------------------------------------------------------------
def test_kb_lookup_scopes_to_collections_and_writes_out_key(monkeypatch):
    seen = {}

    def fake_retrieve(query, **kw):
        seen["query"] = query
        seen["kw"] = kw
        return [{"doc_url": "kb://x/1", "chunk_text": "refund cap is $200"}], 0.81

    monkeypatch.setattr(_registry, "hybrid_retrieve", fake_retrieve)
    state = {"tenant_id": "T1", "case": {"subject": "refund please", "body": "help"}}
    out = h_kb_lookup(state, {"_node_id": "k", "collections": ["billing-runbook"]})

    assert seen["kw"]["kb_sources"] == ["billing-runbook"]
    assert seen["kw"]["use_graph"] is False
    assert seen["kw"]["tenant_id"] == "T1"
    assert seen["query"] == "refund please help"
    kb = out["internal_kb"]
    assert kb["checked"] is True and kb["score"] == 0.81
    assert kb["matches"][0]["doc_url"] == "kb://x/1"


def test_kb_lookup_query_template_and_min_score(monkeypatch):
    seen = {}

    def fake_retrieve(query, **kw):
        seen["query"] = query
        return [{"doc_url": "kb://x/1", "chunk_text": "..."}], 0.2

    monkeypatch.setattr(_registry, "hybrid_retrieve", fake_retrieve)
    state = {"case": {"subject": "Broken"}, "classification": {"topic": "billing"}}
    out = h_kb_lookup(state, {"_node_id": "k", "collections": ["c"],
                              "query": "{{case.subject}} / {{classification.topic}}",
                              "min_score": 0.5})
    assert seen["query"] == "Broken / billing"
    # below min_score -> checked, but no matches handed downstream
    assert out["internal_kb"]["checked"] is True
    assert out["internal_kb"]["matches"] == []


def test_draft_folds_internal_kb_as_authoritative(monkeypatch):
    captured = {}

    def fake_complete(system, user, **kw):
        captured["system"] = system
        captured["user"] = user
        return '{"reply": "Per our runbook, refunds over $200 need a lead.", "confidence": 0.9}'

    monkeypatch.setattr(_registry.llm, "complete", fake_complete)
    monkeypatch.setattr(_registry.llm, "last_usage", None, raising=False)
    state = {
        "case": {"subject": "refund $500", "body": "want it back"},
        "retrieval": [{"doc_url": "https://docs/x", "chunk_text": "public refund info"}],
        "internal_kb": {"matches": [
            {"doc_url": "kb://billing/1", "chunk_text": "Refund cap: $200 auto, above needs a lead."}
        ]},
    }
    out = h_draft(state, {"_node_id": "d", "max_tokens": 200})
    assert "Internal runbook (authoritative" in captured["user"]
    assert "Refund cap: $200" in captured["user"]
    # public docs still included, but after the internal block
    assert captured["user"].index("Internal runbook") < captured["user"].index("Public documentation")
    assert out["trace"][0]["data"]["used_internal_kb"] is True


# --------------------------------------------------------------------------
# Phase 16 — policy rules + Slack signature
# --------------------------------------------------------------------------
def test_policy_predicate_evaluate():
    from interpreter import policy

    state = {"tier": "premium", "classification": {"topic": "billing"},
             "entities": {"report_period_years": 3}}
    assert policy.evaluate({"field": "tier", "op": "eq", "value": "premium"}, state)
    assert not policy.evaluate({"field": "tier", "op": "eq", "value": "basic"}, state)
    assert policy.evaluate(
        {"all": [
            {"field": "tier", "op": "in", "value": ["premium", "enterprise"]},
            {"any": [
                {"field": "classification.topic", "op": "eq", "value": "refund"},
                {"field": "entities.report_period_years", "op": "gte", "value": 2},
            ]},
        ]}, state)
    # missing field: eq is False, ne/nin True, exists:false True
    assert not policy.evaluate({"field": "entities.nope", "op": "eq", "value": 1}, state)
    assert policy.evaluate({"field": "entities.nope", "op": "exists", "value": False}, state)
    # empty predicate never matches
    assert not policy.evaluate({}, state)
    assert not policy.evaluate(None, state)


def test_policy_first_match_priority_and_status():
    from interpreter import policy

    rules = [
        {"name": "lo", "priority": 10, "status": "active",
         "when": {"field": "tier", "op": "eq", "value": "premium"}, "then": {"a": 1}},
        {"name": "hi", "priority": 1, "status": "active",
         "when": {"field": "tier", "op": "eq", "value": "premium"}, "then": {"a": 2}},
        {"name": "off", "priority": 0, "status": "disabled",
         "when": {"field": "tier", "op": "eq", "value": "premium"}, "then": {"a": 3}},
        {"name": "bad", "priority": 0, "status": "active",
         "when": {"nonsense": True}, "then": {"a": 4}},
    ]
    m = policy.first_match(rules, {"tier": "premium"})
    assert m["name"] == "hi"   # lowest priority number, skipping disabled + malformed
    assert policy.first_match(rules, {"tier": "basic"}) is None


def test_jobs_claim_ignores_an_all_null_row():
    """claim_job() (RETURNS SETOF jobs) can hand back a single all-NULL row
    on an empty queue — that must read as 'nothing to do', not a job with
    id 'None' (which then 400s downstream)."""
    from interpreter import jobs

    class _RPC:
        def __init__(self, data): self._d = data
        def execute(self): return type("R", (), {"data": self._d})()

    class _SB:
        def __init__(self, data): self._d = data
        def rpc(self, _name): return _RPC(self._d)

    assert jobs.claim(sb=_SB([])) is None
    assert jobs.claim(sb=_SB([{"job_id": None, "kind": None}])) is None
    assert jobs.claim(sb=_SB([{"job_id": "j1", "kind": "run_flow"}]))["kind"] == "run_flow"


def test_slack_verify_signature():
    from interpreter import slack

    secret = "s3cr3t"
    ts = str(int(__import__("time").time()))
    body = b"payload=%7B%22x%22%3A1%7D"
    import hashlib
    import hmac
    good = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body,
                            hashlib.sha256).hexdigest()
    assert slack.verify_signature(secret, ts, body, good)
    assert not slack.verify_signature(secret, ts, body, good[:-3] + "000")
    assert not slack.verify_signature(secret, "1", body, good)   # stale timestamp


def test_slack_github_available_false_without_creds():
    from interpreter import github, slack

    assert slack.available() is False
    assert github.available() is False


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name, self._filters = store, name, []

    def select(self, *_): return self
    def eq(self, k, v): self._filters.append((k, v)); return self
    def insert(self, row):
        row = {**row, "id": f"ar-{len(self.store[self.name])+1}"}
        self.store[self.name].append(row)
        self._last = [row]
        return self
    def execute(self):
        if hasattr(self, "_last"):
            out, self._last = self._last, None
            return type("R", (), {"data": out})()
        rows = self.store.get(self.name, [])
        for k, v in self._filters:
            rows = [r for r in rows if r.get(k) == v]
        return type("R", (), {"data": rows})()


class _FakeSB:
    def __init__(self, store): self.store = store
    def table(self, name): return _FakeTable(self.store, name)


def test_extract_short_circuits_without_fields():
    out = h_extract({"case": {}}, {"_node_id": "x"})
    assert out["entities"] == {}


def test_policy_gate_first_match_and_route(monkeypatch):
    rules = [{
        "tenant_id": "T", "team": "offboarding",
        "name": "old-export", "priority": 10, "status": "active",
        "when": {"field": "entities.report_age_years", "op": "gte", "value": 2},
        "then": {"type": "route", "action": "ask_human"},
    }]
    sb = _FakeSB({"policy_rules": rules})
    state = {"tenant_id": "T", "team": "offboarding",
             "entities": {"report_age_years": 4}}
    out = h_policy_gate(state, {"_node_id": "p", "_sb": sb})
    assert out["policy"]["matched"] == "old-export"
    assert out["policy"]["action"] == "ask_human"
    assert out["policy"]["task"] is None

    # nothing matches -> pass through
    out2 = h_policy_gate({**state, "entities": {"report_age_years": 1}},
                         {"_node_id": "p", "_sb": sb})
    assert out2["policy"]["matched"] is None


def test_task_dispatch_raises_action_request(monkeypatch):
    from interpreter import slack as slackmod
    monkeypatch.setattr(slackmod, "available", lambda: False)

    sb = _FakeSB({"action_requests": []})
    state = {
        "tenant_id": "T",
        "case": {"subject": "export my data", "body": "leaving next month"},
        "policy": {"matched": "old-export", "task": {
            "type": "task", "task": "github_issue", "repo": "acme/ops",
            "title_tmpl": "Export: {{case.subject}}", "body_tmpl": "{{case.body}}",
            "approval": {"slack_channel": "#leads"},
        }},
    }
    out = h_task_dispatch(state, {"_node_id": "t", "_sb": sb})
    assert out["outcome"]["action"] == "task_dispatched"
    ar = sb.store["action_requests"][0]
    assert ar["kind"] == "github_issue" and ar["status"] == "pending"
    assert ar["payload"]["title"] == "Export: export my data"
    assert ar["payload"]["repo"] == "acme/ops"
    assert out["outcome"]["slack_posted"] is False


def test_task_dispatch_noop_without_policy_task():
    sb = _FakeSB({"action_requests": []})
    out = h_task_dispatch({"policy": {}}, {"_node_id": "t", "_sb": sb})
    assert out["outcome"]["action"] == "task_skipped"
    assert sb.store["action_requests"] == []


# --------------------------------------------------------------------------
# Phase 15 — Google Docs connector (pure bits)
# --------------------------------------------------------------------------
def test_gdrive_parse_doc_id():
    from interpreter import gdrive

    assert gdrive.parse_doc_id(
        "https://docs.google.com/document/d/1AbCdEf_ghIJKlmno-pQRস/edit#heading=x".replace("স", "s")
    ) == "1AbCdEf_ghIJKlmno-pQRs"
    assert gdrive.parse_doc_id("1AbCdEf_ghIJKlmno-pQRstuvWXyz012345") == "1AbCdEf_ghIJKlmno-pQRstuvWXyz012345"
    with pytest.raises(ValueError):
        gdrive.parse_doc_id("https://example.com/not-a-doc")


def test_gdrive_available_false_without_creds():
    from interpreter import gdrive

    assert gdrive.available() is False
    with pytest.raises(RuntimeError):
        gdrive.authorize_url("http://localhost/cb", "state123")


def test_gdrive_docs_json_to_markdown():
    from interpreter import gdrive

    def para(text, style="NORMAL_TEXT", bullet=False):
        p = {"elements": [{"textRun": {"content": text}}],
             "paragraphStyle": {"namedStyleType": style}}
        if bullet:
            p["bullet"] = {"listId": "x"}
        return {"paragraph": p}

    doc = {"body": {"content": [
        para("Refund policy", "HEADING_1"),
        para("Thresholds below.\n"),
        para("Under $200 auto\n", bullet=True),
        para("Over $2000 manager\n", bullet=True),
        {"table": {"tableRows": [
            {"tableCells": [
                {"content": [para("tier\n")]},
                {"content": [para("limit\n")]},
            ]},
        ]}},
    ]}}
    md = gdrive.docs_json_to_markdown(doc)
    assert "# Refund policy" in md
    assert "- Under $200 auto" in md and "- Over $2000 manager" in md
    assert "| tier | limit |" in md
    assert "\n\n\n" not in md   # collapsed blank runs


def test_retry_after_seconds_parses_groq_hint():
    from interpreter import llm

    msg = ("Rate limit reached ... Please try again in 11.2575s. Need more tokens?")
    assert 11.0 < llm._retry_after_seconds(msg) <= 12.5
    # no hint -> small default; everything capped at GROQ_MAX_BACKOFF_S
    assert llm._retry_after_seconds("boom") == 2.0
    assert llm._retry_after_seconds("try again in 999s") == llm.GROQ_MAX_BACKOFF_S


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
# Phase 17a — clarify node (low-confidence recovery)
# --------------------------------------------------------------------------
def test_clarify_offline_produces_questions_without_sf_id():
    # hermetic fixture clears GROQ_API_KEY -> llm stub path
    state = {"case": {"subject": "It broke", "body": "help"},
             "retrieval": [], "confidence": 0.12}
    out = h_clarify(state, {"_node_id": "c", "max_questions": 3})
    assert out["outcome"]["action"] == "need_info"
    assert out["outcome"]["reason"] == "kb_insufficient"
    assert 1 <= len(out["clarification"]["questions"]) <= 3
    assert out["clarification"]["posted"] is False
    assert "chatter" not in out["outcome"]
    assert out["trace"][0]["type"] == "clarify"


def test_clarify_falls_back_to_one_question_when_model_returns_nothing(monkeypatch):
    from interpreter import llm
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "{}")
    out = h_clarify({"case": {"body": "x"}}, {"_node_id": "c"})
    assert len(out["clarification"]["questions"]) == 1
    assert out["outcome"]["action"] == "need_info"


def test_clarify_respects_max_questions(monkeypatch):
    from interpreter import llm
    monkeypatch.setattr(
        llm, "complete",
        lambda *a, **k: '{"questions": ["a?", "b?", "c?", "d?", "e?"], "missing": ["x"]}',
    )
    out = h_clarify({"case": {"body": "x"}}, {"_node_id": "c", "max_questions": 2})
    assert out["clarification"]["questions"] == ["a?", "b?"]
    assert out["clarification"]["missing"] == ["x"]


def test_clarify_default_posts_chatter_dry_run_with_sf_id():
    state = {"case": {"sf_id": "500XXXXXXXXXXXXXXX", "body": "help"}, "confidence": 0.1}
    out = h_clarify(state, {"_node_id": "c", "channel": "email", "_sb": _FakeSB({"runs": []})})
    assert out["outcome"]["delivery"]["dry_run"] is True
    assert out["clarification"]["posted"] is False    # a dry-run isn't a real post
    assert out["clarification"]["auto_sent"] is False
    assert out["outcome"]["awaiting_customer"] is False
    assert out["clarification"]["round"] == 1


def test_clarify_auto_send_emails_the_customer(monkeypatch):
    from interpreter import salesforce as sfmod
    calls = {}
    monkeypatch.setattr(sfmod, "send_case_reply",
                        lambda cid, body, **kw: calls.update(cid=cid, kw=kw) or
                        {"sent": True, "dry_run": False, "via": "email", "to": kw.get("to_email")})
    state = {"case": {"sf_id": "500X", "contact": {"email": "cust@acme.com"}, "body": "help"},
             "sender": {"email": "cust@acme.com", "match": "contact", "known": True},
             "confidence": 0.1}
    out = h_clarify(state, {"_node_id": "c", "auto_send": True, "_sb": _FakeSB({"runs": []})})
    assert calls["cid"] == "500X" and calls["kw"]["to_email"] == "cust@acme.com"
    assert out["clarification"]["auto_sent"] is True
    assert out["outcome"]["sent_to_customer"] is True
    assert out["outcome"]["awaiting_customer"] is True


def test_clarify_round_increments_from_prior_need_info_runs():
    sb = _FakeSB({"runs": [{"case_id": "500Z", "outcome": "need_info", "clarify_round": 1}]})
    state = {"case": {"sf_id": "500Z", "body": "still stuck"}, "confidence": 0.1}
    out = h_clarify(state, {"_node_id": "c", "_sb": sb})
    assert out["clarification"]["round"] == 2
    assert out["outcome"]["action"] == "need_info"
    assert out["clarify_round"] == 2


def test_clarify_exhausted_after_max_rounds_hands_to_human():
    sb = _FakeSB({"runs": [{"case_id": "500Z", "outcome": "need_info", "clarify_round": 2}]})
    state = {"case": {"sf_id": "500Z", "contact": {"email": "c@x.com"}, "body": "x"},
             "confidence": 0.1}
    out = h_clarify(state, {"_node_id": "c", "auto_send": True, "max_rounds": 2, "_sb": sb})
    assert out["clarification"]["round"] == 3 and out["clarification"]["exhausted"] is True
    assert out["outcome"]["action"] == "ask_human"
    assert out["outcome"]["reason"] == "clarify_exhausted"
    assert out["clarification"]["auto_send"] is False   # forced off once exhausted
    assert out["outcome"]["awaiting_customer"] is False


def test_build_row_persists_clarify_round():
    flow = {"flow_id": "f", "tenant_id": "t", "team": "support-triage"}
    final = {"outcome": {"action": "need_info"}, "trace": [], "clarify_round": 2}
    assert build_row(flow, final, case={"sf_id": "500Z"}, source="api")["clarify_round"] == 2


def test_build_row_need_info_is_recorded_but_not_pending():
    flow = {"flow_id": "f", "tenant_id": "t", "team": "support-triage"}
    final = {"outcome": {"action": "need_info", "questions": ["what plan?"]},
             "trace": [], "confidence": 0.1}
    row = build_row(flow, final, case={"sf_id": "500X"}, source="api")
    assert row["outcome"] == "need_info"
    assert row["human_action"] is None


@register("_t_gate17")
def _h_gate17(state, config):
    case = state["case"]
    gate = {"pass": bool(case.get("passed", False))}
    if case.get("forced"):
        gate["forced_escalation"] = "topic 'refund' ~ 'refund'"
    return {"tier": case.get("tier", "basic"), "confidence_gate": gate, "trace": []}


def _retrieval_gate_split_flow():
    """The Phase 17a retrieval_gate fan-out: four mutually-exclusive edges."""
    C = "not confidence_gate.pass and tier != 'enterprise'"
    return {
        "flow_id": "f", "tenant_id": "t", "team": "support-triage", "name": "t",
        "version": 1, "status": "draft",
        "nodes": [
            {"node_id": "g", "type": "_t_gate17", "label": "g", "config": {}},
            {"node_id": "draft", "type": "_t_end", "label": "draft", "config": {}},
            {"node_id": "human", "type": "_t_end", "label": "ask_human", "config": {}},
            {"node_id": "clarify", "type": "_t_end", "label": "clarify", "config": {}},
            {"node_id": "ent", "type": "_t_end", "label": "handover", "config": {}},
        ],
        "edges": [
            {"edge_id": "e1", "source_node_id": "g", "target_node_id": "ent",
             "condition": {"if": "tier == 'enterprise'"}},
            {"edge_id": "e2", "source_node_id": "g", "target_node_id": "draft",
             "condition": {"if": "confidence_gate.pass and tier != 'enterprise'"}},
            {"edge_id": "e3", "source_node_id": "g", "target_node_id": "human",
             "condition": {"if": f"{C} and confidence_gate.forced_escalation"}},
            {"edge_id": "e4", "source_node_id": "g", "target_node_id": "clarify",
             "condition": {"if": f"{C} and not confidence_gate.forced_escalation"}},
        ],
    }


def test_retrieval_gate_split_routes_benign_fail_to_clarify():
    graph = build_graph(_retrieval_gate_split_flow())
    cases = [
        ({"tier": "basic", "passed": True}, "draft"),
        ({"tier": "basic", "passed": False, "forced": True}, "ask_human"),
        ({"tier": "basic", "passed": False, "forced": False}, "clarify"),
        ({"tier": "enterprise", "passed": False}, "handover"),
    ]
    for case, want in cases:
        assert graph.invoke({"case": case, "trace": []})["outcome"]["action"] == want


# --------------------------------------------------------------------------
# Phase 17b — identify node (sender / email-domain -> account resolution)
# --------------------------------------------------------------------------
class _FakeSF:
    """Returns the queued records-lists from successive .query() calls."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.queries = []

    def query(self, soql):
        self.queries.append(soql)
        return {"records": self._responses.pop(0) if self._responses else []}


def test_identify_sender_none_without_creds():
    s = salesforce.identify_sender("jane@acme.com")
    assert s["match"] == "none" and s["known"] is False
    assert s["is_free_domain"] is False and s["domain"] == "acme.com"
    assert "not configured" in s["reason"]


def test_identify_sender_flags_free_mail_and_missing_email():
    assert salesforce.identify_sender("bob@gmail.com")["is_free_domain"] is True
    empty = salesforce.identify_sender("")
    assert empty["match"] == "none" and empty["reason"] == "no sender email"


def test_identify_sender_exact_contact(monkeypatch):
    fake = _FakeSF([[{"Id": "003X", "Name": "Jane Doe", "AccountId": "001X",
                      "Account": {"Name": "Acme Inc"}}]])
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: fake)
    s = salesforce.identify_sender("jane@acme.com")
    assert s["match"] == "contact" and s["known"] is True
    assert s["contact_id"] == "003X" and s["account_id"] == "001X"
    assert s["account_name"] == "Acme Inc" and s["account_matched"] is True
    assert len(fake.queries) == 1


def test_identify_sender_domain_to_account(monkeypatch):
    fake = _FakeSF([
        [],                                                # exact Contact -> none
        [],                                                # exact Lead -> none
        [{"AccountId": "001Y", "Account": {"Name": "Globex"}}],   # by domain
    ])
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: fake)
    s = salesforce.identify_sender("newhire@globex.com")
    assert s["match"] == "domain" and s["account_matched"] is True
    assert s["known"] is False and s["account_id"] == "001Y"
    assert s["account_name"] == "Globex"


def test_identify_sender_skips_domain_step_for_free_mail(monkeypatch):
    fake = _FakeSF([[], []])   # only the two exact-match queries should run
    monkeypatch.setattr(salesforce, "available", lambda: True)
    monkeypatch.setattr(salesforce, "client_for", lambda *a, **k: fake)
    s = salesforce.identify_sender("someone@gmail.com")
    assert s["match"] == "none" and s["is_free_domain"] is True
    assert len(fake.queries) == 2   # no '%@gmail.com' domain query


def test_h_identify_writes_sender_and_trace():
    out = h_identify({"case": {"contact": {"email": "a@b.com"}}}, {"_node_id": "id"})
    assert out["sender"]["match"] == "none"
    assert out["sender"]["domain"] == "b.com"
    assert out["trace"][0]["type"] == "identify"


def test_send_case_reply_dry_run_without_creds():
    r = salesforce.send_case_reply("500X", "1. what plan?", to_email="a@b.com")
    assert r["sent"] is False and r["dry_run"] is True and r["via"] == "dry_run"


def test_clarify_asks_identity_only_when_sender_unknown():
    base = {"case": {"body": "help"}, "confidence": 0.1}
    unknown = h_clarify({**base, "sender": {"match": "none", "known": False}},
                        {"_node_id": "c"})
    assert unknown["clarification"]["ask_identity"] is True

    known = h_clarify({**base, "sender": {"match": "contact", "known": True}},
                      {"_node_id": "c"})
    assert known["clarification"]["ask_identity"] is False

    domain = h_clarify({**base, "sender": {"match": "domain", "known": False,
                                           "account_matched": True,
                                           "account_name": "Acme Inc"}},
                       {"_node_id": "c"})
    assert domain["clarification"]["ask_identity"] is True
    assert domain["clarification"]["account_hint"] == "Acme Inc"


def test_retrieval_gated_portable_flow_compiles():
    import json as _json
    p = pathlib.Path(__file__).resolve().parents[1] / "interpreter/flows/flow_retrieval_gated.json"
    flow = _json.loads(p.read_text())
    assert check_flow(Flow.model_validate(flow), require_expected_types=False) == []
    build_graph(flow)   # raises on a bad entry / unknown type / routing gap
    types = {n["type"] for n in flow["nodes"]}
    assert {"identify", "clarify"} <= types


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
