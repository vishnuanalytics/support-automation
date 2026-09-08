import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { CaseMemoryStats } from "../types";
import { Banner, Button, Field, Input } from "../ui";

/**
 * Pull a tenant's past resolved Salesforce cases over a chosen window into
 * `case_memory`, so `case_lookup` can cite real prior resolutions from day
 * one. Runs the `--from-salesforce` sync bounded to [from, to) against the
 * tenant's own connected org — no export, no upload.
 */

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function CaseHistoryPanel({ tenantId }: { tenantId: string }) {
  const today = new Date();
  const ninetyAgo = new Date(today.getTime() - 90 * 864e5);

  const [stats, setStats] = useState<CaseMemoryStats | null>(null);
  const [from, setFrom] = useState(isoDay(ninetyAgo));
  const [to, setTo] = useState(isoDay(today));
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api.kb
      .caseMemoryStats(tenantId)
      .then(setStats)
      .catch(() => setStats(null));
  }, [tenantId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function pull() {
    if (!from || !to || from >= to) {
      setErr("pick a start date before the end date");
      return;
    }
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      const ack = await api.kb.caseBackfill({ date_from: from, date_to: to }, tenantId);
      setNote(
        `Pull queued for ${ack.since.slice(0, 10)} → ${ack.until.slice(0, 10)}. ` +
          "Resolved cases will land in case memory over the next few minutes — refresh the count.",
      );
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  const span =
    stats?.earliest_resolved_at && stats?.latest_resolved_at
      ? `${stats.earliest_resolved_at.slice(0, 10)} → ${stats.latest_resolved_at.slice(0, 10)}`
      : null;

  return (
    <section
      className="col"
      style={{
        gap: "var(--space-2)",
        padding: "var(--space-3)",
        border: "1px solid var(--line)",
        borderRadius: "var(--radius-md)",
        background: "var(--surface-raised)",
      }}
    >
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <span style={{ font: "var(--type-section)" }}>Case history</span>
        <span className="muted" style={{ fontSize: 12 }}>
          {stats
            ? `${stats.active} resolution${stats.active === 1 ? "" : "s"} in memory${
                span ? ` · ${span}` : ""
              }`
            : "…"}
        </span>
      </div>

      <div className="muted" style={{ fontSize: 12, maxWidth: 720 }}>
        Pull your resolved Salesforce cases from a date range so the bot can cite real
        prior resolutions immediately. It reads closed cases and their last support reply
        from your connected org and embeds them into case memory. This is retrieval
        memory, not model training. Up to a year per pull; run it again for older windows.
      </div>

      {err && <Banner tone="exception" title={err} />}
      {note && <Banner tone="accent" title={note} />}

      <div className="row" style={{ gap: "var(--space-2)", flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label="From (case closed on/after)">
          <Input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} />
        </Field>
        <Field label="To (before)">
          <Input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} />
        </Field>
        <Button variant="primary" size="sm" loading={busy} onClick={pull}>
          Pull from Salesforce
        </Button>
        <Button size="sm" variant="ghost" onClick={refresh}>
          Refresh count
        </Button>
      </div>

      {stats && Object.keys(stats.by_kind).length > 0 && (
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          {Object.entries(stats.by_kind).map(([k, n]) => (
            <span key={k} className="pill" style={{ fontSize: 11 }}>
              {k} · {n}
            </span>
          ))}
        </div>
      )}

      <div className="muted" style={{ fontSize: 11 }}>
        Needs Salesforce connected for this workspace (Connections → Salesforce).
      </div>
    </section>
  );
}
