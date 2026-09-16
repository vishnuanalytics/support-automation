import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { AuditEvent } from "../types";
import { Button, Tag, Banner, DataTable, EmptyState, DateRangeFilter, type Column, type DateRange } from "../ui";

export function ActivityView({ tenantId }: { tenantId: string }) {
  const [rows, setRows] = useState<AuditEvent[]>([]);
  const [filter, setFilter] = useState("");
  const [range, setRange] = useState<DateRange>({ since: null, until: null });
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .listAudit({
        action: filter || undefined,
        since: range.since ?? undefined,
        until: range.until ?? undefined,
        limit: 200,
        tenantId,
      })
      .then(setRows)
      .catch((e: ApiError) => setErr(e.message));
  }, [filter, range, tenantId]);

  const actions = useMemo(() => Array.from(new Set(rows.map((r) => r.action))).sort(), [rows]);

  // Grouped by namespace (the part before the first ".") instead of a flat
  // wall of ~30 individually-styled chip buttons — one per distinct event
  // kind gets overwhelming fast. "flow.created"/"flow.deleted"/… collapse
  // into one "flow" optgroup with just the verb shown per option.
  const actionGroups = useMemo(() => {
    const byNamespace = new Map<string, string[]>();
    for (const a of actions) {
      const ns = a.includes(".") ? a.slice(0, a.indexOf(".")) : "other";
      (byNamespace.get(ns) ?? byNamespace.set(ns, []).get(ns)!).push(a);
    }
    return Array.from(byNamespace.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [actions]);

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
      <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span className="muted" style={{ fontSize: 13 }}>Filter by action</span>
        <span className="ui-select-wrap">
          <select
            className="ui-select"
            style={{ minWidth: 220 }}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            <option value="">All actions</option>
            {actionGroups.map(([ns, items]) => (
              <optgroup key={ns} label={ns}>
                {items.map((a) => (
                  <option key={a} value={a}>
                    {ns === "other" ? a : a.slice(ns.length + 1)}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </span>
        {filter && (
          <Button variant="ghost" size="sm" onClick={() => setFilter("")}>
            Clear ✕
          </Button>
        )}
        <DateRangeFilter value={range} onChange={setRange} />
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
