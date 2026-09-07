import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { AuditEvent } from "../types";
import { Button, Tag, Banner, DataTable, EmptyState, type Column } from "../ui";

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

  const actions = useMemo(() => Array.from(new Set(rows.map((r) => r.action))).sort(), [rows]);

  const columns: Column<AuditEvent>[] = [
    {
      key: "when",
      header: "when",
      cell: (r) => (
        <span className="muted" style={{ font: "var(--type-mono)" }}>
          {new Date(r.created_at).toLocaleString()}
        </span>
      ),
    },
    { key: "actor", header: "actor", cell: (r) => <span className="muted">{r.actor_email ?? "system"}</span> },
    { key: "action", header: "action", cell: (r) => <Tag tone="neutral">{r.action}</Tag> },
    {
      key: "target",
      header: "target",
      cell: (r) => (
        <span className="muted" style={{ font: "var(--type-mono)" }}>
          {r.target_type ? `${r.target_type}${r.target_id ? ` · ${r.target_id}` : ""}` : "—"}
        </span>
      ),
    },
    { key: "summary", header: "summary", cell: (r) => r.summary ?? "" },
  ];

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
        <Button variant={filter === "" ? "primary" : "ghost"} size="sm" onClick={() => setFilter("")}>
          all
        </Button>
        {actions.map((a) => (
          <Button
            key={a}
            variant={filter === a ? "primary" : "ghost"}
            size="sm"
            onClick={() => setFilter(a)}
          >
            {a}
          </Button>
        ))}
      </div>

      {err && <Banner tone="exception" title={err} />}

      <DataTable
        flush
        columns={columns}
        rows={rows}
        rowId={(r) => String(r.event_id)}
        empty={
          <EmptyState
            title="No activity yet"
            body="Publishing a flow, approving a KB change, or managing connections and members shows up here."
          />
        }
      />
    </div>
  );
}
