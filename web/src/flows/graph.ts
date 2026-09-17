import Dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import type { Flow, FlowEdge, FlowNode } from "../types";
import { summarize } from "./nodeSummary";

export type RFNode = Node<{
  label: string;
  nodeType: string;
  terminal: boolean;
  /** line 2 of the card — the one config value that decides behaviour */
  summary?: string;
  /** type is not in the loaded registry */
  invalid?: boolean;
  /** >1 unconditional outgoing edge — builder.py's build_graph rejects this */
  tooManyDefaults?: boolean;
  /** outside the selected node's focus neighborhood — display-only, never saved */
  dimmed?: boolean;
}>;
export type RFEdge = Edge<{ condition: Record<string, unknown> }>;

// Node types that can end a path — each produces a final `outcome` (or, for
// `notify_human`, is the real end of every escalation now that Phase 24
// removed auto-send). Single source of truth — FlowEditor's palette preview
// used to keep its own copy of this list and the two drifted apart.
export const TERMINAL = new Set([
  "auto_reply", "ask_human", "handover", "clarify", "notify", "notify_human",
]);

// Friendly, connector-neutral display names for node types whose internal
// slug predates multi-provider connectors (FR-51) and reads as
// Salesforce-only even though the node itself now works against whichever
// case system a tenant has connected (Salesforce, HubSpot, ...) — `sf_case`/
// `sf_writeback` route through `connectors.resolve_case_connector` same as
// every other case-touching node. `sf_context` is the one genuine exception
// (Salesforce-only, no other-connector equivalent yet — see its own
// NODE_HELP entry in Inspector.tsx), labelled to say so. The node's own
// `type` string is unchanged (stored in flow_nodes.type) — this is display
// only, used by the palette.
export const NODE_LABELS: Record<string, string> = {
  sf_case: "Case lookup / create",
  sf_writeback: "Write case fields",
  sf_context: "Salesforce context (SF only)",
};

export function nodeLabel(type: string): string {
  return NODE_LABELS[type] || type;
}

export function toReactFlow(flow: Flow): { nodes: RFNode[]; edges: RFEdge[] } {
  const needsLayout = flow.nodes.some(
    (n) => n.position_x == null || n.position_y == null,
  );

  let nodes: RFNode[] = flow.nodes.map((n) => ({
    id: n.node_id,
    position: { x: n.position_x ?? 0, y: n.position_y ?? 0 },
    data: {
      label: n.label || n.type,
      nodeType: n.type,
      terminal: TERMINAL.has(n.type),
      summary: summarize(n.type, n.config ?? {}),
    },
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
  g.setGraph({ rankdir: "LR", nodesep: 70, ranksep: 140, edgesep: 30 });
  nodes.forEach((n) => g.setNode(n.id, { width: 200, height: 56 }));
  edges.forEach((e) => g.setEdge(e.source, e.target));
  Dagre.layout(g);
  return nodes.map((n) => {
    const p = g.node(n.id);
    return { ...n, position: { x: Math.round(p.x - 100), y: Math.round(p.y - 28) } };
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

/**
 * A node's full "focus neighborhood" — every node reachable from it
 * (downstream) and every node that can reach it (upstream), both ways,
 * plus every edge along those paths. On a real flow with genuine branches
 * (e.g. a confidence_gate fanning out 6 ways to different terminals),
 * focusing one terminal dims the *other* branches while still showing the
 * shared upstream chain — "just the path that leads here," not just the
 * clicked node's immediate neighbors.
 */
export function neighborhood(
  nodeId: string,
  edges: RFEdge[],
): { nodeIds: Set<string>; edgeIds: Set<string> } {
  const outBySource = new Map<string, RFEdge[]>();
  const inByTarget = new Map<string, RFEdge[]>();
  for (const e of edges) {
    (outBySource.get(e.source) ?? outBySource.set(e.source, []).get(e.source)!).push(e);
    (inByTarget.get(e.target) ?? inByTarget.set(e.target, []).get(e.target)!).push(e);
  }

  const nodeIds = new Set<string>([nodeId]);
  const edgeIds = new Set<string>();

  const walk = (start: string, by: Map<string, RFEdge[]>, other: (e: RFEdge) => string) => {
    const queue = [start];
    while (queue.length) {
      const id = queue.shift()!;
      for (const e of by.get(id) ?? []) {
        edgeIds.add(e.id);
        const next = other(e);
        if (!nodeIds.has(next)) {
          nodeIds.add(next);
          queue.push(next);
        }
      }
    }
  };
  walk(nodeId, outBySource, (e) => e.target); // downstream
  walk(nodeId, inByTarget, (e) => e.source); // upstream

  return { nodeIds, edgeIds };
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
