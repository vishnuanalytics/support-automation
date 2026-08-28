"""
Validates a flow-definition JSON against the Phase 0 schema shape.
Run: python validate_flow.py flow_support_example.json
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


def validate(path: str) -> None:
    with open(path) as f:
        raw = json.load(f)

    flow = Flow.model_validate(raw)
    node_ids = {n.node_id for n in flow.nodes}
    errors = []

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

    present_types = {n.type for n in flow.nodes}
    missing = EXPECTED_TYPES - present_types
    if missing:
        errors.append(f"flow is missing expected node types: {missing}")

    cycle = find_cycle(flow.nodes, flow.edges)
    if cycle:
        errors.append(f"flow contains a cycle: {' -> '.join(cycle)}")

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
