import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { FlowCandidate, FlowMeta } from "../types";

export function FlowList({
  activeId,
  canEdit,
  onSelect,
  onCreated,
}: {
  activeId: string | null;
  canEdit: boolean;
  onSelect: (id: string) => void;
  onCreated: (id: string) => void;
}) {
  const [flows, setFlows] = useState<FlowMeta[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [templates, setTemplates] = useState<
    { id: string; name: string; description: string }[]
  >([]);

  useEffect(() => {
    api.templates.list().then(setTemplates).catch(() => {});
  }, []);

  async function fromTemplate(id: string) {
    if (!id) return;
    try {
      const cand = await api.templates.graph(id);
      await createWithHandoff(cand.name || "New flow", { candidate: cand });
    } catch (e) {
      alert((e as ApiError).message);
    }
  }

  useEffect(() => {
    api
      .listFlows()
      .then(setFlows)
      .catch((e: ApiError) => setErr(e.message));
  }, []);

  async function newFlow() {
    // tenant is inferred from the caller's membership — no id to type
    const team = prompt("team (support / csm / offboarding / …)", "support");
    if (!team?.trim()) return;
    const name = (prompt("flow name", "Untitled flow") || "Untitled flow").trim();
    try {
      const { flow_id } = await api.createFlow({ team: team.trim(), name });
      onCreated(flow_id);
    } catch (e) {
      alert((e as ApiError).message);
    }
  }

  /** create an empty flow, stash a proposed graph for the editor to load
   *  as an unsaved draft (Phase 19). */
  async function createWithHandoff(
    defaultName: string,
    handoff: { candidate?: FlowCandidate; mermaidPrompt?: boolean },
  ) {
    const team = prompt("team (support / csm / offboarding / …)", "support");
    if (!team?.trim()) return;
    const name = (prompt("flow name", defaultName) || defaultName).trim();
    try {
      const { flow_id } = await api.createFlow({ team: team.trim(), name });
      if (handoff.candidate) {
        sessionStorage.setItem(
          `pendingCandidate:${flow_id}`,
          JSON.stringify(handoff.candidate),
        );
      } else if (handoff.mermaidPrompt) {
        sessionStorage.setItem(`pendingAssistMode:${flow_id}`, "mermaid");
      }
      onCreated(flow_id);
    } catch (e) {
      alert((e as ApiError).message);
    }
  }

  async function fromPrompt() {
    const p = prompt(
      "Describe the support flow you want — e.g. “retrieve docs, triage by tier, " +
        "draft a reply, auto-send only if confident, otherwise ask a human”",
    );
    if (!p?.trim()) return;
    try {
      const res = await api.assistNewFlow(p.trim());
      await createWithHandoff(res.name || "AI flow", { candidate: res });
    } catch (e) {
      alert((e as ApiError).message);
    }
  }

  const byTenant = flows.reduce<Record<string, FlowMeta[]>>((acc, f) => {
    (acc[f.tenant_id] ||= []).push(f);
    return acc;
  }, {});

  return (
    <div className="col">
      {canEdit && (
        <div className="row" style={{ flexWrap: "wrap", gap: 4 }}>
          <button onClick={newFlow}>＋ New flow</button>
          <button onClick={fromPrompt} title="describe it in plain English, AI drafts the graph">
            ✨ From prompt
          </button>
          <button
            onClick={() => createWithHandoff("Imported flow", { mermaidPrompt: true })}
            title="start from a Mermaid flowchart"
          >
            ⬇ From Mermaid
          </button>
          {templates.length > 0 && (
            <select
              value=""
              title="start from a ready-made flow"
              onChange={(e) => {
                void fromTemplate(e.target.value);
                e.currentTarget.value = "";
              }}
            >
              <option value="">📋 From template…</option>
              {templates.map((t) => (
                <option key={t.id} value={t.id} title={t.description}>
                  {t.name}
                </option>
              ))}
            </select>
          )}
        </div>
      )}
      {err && <div className="err">{err}</div>}
      {Object.entries(byTenant).map(([tenant, list]) => (
        <div key={tenant}>
          <h3 title={tenant}>tenant …{tenant.slice(0, 8)}</h3>
          {list.map((f) => (
            <div
              key={f.flow_id}
              className={`flow-item${f.flow_id === activeId ? " active" : ""}`}
              onClick={() => onSelect(f.flow_id)}
            >
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span>{f.team}</span>
                <span className={`pill ${f.status}`}>{f.status}</span>
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                {f.name}
              </div>
            </div>
          ))}
        </div>
      ))}
      {flows.length === 0 && !err && <div className="muted">no flows visible</div>}
    </div>
  );
}
