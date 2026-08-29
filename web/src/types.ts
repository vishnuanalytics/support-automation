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
  published_version: number | null;
  updated_at?: string;
}

export interface Flow extends FlowMeta {
  flow_version?: number | null;
  nodes: FlowNode[];
  edges: FlowEdge[];
}

export interface FlowVersion {
  version: number;
  name: string;
  definition_hash: string;
  created_by: string | null;
  created_at: string;
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

export interface Retrieved {
  doc_url: string;
  heading_path: string | null;
  rerank_score: number | null;
}

export interface RunResult {
  run_id?: string | null;
  trace: TraceStep[];
  outcome: Record<string, unknown> | null;
  tier?: string;
  region?: string;
  confidence?: number;
  confidence_gate?: Record<string, unknown>;
  sf_writeback?: Record<string, unknown>;
  query?: string;
  retrieval: Retrieved[];
}

export interface RunRow {
  run_id: string;
  flow_id: string;
  team: string;
  tier: string | null;
  region: string | null;
  outcome: string | null;
  confidence: number | null;
  subject: string | null;
  source: "api" | "cli" | "worker";
  human_action: string | null;
  edit_distance: number | null;
  created_at: string;
}

export interface RunDetail extends RunRow {
  gate: Record<string, unknown> | null;
  trace: TraceStep[];
  retrieval: Retrieved[];
  sf_writeback: Record<string, unknown> | null;
  case_payload: Record<string, unknown> | null;
  draft: string | null;
  human_reply: string | null;
}

export interface RunStats {
  total: number;
  by_outcome: Record<string, number>;
  by_tier: Record<string, number>;
  low_confidence: number;
  by_human_action: Record<string, number>;
  draft_acceptance: number | null;
}

export interface KbCollection {
  source_id: string;
  name: string;
  description: string | null;
  tenant_id: string;
  entry_count: number;
  created_at?: string;
}

export interface KbEntryRow {
  entry_id: string;
  title: string;
  status: string;
  chunk_count: number;
  embedded_at: string | null;
  updated_at: string;
  updated_by: string | null;
  origin?: "manual" | "gdoc";
  gdoc_url?: string | null;
  synced_at?: string | null;
  sync_error?: string | null;
}

export interface KbEntry extends KbEntryRow {
  source_id: string;
  tenant_id: string;
  body_md: string;
  embed_hash: string | null;
  gdoc_id?: string | null;
}

export interface GoogleStatus {
  configured: boolean;
  connected: Record<string, boolean>; // tenant_id -> connected
}

export interface PolicyRule {
  rule_id: string;
  tenant_id: string;
  team: string;
  name: string;
  priority: number;
  when: Record<string, unknown>;
  then: Record<string, unknown>;
  status: "active" | "disabled";
  updated_at: string;
}

export interface ActionRequest {
  id: string;
  tenant_id: string;
  run_id: string | null;
  rule_name: string | null;
  kind: string;
  payload: Record<string, unknown>;
  status: "pending" | "approved" | "rejected" | "expired" | "done" | "error";
  slack_channel: string | null;
  decided_by: string | null;
  decided_at: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
}
