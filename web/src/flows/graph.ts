import Dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import type { Flow, FlowEdge, FlowNode } from "../types";

export type RFNode = Node<{ label: string; nodeType: string; terminal: boolean }>;
export type RFEdge = Edge<{ condition: Record<string, unknown> }>;

const TERMINAL = new Set(["auto_reply", "ask_human", "handover", "clarify", "notify"]);

export function toReactFlow(flow: Flow): { nodes: RFNode[]; edges: RFEdge[] } {
  const needsLayout = flow.nodes.some(
    (n) => n.position_x == null || n.position_y == null,
  );

  let nodes: RFNode[] = flow.nodes.map((n) => ({
    id: n.node_id,
    position: { x: n.position_x ?? 0, y: n.position_y ?? 0 },
    data: { label: n.label || n.type, nodeType: n.type, terminal: TERMINAL.has(n.type) },
    type: "flowNode",
  }));

  const edges: RFEdge[] = flow.edges.map((e) => ({
    id: e.edge_id,
    source: e.source_node_id,
    target: e.target_node_id,
    label: (e.condition as { if?: string })?.if ?? "",
    data: { condition: e.condition || {} },
    animated: !!(e.condition as { if?: string })?.if,
  }));

  if (needsLayout) nodes = layout(nodes, edges);
  return { nodes, edges };
}

export function layout(nodes: RFNode[], edges: RFEdge[]): RFNode[] {
  const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "LR", nodesep: 40, ranksep: 90 });
  nodes.forEach((n) => g.setNode(n.id, { width: 170, height: 48 }));
  edges.forEach((e) => g.setEdge(e.source, e.target));
  Dagre.layout(g);
  return nodes.map((n) => {
    const p = g.node(n.id);
    return { ...n, position: { x: Math.round(p.x - 85), y: Math.round(p.y - 24) } };
  });
}

/** React Flow state + the current DB flow -> the payload for PUT/validate */
export function toFlowPayload(
  flow: Flow,
  rfNodes: RFNode[],
  rfEdges: RFEdge[],
  configById: Record<string, Record<string, unknown>>,
): Partial<Flow> {
  const nodes: FlowNode[] = rfNodes.map((n) => ({
    node_id: n.id,
    type: n.data.nodeType,
    label: n.data.label,
    position_x: Math.round(n.position.x),
    position_y: Math.round(n.position.y),
    config: configById[n.id] ?? {},
  }));
  const edges: FlowEdge[] = rfEdges.map((e) => ({
    edge_id: e.id,
    source_node_id: e.source,
    target_node_id: e.target,
    condition: (e.data?.condition as Record<string, unknown>) ?? {},
  }));
  return { name: flow.name, status: flow.status, version: flow.version, nodes, edges };
}

export function uuid(): string {
  return crypto.randomUUID();
}

/** Phase 19 — a proposed graph (Mermaid import / AI assist) -> canvas state.
 *  Reuses toReactFlow (dagre-lays-out the null positions) and pulls the
 *  per-node config out into the map the inspector edits. */
export function candidateToCanvas(
  flow: Flow,
  res: { nodes: FlowNode[]; edges: FlowEdge[] },
): { nodes: RFNode[]; edges: RFEdge[]; configById: Record<string, Record<string, unknown>> } {
  const { nodes, edges } = toReactFlow({ ...flow, nodes: res.nodes, edges: res.edges });
  const configById = Object.fromEntries(
    res.nodes.map((n) => [n.node_id, (n.config ?? {}) as Record<string, unknown>]),
  );
  return { nodes, edges, configById };
}
