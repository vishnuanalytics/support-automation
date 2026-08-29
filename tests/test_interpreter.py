"""
Offline unit tests for the Phase 2 interpreter — no DB, no network, no LLM.

Run:  python -m tests.test_interpreter      (or: pytest tests/)
Covers: safe condition eval, flow validation, and graph wiring/routing.
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import conditions, salesforce
from interpreter.builder import FlowBuildError, FlowRoutingError, build_graph
from interpreter.flows.validate_flow import Flow, check_flow
from interpreter.registry import h_ask_human, h_sf_writeback, register

# hermetic: a populated .env (SF creds, GROQ key) must not turn these into
# live calls. The imports above run load_dotenv() (via scraper.py), so clear
# the SF vars *after* importing and reset the cached client.
for _k in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN",
           "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET",
           "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE"):
    os.environ.pop(_k, None)
salesforce._client_obj = None
assert not salesforce.available(), "SF creds leaked into the offline test env"


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
