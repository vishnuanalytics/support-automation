"""
Validates a flow-definition JSON against the Phase 0 schema shape.
Run: python interpreter/flows/validate_flow.py interpreter/flows/flow_support_example.json

`check_flow()` here is also imported by interpreter.loader (one validator,
not two) — the CLI keeps the strict EXPECTED_TYPES check, the loader doesn't.
"""

import sys
import json
from pydantic import BaseModel, Field, field_validator


class FlowNode(BaseModel):
    node_id: str
    type: str
    label: str | None = None
    config: dict = Field(default_factory=dict)


class FlowEdge(BaseModel):
    source_node_id: str
    target_node_id: str
    condition: dict = Field(default_factory=dict)
    label: str | None = None


class Flow(BaseModel):
    flow_id: str
    tenant_id: str
    team: str
    name: str
    version: int
    status: str
    nodes: list[FlowNode]
    edges: list[FlowEdge]

    @field_validator("status")
    @classmethod
    def status_valid(cls, v):
        allowed = {"draft", "published", "archived"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}, got {v!r}")
        return v


# node types that must appear at least once for a "complete" support flow.
# not enforced by the schema itself (types are generic) -- this is a
# convention check for Phase 2's interpreter to have something to build.
EXPECTED_TYPES = {"retrieve", "classify", "draft", "confidence_gate"}


def find_cycle(nodes: list[FlowNode], edges: list[FlowEdge]) -> list[str] | None:
    """DFS with a recursion stack. Returns the cycle path if one exists, else None."""
    adjacency: dict[str, list[str]] = {n.node_id: [] for n in nodes}
    for e in edges:
        if e.source_node_id in adjacency:
            adjacency[e.source_node_id].append(e.target_node_id)

    visited: set[str] = set()
    in_stack: set[str] = set()
    path: list[str] = []

    def dfs(node_id: str) -> list[str] | None:
        visited.add(node_id)
        in_stack.add(node_id)
        path.append(node_id)
        for neighbor in adjacency.get(node_id, []):
            if neighbor in in_stack:
                return path[path.index(neighbor):] + [neighbor]
            if neighbor not in visited:
                result = dfs(neighbor)
                if result:
                    return result
        path.pop()
        in_stack.discard(node_id)
        return None

    for n in nodes:
        if n.node_id not in visited:
            result = dfs(n.node_id)
            if result:
                return result
    return None


def check_flow(flow: "Flow", *, require_expected_types: bool = True) -> list[str]:
    """
    Structural checks on an already-parsed Flow. Returns a list of error
    strings (empty == valid). Used both by the CLI below and by the Phase 2
    interpreter's loader, which builds a `Flow` straight from Supabase rows
    rather than a file -- one validator, not two.
    """
    node_ids = {n.node_id for n in flow.nodes}
    errors: list[str] = []

    # referential integrity: every edge must point at real nodes
    for e in flow.edges:
        if e.source_node_id not in node_ids:
            errors.append(f"edge source '{e.source_node_id}' has no matching node")
        if e.target_node_id not in node_ids:
            errors.append(f"edge target '{e.target_node_id}' has no matching node")

    # every node except pure terminal nodes should have at least one
    # outgoing or incoming edge, otherwise it's orphaned
    connected = {e.source_node_id for e in flow.edges} | {e.target_node_id for e in flow.edges}
    for n in flow.nodes:
        if n.node_id not in connected:
            errors.append(f"node '{n.node_id}' ({n.type}) is not connected to any edge")

    if require_expected_types:
        present_types = {n.type for n in flow.nodes}
        missing = EXPECTED_TYPES - present_types
        if missing:
            errors.append(f"flow is missing expected node types: {missing}")

    cycle = find_cycle(flow.nodes, flow.edges)
    if cycle:
        errors.append(f"flow contains a cycle: {' -> '.join(cycle)}")

    # routing: the builder (interpreter/builder.py::_make_router) treats a
    # node's unconditional edge as its default/else branch, so more than one
    # is unbuildable. Checked here too so save/publish reject it up front
    # instead of publishing a flow that only fails at run time (2026-09-23: a
    # re-applied seed migration duplicated the Globex flow's edges and the
    # broken graph was published).
    defaults: dict[str, int] = {}
    for e in flow.edges:
        if not (e.condition or {}).get("if"):
            defaults[e.source_node_id] = defaults.get(e.source_node_id, 0) + 1
    for source_id, count in defaults.items():
        if count > 1:
            errors.append(
                f"node '{source_id}' has {count} unconditional outgoing edges "
                "(at most one; give the others an 'if' condition)"
            )

    return errors


def _is_candidate(raw: dict) -> bool:
    """A template / AI-generate candidate (`{nodes:[{key,...}], edges:[{source,
    target}]}`, see interpreter/templates.py) rather than a stored flow row."""
    return "flow_id" not in raw and any(
        "source" in e or "target" in e for e in raw.get("edges") or []
    )


def validate_candidate(path: str, raw: dict) -> None:
    """Validate a template the way the app loads it: assemble_candidate()
    (which runs check_flow without the EXPECTED_TYPES convention check)."""
    if __package__ in (None, ""):  # run as a script path: make `interpreter` importable
        import pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from interpreter.flows.flow_candidate import assemble_candidate  # avoid import cycle

    cand = assemble_candidate(raw.get("nodes") or [], raw.get("edges") or [])
    if cand["errors"]:
        print(f"INVALID: {path}")
        for err in cand["errors"]:
            print(f"  - {err}")
        sys.exit(1)

    print(f"VALID: {path} (template)")
    print(f"  template: {raw.get('name') or raw.get('id') or path}")
    print(f"  nodes: {len(cand['nodes'])}, edges: {len(cand['edges'])}")
    for w in cand["warnings"]:
        print(f"  warning: {w}")


def validate(path: str) -> None:
    with open(path) as f:
        raw = json.load(f)

    if _is_candidate(raw):
        validate_candidate(path, raw)
        return

    flow = Flow.model_validate(raw)
    errors = check_flow(flow)

    if errors:
        print(f"INVALID: {path}")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

    print(f"VALID: {path}")
    print(f"  flow: {flow.name} (team={flow.team}, status={flow.status})")
    print(f"  nodes: {len(flow.nodes)}, edges: {len(flow.edges)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python validate_flow.py <flow.json>")
        sys.exit(1)
    validate(sys.argv[1])
