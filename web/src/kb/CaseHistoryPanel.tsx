import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { CaseMemoryStats } from "../types";
import { Banner, Button } from "../ui";

/**
 * Bootstrap `case_memory` from a tenant's historical cases so `case_lookup`
 * has real prior resolutions to cite from day one — for tenants whose
 * Salesforce isn't reachable (sandbox / permissions), upload a Bulk-API
 * export (EmailMessage + optional Case) or a flat CSV.
 */

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

export function CaseHistoryPanel({ tenantId }: { tenantId: string }) {
  const [stats, setStats] = useState<CaseMemoryStats | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(() => {
    api.kb
      .caseMemoryStats(tenantId)
      .then(setStats)
      .catch(() => setStats(null));
  }, [tenantId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function submit() {
    if (files.length === 0) return;
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      const payload = await Promise.all(
        files.map(async (f) => ({ filename: f.name, content_b64: await readAsDataUrl(f) })),
      );
      const ack = await api.kb.caseImport(payload, tenantId);
      const kinds = Object.values(ack.files).join(", ");
      setNote(
        `Import queued (${kinds}). Resolved cases will appear in case memory in a minute or two — refresh the count.`,
      );
      setFiles([]);
      if (inputRef.current) inputRef.current.value = "";
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
        Load past solved cases so the bot can cite real prior resolutions from day one.
        Upload a Salesforce Bulk-API export — an <code>EmailMessage</code> query result,
        and optionally a <code>Case</code> query result for status/type — or a CSV with{" "}
        <code>subject</code>, <code>problem</code>, <code>resolution</code>,{" "}
        <code>resolved_at</code> columns. Each file up to 12&nbsp;MB; slice a large export
        by month. This is retrieval memory, not model training.
      </div>

      {err && <Banner tone="exception" title={err} />}
      {note && <Banner tone="accent" title={note} />}

      <div className="row" style={{ gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
        <input
          ref={inputRef}
          type="file"
          accept=".xml,.csv"
          multiple
          onChange={(e) => setFiles(Array.from(e.target.files ?? []).slice(0, 2))}
        />
        <Button variant="primary" size="sm" loading={busy} disabled={files.length === 0} onClick={submit}>
          Import case history
        </Button>
        <Button size="sm" variant="ghost" onClick={refresh}>
          Refresh count
        </Button>
      </div>
      {files.length > 0 && (
        <div className="muted" style={{ fontSize: 11 }}>
          {files.map((f) => `${f.name} (${(f.size / 1024 / 1024).toFixed(1)} MB)`).join("  ·  ")}
        </div>
      )}

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
        Connected to Salesforce instead? A date-range pull from the live org is coming to
        this panel — it avoids the export step.
      </div>
    </section>
  );
}
