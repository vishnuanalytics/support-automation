"""Phase 19a -- offline tests for the Mermaid -> candidate-flow parser."""

from __future__ import annotations

from interpreter.builder import build_graph
from interpreter.flows.flow_candidate import assemble_candidate
from interpreter.flows.mermaid_import import mermaid_to_flow
from interpreter.flows.validate_flow import Flow, check_flow


def _types(res):
    return [n["type"] for n in res["nodes"]]


def test_linear_flowchart_maps_types_and_edges():
    res = mermaid_to_flow(
        """
        flowchart TD
          A[Retrieve docs] --> B[Classify]
          B --> C[Draft reply]
          C --> D[Confidence gate]
          D --> E[Auto reply]
        """
    )
    assert res["errors"] == []
    assert _types(res) == ["retrieve", "classify", "draft", "confidence_gate", "auto_reply"]
    assert len(res["edges"]) == 4
    # every node got a fresh uuid, positions left for the canvas to lay out
    assert all(n["position_x"] is None for n in res["nodes"])
    assert len({n["node_id"] for n in res["nodes"]}) == 5


def test_chained_and_fanout_edges():
    res = mermaid_to_flow(
        "graph LR\n  R[retrieve] --> C[classify] --> G[confidence_gate]\n"
        "  G --> A[auto_reply] & H[ask_human]"
    )
    pairs = {
        (next(n["type"] for n in res["nodes"] if n["node_id"] == e["source_node_id"]),
         next(n["type"] for n in res["nodes"] if n["node_id"] == e["target_node_id"]))
        for e in res["edges"]
    }
    assert ("retrieve", "classify") in pairs
    assert ("classify", "confidence_gate") in pairs
    assert ("confidence_gate", "auto_reply") in pairs
    assert ("confidence_gate", "ask_human") in pairs


def test_edge_labels_are_flagged_not_converted():
    res = mermaid_to_flow(
        """
        flowchart TD
          G{Confident?} -->|yes| S[Auto reply]
          G -->|no| E[Escalate to human]
        """
    )
    assert all(e["condition"] == {} for e in res["edges"])
    assert any("not turned into routing conditions" in w for w in res["warnings"])
    assert _types(res) == ["confidence_gate", "auto_reply", "ask_human"]


def test_inline_label_syntax():
    res = mermaid_to_flow("flowchart LR\n A[retrieve] -- go --> B[classify]")
    assert len(res["edges"]) == 1
    assert res["edges"][0]["condition"] == {}
    assert any("edge label" in w for w in res["warnings"])


def test_unknown_node_becomes_draft_and_is_flagged():
    res = mermaid_to_flow("flowchart TD\n A[Do a barrel roll] --> B[Auto reply]")
    assert res["nodes"][0]["type"] == "draft"
    assert any("not a known node type" in w for w in res["warnings"])


def test_node_shapes_and_comments_and_subgraph():
    res = mermaid_to_flow(
        """
        %% a support flow
        flowchart TD
          subgraph triage
            A([retrieve]) --> B{{classify}}
          end
          B --> C[(draft)]
          C --> D>confidence gate]
        """
    )
    assert _types(res) == ["retrieve", "classify", "draft", "confidence_gate"]
    assert res["errors"] == []


def test_front_matter_title_is_returned():
    res = mermaid_to_flow(
        "---\ntitle: Billing triage\n---\nflowchart TD\n A[retrieve] --> B[classify]"
    )
    assert res["name"] == "Billing triage"


def test_self_loop_is_reported_as_a_cycle():
    res = mermaid_to_flow("flowchart TD\n A[retrieve] --> A")
    assert any("cycle" in e.lower() or "start node" in e.lower() for e in res["errors"])


def test_multiple_roots_is_a_warning_not_an_error():
    res = mermaid_to_flow(
        "flowchart TD\n A[retrieve] --> C[classify]\n B[identify] --> C"
    )
    assert res["errors"] == []
    assert any("start node" in w for w in res["warnings"])


def test_empty_input_errors_cleanly():
    res = mermaid_to_flow("   ")
    assert res["nodes"] == [] and res["errors"]


def test_result_compiles_into_a_real_stategraph():
    res = mermaid_to_flow(
        "flowchart TD\n R[retrieve] --> C[classify] --> D[draft] --> "
        "G[confidence_gate] --> A[auto_reply]"
    )
    flow = {
        "flow_id": "f", "tenant_id": "t", "team": "support", "name": "n",
        "version": 1, "status": "draft", "nodes": res["nodes"], "edges": res["edges"],
    }
    assert check_flow(Flow.model_validate(flow), require_expected_types=False) == []
    build_graph(flow)  # must not raise


def test_assemble_candidate_keeps_existing_uuid_keys():
    uid = "11111111-1111-1111-1111-111111111111"
    res = assemble_candidate(
        [{"key": uid, "type": "retrieve", "label": "R"},
         {"key": "new", "type": "classify", "label": "C"}],
        [{"source": uid, "target": "new"}],
    )
    assert res["nodes"][0]["node_id"] == uid
    assert res["edges"][0]["source_node_id"] == uid
    assert res["nodes"][1]["node_id"] != "new"  # a fresh uuid
