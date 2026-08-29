import { describe, expect, it } from "vitest";
import type { Flow } from "../types";
import { layout, toFlowPayload, toReactFlow, uuid } from "./graph";

const flow: Flow = {
  flow_id: "f", tenant_id: "t", team: "support", name: "n",
  status: "draft", version: 1,
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

describe("uuid", () => {
  it("produces distinct v4-ish ids", () => {
    const a = uuid();
    const b = uuid();
    expect(a).not.toBe(b);
    expect(a).toMatch(/^[0-9a-f-]{36}$/i);
  });
});
