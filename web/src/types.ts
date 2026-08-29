export type NodeType =
  | "retrieve" | "classify" | "sf_writeback" | "draft"
  | "confidence_gate" | "auto_reply" | "ask_human" | "handover"
  | string;

export interface FlowNode {
  node_id: string;
  type: NodeType;
  label: string | null;
  position_x: number | null;
  position_y: number | null;
  config: Record<string, unknown>;
}

export interface FlowEdge {
  edge_id: string;
  source_node_id: string;
  target_node_id: string;
  condition: Record<string, unknown>; // {} or {"if": "..."}
}

export interface FlowMeta {
  flow_id: string;
  tenant_id: string;
  team: string;
  name: string;
  status: "draft" | "published" | "archived";
  version: number;
  updated_at?: string;
}

export interface Flow extends FlowMeta {
  nodes: FlowNode[];
  edges: FlowEdge[];
}

export interface NodeTypesResp {
  types: string[];
  defaults: Record<string, Record<string, unknown>>;
}

export interface TraceStep {
  node_id: string;
  type: string;
  summary: string;
  data: Record<string, unknown>;
}

export interface RunResult {
  trace: TraceStep[];
  outcome: Record<string, unknown> | null;
  tier?: string;
  region?: string;
  confidence?: number;
  confidence_gate?: Record<string, unknown>;
  sf_writeback?: Record<string, unknown>;
  query?: string;
  retrieval: { doc_url: string; heading_path: string | null; rerank_score: number | null }[];
}
