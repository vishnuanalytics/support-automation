import { describe, expect, it } from "vitest";
import type { Flow } from "../types";
import { candidateToCanvas, layout, neighborhood, toFlowPayload, toReactFlow, uuid } from "./graph";
import type { RFEdge } from "./graph";

const flow: Flow = {
  flow_id: "f", tenant_id: "t", team: "support", name: "n",
  status: "draft", version: 1, published_version: null,
  nodes: [
    { node_id: "r", type: "retrieve", label: "R", position_x: null, position_y: null, config: { top_k: 3 } },
    { node_id: "g", type: "confidence_gate", label: "G", position_x: null, position_y: null, config: {} },
    { node_id: "a", type: "auto_reply", label: "A", position_x: 10, position_y: 20, config: {} },
  ],
  edges: [
    { edge_id: "e1", source_node_id: "r", target_node_id: "g", condition: {} },
    { edge_id: "e2", source_node_id: "g", target_node_id: "a", condition: { if: "confidence_gate.pass" } },
  ],
};

describe("toReactFlow", () => {
  it("maps nodes and edges and marks terminals", () => {
    const { nodes, edges } = toReactFlow(flow);
    expect(nodes.map((n) => n.id).sort()).toEqual(["a", "g", "r"]);
    expect(nodes.find((n) => n.id === "a")!.data.terminal).toBe(true);
    expect(nodes.find((n) => n.id === "r")!.data.terminal).toBe(false);
    // conditional edge is animated and carries its expression
    const e2 = edges.find((e) => e.id === "e2")!;
    expect(e2.animated).toBe(true);
    expect(e2.label).toBe("confidence_gate.pass");
  });

  it("auto-lays-out nodes that have no saved position", () => {
    const { nodes } = toReactFlow(flow);
    // all three had null/duplicate-origin coords -> dagre spread them out
    const xs = nodes.map((n) => n.position.x);
    expect(nodes.every((n) => Number.isFinite(n.position.x) && Number.isFinite(n.position.y))).toBe(true);
    expect(new Set(xs).size).toBeGreaterThan(1); // not all stacked at the same x
  });
});

describe("toFlowPayload", () => {
  it("round-trips RF state + edited config back to the flow shape", () => {
    const { nodes, edges } = toReactFlow(flow);
    const cfg = { r: { top_k: 9 }, g: { default_threshold: 0.5 }, a: {} };
    const payload = toFlowPayload(flow, nodes, edges, cfg);
    expect(payload.nodes!.find((n) => n.node_id === "r")!.config).toEqual({ top_k: 9 });
    expect(payload.edges!.find((e) => e.edge_id === "e2")!.condition).toEqual({
      if: "confidence_gate.pass",
    });
    expect(payload.name).toBe("n");
  });
});

describe("layout", () => {
  it("returns a position for every node, left-to-right", () => {
    const { nodes, edges } = toReactFlow(flow);
    const laid = layout(nodes, edges);
    expect(laid).toHaveLength(3);
    const byId = Object.fromEntries(laid.map((n) => [n.id, n.position]));
    expect(byId.r.x).toBeLessThan(byId.a.x); // retrieve is upstream of auto_reply
  });
});

describe("candidateToCanvas", () => {
  it("lays out an imported/assisted graph and pulls out its config", () => {
    const candidate = {
      nodes: [
        { node_id: "x", type: "retrieve", label: "X", position_x: null, position_y: null, config: { top_k: 7 } },
        { node_id: "y", type: "handover", label: "Y", position_x: null, position_y: null, config: {} },
      ],
      edges: [{ edge_id: "e", source_node_id: "x", target_node_id: "y", condition: {} }],
    };
    const { nodes, edges, configById } = candidateToCanvas(flow, candidate);
    expect(nodes.map((n) => n.id).sort()).toEqual(["x", "y"]);
    expect(nodes.find((n) => n.id === "y")!.data.terminal).toBe(true);
    expect(edges).toHaveLength(1);
    expect(configById.x).toEqual({ top_k: 7 });
    // null positions were replaced by a dagre layout
    expect(nodes.every((n) => Number.isFinite(n.position.x))).toBe(true);
  });
});

describe("uuid", () => {
  it("produces distinct v4-ish ids", () => {
    const a = uuid();
    const b = uuid();
    expect(a).not.toBe(b);
    expect(a).toMatch(/^[0-9a-f-]{36}$/i);
  });
});

describe("neighborhood", () => {
  // trigger -> gate -> {a, b, c} — a real branch, like confidence_gate's
  // fan-out. Focusing "a" should keep the shared upstream (trigger, gate)
  // but drop the sibling branches (b, c) it shares no path with.
  const branchEdges: RFEdge[] = [
    { id: "e0", source: "trigger", target: "gate", data: { condition: {} } },
    { id: "e1", source: "gate", target: "a", data: { condition: { if: "x" } } },
    { id: "e2", source: "gate", target: "b", data: { condition: { if: "y" } } },
    { id: "e3", source: "gate", target: "c", data: { condition: {} } },
  ];

  it("keeps shared upstream but drops sibling branches", () => {
    const { nodeIds, edgeIds } = neighborhood("a", branchEdges);
    expect(nodeIds).toEqual(new Set(["a", "gate", "trigger"]));
    expect(edgeIds).toEqual(new Set(["e0", "e1"]));
  });

  it("includes full downstream from an upstream node", () => {
    const { nodeIds } = neighborhood("gate", branchEdges);
    expect(nodeIds).toEqual(new Set(["gate", "a", "b", "c", "trigger"]));
  });

  it("a node with no edges at all is its own singleton neighborhood", () => {
    const { nodeIds, edgeIds } = neighborhood("orphan", branchEdges);
    expect(nodeIds).toEqual(new Set(["orphan"]));
    expect(edgeIds.size).toBe(0);
  });
});
