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

/** Phase 22 — one timeline per Case: jobs + runs + nodes + errors, in order. */
export interface TraceEvent {
  ts: string | null;
  kind: "job" | "run_start" | "run_end" | "node" | "channel";
  label: string;
  status?: string | null;
  summary?: string | null;
  error?: string | null;
  data?: Record<string, unknown>;
}
export interface TraceResult {
  key: string;
  sf_id: string | null;
  case_number: string | null;
  counts: { runs: number; jobs: number; events: number; errors: number };
  outcome: string | null;
  human_action: string | null;
  flow_version: number | null;
  degraded_llm: boolean;
  stale_jobs: string[];
  failed_jobs: string[];
  errors: string[];
  labels_written: Record<string, unknown>;
  labels_skipped: Record<string, unknown>;
  final_queue: string | null;
  total_ms: number;
  total_tokens: number;
  timeline: TraceEvent[];
}

/** Phase 20o — Salesforce routing metadata for the flow editor's dropdowns
 *  (notify / clarify node forms). `available:false` when the API has no SF creds. */
export interface SfMeta {
  available: boolean;
  queues: { id: string; name: string; developer_name: string | null }[];
  case_types: string[];
  modules: string[];
  case_fields?: SalesforceCaseField[];
  users?: { id: string; name: string; email: string | null }[];
  error?: string;
}

/** Slack workspace metadata for the flow editor's pickers (`notify_human`
 *  channel / @mention fields). `available:false` when the tenant hasn't
 *  connected Slack. */
export interface SlackMeta {
  available: boolean;
  channels: { id: string; name: string; is_member: boolean }[];
  users: { id: string; name: string; email: string | null }[];
  usergroups: { id: string; handle: string; name: string }[];
  errors?: string[];
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
  provisional_count?: number;
  created_at?: string;
}

