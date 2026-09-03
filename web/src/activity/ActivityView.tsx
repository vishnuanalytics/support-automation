import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { AuditEvent } from "../types";

export function ActivityView({ tenantId }: { tenantId: string }) {
  const [rows, setRows] = useState<AuditEvent[]>([]);
  const [filter, setFilter] = useState("");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .listAudit({ action: filter || undefined, limit: 200, tenantId })
      .then(setRows)
      .catch((e: ApiError) => setErr(e.message));
  }, [filter, tenantId]);

  const actions = useMemo(
    () => Array.from(new Set(rows.map((r) => r.action))).sort(),
    [rows],
  );

  return (
    <div className="activity-view col" style={{ padding: 12, overflow: "auto" }}>
      <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
        <button className={filter === "" ? "primary" : ""} onClick={() => setFilter("")}>
          all
        </button>
        {actions.map((a) => (
          <button key={a} className={filter === a ? "primary" : ""} onClick={() => setFilter(a)}>
            {a}
          </button>
        ))}
      </div>

      {err && <div className="banner err">{err}</div>}

      <table className="runs-table">
        <thead>
          <tr>
            <th>when</th>
            <th>actor</th>
            <th>action</th>
            <th>target</th>
            <th>summary</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.event_id}>
              <td className="muted">{new Date(r.created_at).toLocaleString()}</td>
              <td className="muted">{r.actor_email ?? "system"}</td>
              <td>
                <span className="pill">{r.action}</span>
              </td>
              <td className="muted">
                {r.target_type ? `${r.target_type}${r.target_id ? ` · ${r.target_id}` : ""}` : "—"}
              </td>
              <td>{r.summary ?? ""}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={5} className="muted">
                no activity yet — publishing a flow, approving a KB change, or managing
                connections/members will show up here
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
