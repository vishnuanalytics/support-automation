import { useState } from "react";
import { api, ApiError } from "../api";
import type { TraceEvent, TraceResult } from "../types";

const MARK: Record<TraceEvent["kind"], string> = {
  job: "▸",
  run_start: "┌",
  run_end: "└",
  node: "•",
  channel: "✉",
};

function chip(text: string, tone: "ok" | "warn" | "err" | "muted" = "muted") {
  const bg = { ok: "#1f7a3d", warn: "#8a6d1f", err: "#8a1f1f", muted: "var(--border)" }[tone];
  return (
    <span style={{ background: bg, borderRadius: 4, padding: "1px 6px", fontSize: 11 }}>{text}</span>
  );
}

export function TraceView() {
  const [key, setKey] = useState("");
  const [t, setT] = useState<TraceResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [copied, setCopied] = useState(false);

  const load = () => {
    if (!key.trim()) return;
    setBusy(true);
    setErr(null);
    setT(null);
    setOpen(new Set());
    api.trace
      .get(key)
      .then(setT)
      .catch((e: ApiError) => setErr(e.message))
      .finally(() => setBusy(false));
  };

  const copyText = async () => {
    try {
      const md = await api.trace.md(key);
      await navigator.clipboard.writeText(typeof md === "string" ? md : JSON.stringify(md, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — ignore */
    }
  };

  const toggle = (i: number) =>
    setOpen((s) => {
      const n = new Set(s);
      n.has(i) ? n.delete(i) : n.add(i);
      return n;
    });

  return (
    <div className="col" style={{ gap: 12, maxWidth: 980 }}>
      <div className="muted" style={{ fontSize: 12 }}>
        One timeline per Case — every job, run, node and error, in order. Enter a Salesforce
        Case number, Case id, run id, or job id.
      </div>
      <div className="row" style={{ gap: 6 }}>
        <input
          value={key}
          placeholder="00001234  /  500jV…  /  run id"
          onChange={(e) => setKey(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && load()}
          style={{ minWidth: 320 }}
        />
        <button className="primary" onClick={load} disabled={busy}>
          {busy ? "…" : "trace"}
        </button>
        {t && <button onClick={copyText}>{copied ? "copied ✓" : "copy as text"}</button>}
      </div>

      {err && <div className="banner err">{err}</div>}

      {t && (
        <>
          <div
            className="col"
            style={{ gap: 6, border: "1px solid var(--border)", borderRadius: 6, padding: 10 }}
          >
            <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <strong>{t.case_number || t.sf_id || t.key}</strong>
              {t.outcome && chip(`outcome: ${t.outcome}`, "ok")}
              {t.human_action && chip(`human: ${t.human_action}`)}
              {t.flow_version != null && chip(`flow v${t.flow_version}`)}
              {t.degraded_llm && chip("LLM STUB (quota)", "warn")}
              {t.failed_jobs.length > 0 && chip(`${t.failed_jobs.length} failed job`, "err")}
              {t.stale_jobs.length > 0 && chip(`${t.stale_jobs.length} stale job`, "warn")}
              {chip(`${t.total_ms} ms`)}
              {chip(`${t.total_tokens} tok`)}
            </div>
            <div className="muted" style={{ fontSize: 12 }}>
              {t.counts.runs} run(s) · {t.counts.jobs} job(s)
              {t.final_queue ? ` · landed with: ${t.final_queue}` : ""}
            </div>
            {(Object.keys(t.labels_written).length > 0 ||
              Object.keys(t.labels_skipped).length > 0) && (
              <div className="muted" style={{ fontSize: 12 }}>
                labels written: <code>{JSON.stringify(t.labels_written)}</code>
                {Object.keys(t.labels_skipped).length > 0 && (
                  <>
                    {" "}
                    · skipped: <code>{JSON.stringify(t.labels_skipped)}</code>
                  </>
                )}
              </div>
            )}
            {t.errors.length > 0 && (
              <div className="col" style={{ gap: 2 }}>
                {t.errors.map((e, i) => (
                  <div key={i} className="err" style={{ fontSize: 12 }}>
                    ⚠ {e}
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="col" style={{ gap: 0, fontFamily: "var(--mono, monospace)", fontSize: 12.5 }}>
            {t.timeline.map((e, i) => {
              const tone =
                e.error || e.status === "error"
                  ? "err"
                  : e.status === "stub"
                    ? "warn"
                    : e.kind === "run_end"
                      ? "ok"
                      : "muted";
              const hasData = e.data && Object.keys(e.data).length > 0;
              return (
                <div
                  key={i}
                  style={{
                    borderLeft: "2px solid var(--border)",
                    padding: "4px 0 4px 10px",
                    marginLeft: e.kind === "node" ? 16 : 0,
                    cursor: hasData ? "pointer" : "default",
                  }}
                  onClick={() => hasData && toggle(i)}
                >
                  <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
                    <span className="muted" style={{ width: 175, flexShrink: 0 }}>
                      {e.ts ? new Date(e.ts).toLocaleTimeString() : "—"}
                    </span>
                    <span>
                      {MARK[e.kind]} {e.label}
                    </span>
                    {e.status && e.kind !== "node" && chip(String(e.status), tone)}
                    {e.status === "stub" && chip("stub", "warn")}
                  </div>
                  {e.summary && (
                    <div className="muted" style={{ marginLeft: 183 }}>
                      {e.summary}
                    </div>
                  )}
                  {e.error && (
                    <div className="err" style={{ marginLeft: 183 }}>
                      {e.error}
                    </div>
                  )}
                  {hasData && open.has(i) && (
                    <pre
                      style={{
                        marginLeft: 183,
                        marginTop: 4,
                        maxHeight: 320,
                        overflow: "auto",
                        background: "var(--bg2, #1116)",
                        padding: 8,
                        borderRadius: 4,
                      }}
                    >
                      {JSON.stringify(e.data, null, 2)}
                    </pre>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