export interface KbEntryRow {
  entry_id: string;
  title: string;
  status: string; // active | provisional | superseded | archived
  chunk_count: number;
  embedded_at: string | null;
  updated_at: string;
  updated_by: string | null;
  origin?: "manual" | "gdoc" | "file" | "crawl" | "import" | "review_writeback";
  gdoc_url?: string | null;
  synced_at?: string | null;
  sync_error?: string | null;
  provisional_until?: string | null;
  supersedes_entry_id?: string | null;
  source_review_task?: string | null;
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
  tenant_id?: string;
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

export interface FreshchatChannel {
  tenant_id: string;
  configured: boolean;
  status: "none" | "inactive" | "active" | "error";
  domain?: string;
  team?: string;
  auto_send_enabled?: boolean;
  signature_verification?: boolean;
}

export interface FreshchatChannelSave {
  tenant_id?: string;
  domain?: string;
  team?: string;
  api_token?: string;
  webhook_public_key?: string;
  auto_send_enabled?: boolean;
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

// ── KIL-f: Knowledge Integrity Loop review queue + metrics ─────────────
export interface IntegrityVerdict {
  relation: "entails" | "neutral" | "contradicts";
  flagged: boolean;
  novel: boolean;
  salient: string[];
  verdicts: { claim: string; relation: string; evidence: string; confidence: number }[];
  backend: string;
}

export interface ReviewTask {
  id: string;
  case_sf_id: string | null;
  case_number: string | null;
  run_id: string | null;
  kind: "human_reply_review" | "sample";
  trigger: "contradicts" | "novel" | "sample" | null;
  statement: string | null;
  verdict: IntegrityVerdict;
  contexts: { ref: string | null; kind: string | null; text: string }[];
  status: "open" | "correct" | "wrong" | "dismissed";
  reviewer_id: string | null;
  reviewed_at: string | null;
  kb_change_id: string | null;
  created_at: string;
}

export interface KilMetrics {
  window_days: number;
  review: {
    total: number;
    open: number;
    by_trigger: Record<string, number>;
    by_status: Record<string, number>;
    resolved: number;
    flag_precision: number | null;
    false_flag_rate: number | null;
    agent_correction_rate: number | null;
    median_time_to_review_h: number | null;
  };
  kb_writeback: {
    entries: number;
    provisional: number;
    active: number;
    superseded: number;
    promotion_rate: number | null;
  };
  knowledge_freshness_days: number | null;
  weekly: { week: string; flagged: number }[];
}

export interface KilDigest {
  week_of: string;
  this_week: KilMetrics;
  deltas: {
    flagged: number;
    flag_precision: number | null;
    false_flag_rate: number | null;
    knowledge_freshness_days: number | null;
  };
  top_contradictions: { claim: string; count: number }[];
  recent_kb_changes: { title: string; status: string; created_at: string }[];
  markdown: string;
}

export interface FlowTrigger {
  trigger_id: string;
  kind: "webhook" | "schedule";
  cron: string | null;
  label: string | null;
  enabled: boolean;
  url?: string;                 // webhook only
  last_fired_at: string | null;
  fire_count: number;
  created_at: string;
}

export interface Connection {
  slug: string;
  base_url: string;
  auth: { type?: string; header_name?: string; username?: string };
  has_secret: boolean;
  created_at: string;
}

export interface ConnectionAction {
  action_id?: string;
  name: string;
  method: string;
  path: string;
  params: ConnectorParam[];
  body_template?: unknown;
}

// FR-47 — one param on a connector action's declared shape (GET /api/connectors).
export interface ConnectorParam {
  key: string;
  label: string;
  type: "string" | "template" | "json" | "select";
  required?: boolean;
  options?: string[];
}

export interface ConnectorAction {
  name: string;
  description: string;
  params: ConnectorParam[];
}

export interface Connector {
  slug: string;
  label: string;
  auth: "builtin" | "apikey" | "oauth2" | "none";
  actions: ConnectorAction[];
}

export interface LlmKeyStatus {
  tenant_id: string;
  tenant: { groq: boolean; anthropic: boolean; openrouter: boolean };
  platform: { groq: boolean; anthropic: boolean; openrouter: boolean };
}

export interface ModelInfo {
  id: string;
  provider: "groq" | "anthropic" | "openrouter";
  available: boolean;
}

export interface ModelsResp {
  models: ModelInfo[];
  default_model: string;
  fast_model: string;
}

export interface SalesforceOrg {
  org_label: string;
  SF_USERNAME?: string;
  SF_DOMAIN?: string;
  SF_OAUTH_INSTANCE_URL?: string;
  has_credentials: boolean;
  updated_at: string;
}

export interface SalesforceCaseField {
  name: string;
  label: string;
  type: string;
  custom: boolean;
  picklist_values: { value: string; label: string }[];
}

export interface SalesforceQueue {
  id: string;
  name: string;
  developer_name: string | null;
}

export interface SalesforceOrgSchema {
  case_fields: SalesforceCaseField[];
  queues: SalesforceQueue[];
  errors: string[];
}

export interface ZendeskConnection {
  tenant_id: string;
  configured: boolean;
  status: "none" | "inactive" | "active" | "error";
  subdomain?: string;
  email?: string;
}

export interface ZendeskConnectionSave {
  tenant_id?: string;
  subdomain?: string;
  email?: string;
  api_token?: string;
}

export interface CaseConnector {
  tenant_id: string;
  case_connector: string;
}

export interface CaseTaxonomyRule {
  keywords: string[];
  module?: string;
  submodule?: string;
  case_type?: string;
}

export interface CaseTaxonomyConfig {
  module_rules?: CaseTaxonomyRule[];
  submodule_rules?: Record<string, CaseTaxonomyRule[]>;
  region_by_country?: Record<string, string>;
  case_type_rules?: CaseTaxonomyRule[];
}

export interface CaseTaxonomy {
  tenant_id: string;
  config: CaseTaxonomyConfig;
  updated_at: string | null;
  defaults: Required<CaseTaxonomyConfig>;
}

export interface BillingUsage {
  period_label: string;                 // "2026-09"
  period: { start: string; end: string };
  plan: string;                         // "free" | "pro"
  limits: { runs: number | null; tokens: number | null };
  runs_count: number;
  tokens_total: number;
  tokens_by_model: Record<string, number>;
  by_flow: { flow_id: string; name: string; runs: number; tokens: number; estimated_cost_usd: number }[];
  estimated_cost_usd: number;
  daily: { date: string; runs: number; tokens: number }[];
  pct_runs_used: number | null;
  pct_tokens_used: number | null;
}

export interface AuditEvent {
  event_id: number;
  tenant_id: string;
  actor_id: string | null;
  actor_email: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  summary: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface TemplateMeta {
  id: string;
  name: string;
  category: string;
  description: string;
  source: "built-in" | "custom";
}

export interface KbExportBundle {
  collection: { name: string | null; description: string | null };
  entries: { title: string; body_md: string; status: string }[];
}
