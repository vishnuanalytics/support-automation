export type NodeType =
  | "retrieve" | "classify" | "sf_writeback" | "draft"
  | "confidence_gate" | "auto_reply" | "ask_human" | "handover"
  | "team_route" | "notify" | "clarify" | "identify" | "case_lookup"
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
  sf_entry?: boolean;          // the Salesforce Case hook runs this flow
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

/** Phase 20o — Salesforce routing metadata for the flow editor's dropdowns
 *  (notify / clarify node forms). `available:false` when the API has no SF creds. */
export interface SfMeta {
  available: boolean;
  queues: { id: string; name: string; developer_name: string | null }[];
  case_types: string[];
  modules: string[];
  error?: string;
}

/** Phase 19 — a proposed flow graph (from Mermaid import or AI assist),
 *  loaded onto the editor canvas as unsaved state; never persisted as-is. */
export interface FlowCandidate {
  name: string | null;
  nodes: FlowNode[];
  edges: FlowEdge[];
  warnings: string[];
  errors: string[];
}

export interface AssistResult extends FlowCandidate {
  summary?: string | null;
  diff?: GraphDiff | null;
}

export interface GraphDiff {
  added_nodes: string[];
  removed_nodes: string[];
  changed_nodes: string[];
  added_edges: number;
  removed_edges: number;
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

export interface EmailChannel {
  tenant_id: string;
  gmail_available: boolean;
  configured: boolean;
  status: "none" | "inactive" | "active" | "error";
  provider?: "imap" | "gmail";
  team?: string;
  username?: string;
  from_addr?: string;
  from_name?: string;
  no_reply_addr?: string | null;
  imap_host?: string;
  imap_port?: number;
  smtp_host?: string;
  smtp_port?: number;
  folder?: string;
  auto_send_enabled?: boolean;
  last_poll_at?: string | null;
  last_error?: string | null;
}

export interface EmailChannelSave {
  provider: "imap" | "gmail";
  team?: string;
  imap_host?: string;
  imap_port?: number;
  smtp_host?: string;
  smtp_port?: number;
  username?: string;
  password?: string;
  from_addr?: string;
  from_name?: string;
  no_reply_addr?: string;
  folder?: string;
  auto_send_enabled?: boolean;
  active?: boolean;
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

export interface Member {
  user_id: string;
  role: "owner" | "editor" | "viewer";
  email: string;
  is_you: boolean;
}

export interface Invitation {
  invite_id: string;
  tenant_id: string;
  email: string;
  role: "editor" | "viewer";
  status: "pending" | "accepted" | "revoked";
  invited_by: string | null;
  created_at: string;
  accepted_at: string | null;
}
