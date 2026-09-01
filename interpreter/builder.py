"""
Compile a flow dict into a real LangGraph `StateGraph`.

Wiring rules:
  * entry point   = the unique node with no incoming edge
  * terminal node = a node with no outgoing edge -> wired to END
  * a node with exactly one outgoing edge and an empty `condition`
    -> plain edge
  * otherwise -> conditional edges: at run time we evaluate each outgoing
    edge's `condition.if` (safe AST eval, `conditions.py`) in a stable order
    and take the first that's true; an edge with an empty `condition` acts
    as the `else` / default branch. No branch and no default -> FlowRoutingError.

The flow is guaranteed acyclic and referentially sound before we get here
(loader runs `validate_flow.check_flow`).
"""

from __future__ import annotations

import time
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from .conditions import evaluate
from .registry import get_handler, known_types
from .state import CaseState


class FlowRoutingError(RuntimeError):
    pass


class FlowBuildError(ValueError):
    pass


def _context(state: CaseState) -> dict[str, Any]:
    """Names available to edge `condition` expressions."""
    gate = state.get("confidence_gate") or {}
    return {
        "tier": state.get("tier", "basic"),
        "region": state.get("region"),
        "confidence": state.get("confidence", 0.0),
        "retrieval_score": state.get("retrieval_score", 0.0),
        "draft_confidence": state.get("draft_confidence", 0.0),
        "confidence_gate": gate,
        "classification": state.get("classification") or {},
        "answer_mode": (state.get("classification") or {}).get("answer_mode") or "informational",
        "entities": state.get("entities") or {},
        "policy": state.get("policy") or {},
        "routed_team": state.get("routed_team") or "",
        "prior_resolutions": state.get("prior_resolutions") or [],
        "investigation_hints": state.get("investigation_hints") or [],
        "clarification": state.get("clarification") or {},
        "sender": state.get("sender") or {},
        "outcome": state.get("outcome") or {},
        "case": state.get("case") or {},
        # Phase 25 — enrichment nodes; edges can branch on `sf_context.*`,
        # `ai.<key>.*`, `attachments`.
        "sf_context": state.get("sf_context") or {},
        "ai": state.get("ai") or {},
        "attachments": state.get("attachments") or [],
    }


def _make_node(handler: Callable, config: dict) -> Callable[[CaseState], dict]:
    def _node(state: CaseState) -> dict:
        t0 = time.perf_counter()
        out = handler(state, config)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        # stamp timing (and any token usage the handler recorded) onto this
        # node's trace entry — Phase 7 latency/token accounting.
        for entry in out.get("trace", []):
            entry.setdefault("data", {})
            entry["data"]["elapsed_ms"] = elapsed_ms
        return out
    return _node


def _make_router(source_id: str, out_edges: list[dict]) -> Callable[[CaseState], str]:
    # stable order; the empty-condition edge (if any) is the default/else
    conditional = sorted(
        (e for e in out_edges if (e.get("condition") or {}).get("if")),
        key=lambda e: str(e.get("edge_id") or ""),
    )
    defaults = [e for e in out_edges if not (e.get("condition") or {}).get("if")]
    if len(defaults) > 1:
        raise FlowBuildError(
            f"node {source_id!r} has {len(defaults)} unconditional outgoing edges"
        )
    default_target = defaults[0]["target_node_id"] if defaults else None

    def _router(state: CaseState) -> str:
        ctx = _context(state)
        for e in conditional:
            expr = e["condition"]["if"]
            if evaluate(expr, ctx):
                return e["target_node_id"]
        if default_target is not None:
            return default_target
        raise FlowRoutingError(
            f"no outgoing edge from {source_id!r} matched; ctx tier={ctx['tier']!r} "
            f"gate={ctx['confidence_gate']!r}"
        )

    return _router


def build_graph(flow: dict[str, Any], *, checkpointer=None):
    """flow dict (from loader.load_flow) -> compiled StateGraph."""
    nodes: list[dict] = flow["nodes"]
    edges: list[dict] = flow["edges"]
    if not nodes:
        raise FlowBuildError("flow has no nodes")

    unknown = {n["type"] for n in nodes} - known_types()
    if unknown:
        raise FlowBuildError(f"no registered handler for node type(s): {sorted(unknown)}")

    out_by_src: dict[str, list[dict]] = {}
    in_ids: set[str] = set()
    for e in edges:
        out_by_src.setdefault(e["source_node_id"], []).append(e)
        in_ids.add(e["target_node_id"])

    roots = [n["node_id"] for n in nodes if n["node_id"] not in in_ids]
    if len(roots) != 1:
        raise FlowBuildError(
            f"expected exactly one entry node (no incoming edge), found {len(roots)}: {roots}"
        )
    entry = roots[0]

    g = StateGraph(CaseState)
    for n in nodes:
        cfg = dict(n.get("config") or {})
        cfg["_node_id"] = n["node_id"]
        cfg["_label"] = n.get("label")
        g.add_node(n["node_id"], _make_node(get_handler(n["type"]), cfg))

    g.set_entry_point(entry)

    for n in nodes:
        nid = n["node_id"]
        outs = out_by_src.get(nid, [])
        if not outs:
            g.add_edge(nid, END)
            continue
        only_defaults = all(not (e.get("condition") or {}).get("if") for e in outs)
        if len(outs) == 1 and only_defaults:
            g.add_edge(nid, outs[0]["target_node_id"])
            continue
        router = _make_router(nid, outs)
        targets = {e["target_node_id"] for e in outs}
        g.add_conditional_edges(nid, router, {t: t for t in targets})

    return g.compile(checkpointer=checkpointer)


def describe_graph(flow: dict[str, Any]) -> str:
    """Human-readable wiring summary (for the CLI / debugging)."""
    by_id = {n["node_id"]: n for n in flow["nodes"]}
    in_ids = {e["target_node_id"] for e in flow["edges"]}
    lines = [f"flow: {flow['name']}  ({flow['team']} / {flow['status']} / v{flow['version']})"]
    lines.append(f"  {len(flow['nodes'])} nodes, {len(flow['edges'])} edges")
    for n in flow["nodes"]:
        tag = "  (entry)" if n["node_id"] not in in_ids else ""
        lines.append(f"  * {n['type']:<16} {n.get('label') or '':<22}{tag}")
    lines.append("  edges:")
    for e in flow["edges"]:
        s = by_id[e["source_node_id"]]["type"]
        t = by_id[e["target_node_id"]]["type"]
        cond = (e.get("condition") or {}).get("if", "")
        lines.append(f"    {s:>16} -> {t:<16} {('[' + cond + ']') if cond else ''}")
    return "\n".join(lines)
