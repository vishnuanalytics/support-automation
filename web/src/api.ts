import { supabase } from "./supabase";
import type {
  AssistResult,
  EmailChannel,
  EmailChannelSave,
  Flow,
  FlowCandidate,
  FlowMeta,
  FlowVersion,
  FlowTrigger,
  Connection,
  ActionRequest,
  GoogleStatus,
  Invitation,
  KbCollection,
  KbEntry,
  KbEntryRow,
  Member,
  PolicyRule,
  NodeTypesResp,
  ReviewTask,
  KilMetrics,
  KilDigest,
  SfMeta,
  TraceResult,
  RunDetail,
  RunResult,
  RunRow,
  RunStats,
} from "./types";

const BASE = (import.meta.env.VITE_API_BASE as string) || ""; // "" -> vite proxy

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  const res = await fetch(`${BASE}/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    },
  });
  const body = res.headers.get("content-type")?.includes("application/json")
    ? await res.json()
    : await res.text();
  if (!res.ok) {
    const detail = (body && (body.detail ?? body)) as unknown;
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  /** structural errors from PUT/validate, if present */
  get errors(): string[] | null {
    const d = this.detail as { errors?: string[] } | null;
    return d && Array.isArray(d.errors) ? d.errors : null;
  }
}

export const api = {
  nodeTypes: () => req<NodeTypesResp>("/node-types"),
  listFlows: () => req<FlowMeta[]>("/flows"),
  listTenants: () => req<{ tenant_id: string; role: string; name?: string | null }[]>("/tenants"),
  createTenant: (name: string) =>
    req<{ tenant_id: string; name: string; role: string }>("/tenants", {
      method: "POST", body: JSON.stringify({ name }),
    }),
  createFlow: (b: { team: string; name: string; status?: string; tenant_id?: string }) =>
    req<{ flow_id: string }>("/flows", { method: "POST", body: JSON.stringify(b) }),
  getFlow: (id: string) => req<Flow>(`/flows/${id}`),
  saveFlow: (id: string, b: Partial<Flow>) =>
    req<Flow>(`/flows/${id}`, { method: "PUT", body: JSON.stringify(b) }),
  deleteFlow: (id: string) => req<string>(`/flows/${id}`, { method: "DELETE" }),
  publishFlow: (id: string) =>
    req<{ published_version: number }>(`/flows/${id}/publish`, { method: "POST" }),
  setSfEntry: (id: string, sf_entry: boolean) =>
    req<{ sf_entry: boolean }>(`/flows/${id}/sf-entry`, {
      method: "PUT",
      body: JSON.stringify({ sf_entry }),
    }),
  listVersions: (id: string) => req<FlowVersion[]>(`/flows/${id}/versions`),

  triggers: {
    list: (flowId: string) => req<FlowTrigger[]>(`/flows/${flowId}/triggers`),
    create: (flowId: string, b: { kind: "webhook" | "schedule"; cron?: string; label?: string }) =>
      req<FlowTrigger>(`/flows/${flowId}/triggers`, { method: "POST", body: JSON.stringify(b) }),
    remove: (flowId: string, id: string) =>
      req<void>(`/flows/${flowId}/triggers/${id}`, { method: "DELETE" }),
  },

  templates: {
    list: () =>
      req<{ id: string; name: string; category: string; description: string }[]>("/templates"),
    graph: (id: string) => req<FlowCandidate>(`/templates/${encodeURIComponent(id)}`),
  },

  connections: {
    list: () => req<Connection[]>("/connections"),
    create: (b: { slug: string; base_url: string; auth: Record<string, unknown> }) =>
      req<Connection>("/connections", { method: "POST", body: JSON.stringify(b) }),
    remove: (slug: string) => req<void>(`/connections/${encodeURIComponent(slug)}`, { method: "DELETE" }),
  },
  rollbackFlow: (id: string, version: number) =>
    req<{ published_version: number }>(`/flows/${id}/rollback`, {
      method: "POST",
      body: JSON.stringify({ version }),
    }),
  validateFlow: (id: string, b: Partial<Flow>) =>
    req<{ valid: boolean; errors: string[] }>(`/flows/${id}/validate`, {
      method: "POST",
      body: JSON.stringify(b),
    }),
  importMermaid: (text: string) =>
    req<FlowCandidate>("/flows/import/mermaid", {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  assistNewFlow: (prompt: string) =>
    req<AssistResult>("/flows/assist", {
      method: "POST",
      body: JSON.stringify({ prompt }),
    }),
  assistEditFlow: (id: string, instruction: string) =>
    req<AssistResult>(`/flows/${id}/assist`, {
      method: "POST",
      body: JSON.stringify({ instruction }),
    }),
  runFlow: (id: string, caseJson: Record<string, unknown>) =>
    req<RunResult>(`/flows/${id}/run`, {
      method: "POST",
      body: JSON.stringify({ case: caseJson }),
    }),

  kb: {
    listCollections: () => req<KbCollection[]>("/kb/collections"),
    createCollection: (b: { name: string; description?: string; tenant_id?: string }) =>
      req<{ source_id: string }>("/kb/collections", { method: "POST", body: JSON.stringify(b) }),
    updateCollection: (id: string, b: { name?: string; description?: string }) =>
      req<KbCollection>(`/kb/collections/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
    deleteCollection: (id: string) =>
      req<void>(`/kb/collections/${id}`, { method: "DELETE" }),
    listEntries: (id: string) => req<KbEntryRow[]>(`/kb/collections/${id}/entries`),
    createEntry: (id: string, b: { title: string; body_md: string }) =>
      req<KbEntry>(`/kb/collections/${id}/entries`, { method: "POST", body: JSON.stringify(b) }),
    upload: (id: string, b: { filename: string; content_b64: string }) =>
      req<KbEntry>(`/kb/collections/${id}/upload`, { method: "POST", body: JSON.stringify(b) }),
    crawl: (id: string, url: string, max_pages = 20) =>
      req<{ job_id: string }>(`/kb/collections/${id}/crawl`, {
        method: "POST",
        body: JSON.stringify({ url, max_pages }),
      }),
    getEntry: (id: string) => req<KbEntry>(`/kb/entries/${id}`),
    updateEntry: (id: string, b: { title?: string; body_md?: string }) =>
      req<KbEntry>(`/kb/entries/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
    deleteEntry: (id: string) => req<void>(`/kb/entries/${id}`, { method: "DELETE" }),
    linkGdoc: (id: string, doc_url: string) =>
      req<KbEntry>(`/kb/collections/${id}/gdoc`, { method: "POST", body: JSON.stringify({ doc_url }) }),
    resyncGdoc: (entryId: string) =>
      req<KbEntry>(`/kb/entries/${entryId}/resync`, { method: "POST" }),
  },

  google: {
    status: () => req<GoogleStatus>("/integrations/google/status"),
    authorize: (tenant_id: string) =>
      req<{ url: string }>(`/integrations/google/authorize?tenant_id=${tenant_id}`),
  },

  slack: {
    status: () => req<GoogleStatus>("/integrations/slack/status"),
    authorize: (tenant_id: string) =>
      req<{ url: string }>(`/integrations/slack/authorize?tenant_id=${tenant_id}`),
  },

  email: {
    status: () => req<EmailChannel>("/integrations/email"),
    save: (b: EmailChannelSave) =>
      req<EmailChannel>("/integrations/email", { method: "PUT", body: JSON.stringify(b) }),
    remove: () => req<void>("/integrations/email", { method: "DELETE" }),
    test: (b: EmailChannelSave) =>
      req<{ ok: boolean; imap?: boolean; smtp?: boolean; error: string | null }>(
        "/integrations/email/test",
        { method: "POST", body: JSON.stringify(b) },
      ),
    googleAuthorize: () => req<{ url: string }>("/integrations/email/google/authorize"),
  },

  salesforce: {
    meta: () => req<SfMeta>("/salesforce/meta"),
  },

  trace: {
    get: (key: string) => req<TraceResult>(`/trace/${encodeURIComponent(key.trim())}`),
    md: (key: string) => req<string>(`/trace/${encodeURIComponent(key.trim())}?format=md`),
    retry: (key: string) =>
      req<{ job_id: string; sf_id: string }>(
        `/trace/${encodeURIComponent(key.trim())}/retry`, { method: "POST" }),
  },

  acceptInvitations: () =>
    req<{ accepted: number }>("/invitations/accept", { method: "POST" }),
  team: {
    members: () => req<Member[]>("/members"),
    removeMember: (userId: string) =>
      req<void>(`/members/${userId}`, { method: "DELETE" }),
    invitations: () => req<Invitation[]>("/invitations"),
    invite: (b: { email: string; role: "editor" | "viewer" }) =>
      req<Invitation>("/invitations", { method: "POST", body: JSON.stringify(b) }),
    revoke: (id: string) => req<void>(`/invitations/${id}`, { method: "DELETE" }),
  },

  rules: {
    list: (team?: string) =>
      req<PolicyRule[]>(`/rules${team ? `?team=${encodeURIComponent(team)}` : ""}`),
    create: (b: Partial<PolicyRule> & { team: string; name: string }) =>
      req<PolicyRule>("/rules", { method: "POST", body: JSON.stringify(b) }),
    update: (id: string, b: Partial<PolicyRule>) =>
      req<PolicyRule>(`/rules/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
    remove: (id: string) => req<void>(`/rules/${id}`, { method: "DELETE" }),
  },

  actionRequests: (limit = 50) => req<ActionRequest[]>(`/action-requests?limit=${limit}`),

  review: {
    list: (status = "open") =>
      req<ReviewTask[]>(`/review-tasks?status=${encodeURIComponent(status)}`),
    resolve: (id: string, status: "correct" | "wrong" | "dismissed") =>
      req<{ task: ReviewTask; kb_change: unknown }>(`/review-tasks/${id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ status }),
      }),
    metrics: (days = 30) => req<KilMetrics>(`/kil/metrics?days=${days}`),
    digest: (weeks = 4) =>
      req<KilDigest>(`/kil/digest?weeks=${weeks}`),
  },

  approvals: {
    list: () =>
      req<{ review_tasks: ReviewTask[]; action_requests: ActionRequest[] }>("/approvals"),
    decide: (arId: string, decision: "approve" | "reject") =>
      req<{ status: string; job_kind: string | null }>(
        `/approvals/action-requests/${arId}`,
        { method: "POST", body: JSON.stringify({ decision }) },
      ),
  },

  runStats: () => req<RunStats>("/runs/stats"),
  listRuns: (q: { flow_id?: string; outcome?: string; limit?: number } = {}) => {
    const p = new URLSearchParams();
    if (q.flow_id) p.set("flow_id", q.flow_id);
    if (q.outcome) p.set("outcome", q.outcome);
    if (q.limit) p.set("limit", String(q.limit));
    const qs = p.toString();
    return req<RunRow[]>(`/runs${qs ? `?${qs}` : ""}`);
  },
  getRun: (id: string) => req<RunDetail>(`/runs/${id}`),
};
