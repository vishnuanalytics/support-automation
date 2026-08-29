import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { FlowMeta } from "../types";

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

  const byTenant = flows.reduce<Record<string, FlowMeta[]>>((acc, f) => {
    (acc[f.tenant_id] ||= []).push(f);
    return acc;
  }, {});

  return (
    <div className="col">
      {canEdit && <button onClick={newFlow}>＋ New flow</button>}
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
