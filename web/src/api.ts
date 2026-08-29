import { supabase } from "./supabase";
import type {
  Flow,
  FlowMeta,
  FlowVersion,
  ActionRequest,
  GoogleStatus,
  KbCollection,
  KbEntry,
  KbEntryRow,
  PolicyRule,
  NodeTypesResp,
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
  createFlow: (b: { tenant_id: string; team: string; name: string; status?: string }) =>
    req<{ flow_id: string }>("/flows", { method: "POST", body: JSON.stringify(b) }),
  getFlow: (id: string) => req<Flow>(`/flows/${id}`),
  saveFlow: (id: string, b: Partial<Flow>) =>
    req<Flow>(`/flows/${id}`, { method: "PUT", body: JSON.stringify(b) }),
  deleteFlow: (id: string) => req<string>(`/flows/${id}`, { method: "DELETE" }),
  publishFlow: (id: string) =>
    req<{ published_version: number }>(`/flows/${id}/publish`, { method: "POST" }),
  listVersions: (id: string) => req<FlowVersion[]>(`/flows/${id}/versions`),
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
