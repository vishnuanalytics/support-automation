"""
Phase 2: config-driven LangGraph interpreter.

A support "agent" is not hand-written Python here -- it is a row in `flows`
plus its `flow_nodes` / `flow_edges` in Supabase. `build_graph` reads one
such flow and compiles it into a real LangGraph `StateGraph` at runtime,
mapping each node's free-string `type` to a handler via `registry`.

This is the seam that lets the Phase 5 no-code UI be additive: the canvas
reads/writes the same rows; nothing about the runtime changes.

Public surface:
    load_flow(flow_id, ...)   -> dict  (loader.py)
    build_graph(flow_dict)    -> CompiledStateGraph  (builder.py)
    run_flow(flow_id, case)   -> CaseState  (run.py)
"""

from .loader import list_flows, load_flow
from .builder import build_graph
from .state import CaseState

__all__ = ["load_flow", "list_flows", "build_graph", "CaseState"]
