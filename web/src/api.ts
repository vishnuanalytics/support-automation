import { supabase } from "./supabase";
import type { Flow, FlowMeta, NodeTypesResp, RunResult } from "./types";

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
};
