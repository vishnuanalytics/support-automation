import { useState } from "react";
import { api, ApiError } from "../api";
import type { TraceEvent, TraceResult } from "../types";
import { Toolbar, Button, Tag, TraceStep, Banner, Input, EmptyState } from "../ui";

const MARK: Record<TraceEvent["kind"], string> = {
  job: "▸",
  run_start: "┌",
  run_end: "└",
  node: "•",
  channel: "✉",
};

export function TraceView() {
  const [key, setKey] = useState("");
  const [t, setT] = useState<TraceResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [raw, setRaw] = useState<string | null>(null);
  const [retryMsg, setRetryMsg] = useState<string | null>(null);

  const load = () => {
    if (!key.trim()) return;
    setBusy(true);
    setErr(null);
    setT(null);
    setRaw(null);
    setRetryMsg(null);
    api.trace
      .get(key)
      .then(setT)
      .catch((e: ApiError) => setErr(e.message))
      .finally(() => setBusy(false));
  };

  const asText = async (): Promise<string> => {
    const md = await api.trace.md(key);
    return typeof md === "string" ? md : JSON.stringify(md, null, 2);
  };

  const copyText = async () => {
    try {
      await navigator.clipboard.writeText(await asText());
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setRaw(await asText().catch(() => "could not load report"));
    }
  };

  const showRaw = async () => setRaw(raw == null ? await asText().catch(() => "") : null);

  const retry = async () => {
    if (!key.trim() || busy) return;
    setBusy(true);
    setRetryMsg(null);
    try {
      const r = await api.trace.retry(key);
      setRetryMsg(`re-queued Case ${r.sf_id} — job ${r.job_id?.slice(0, 8) ?? "?"}`);
      setTimeout(load, 1500);
    } catch (e) {
      setRetryMsg((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="trace-shell">
      <div className="app-toolbar">
        <Toolbar title="Trace">
          <Input
            value={key}
            placeholder="00001234 / 500jV… / run id / job id"
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && load()}
            style={{ width: 300 }}
          />
          <Button variant="primary" onClick={load} loading={busy}>
            Trace
          </Button>
          {t && (
            <>
              <Button variant="ghost" onClick={copyText}>
                {copied ? "Copied ✓" : "Copy as text"}
              </Button>
              <Button variant="ghost" onClick={showRaw}>
                {raw == null ? "Raw report" : "Hide raw"}
              </Button>
              <Button variant="ghost" onClick={retry} disabled={busy} title="re-enqueue the flow for this Case">
                Retry
              </Button>
            </>
          )}
        </Toolbar>
      </div>

      <div className="trace-body">
        <div className="muted" style={{ fontSize: 12, marginBottom: 12 }}>
          One timeline per Case — every job, run, node and error, in order. Enter a Salesforce Case
          number, Case id, run id, or job id.
        </div>

        {retryMsg && <Banner tone="accent" title={retryMsg} />}
        {err && <Banner tone="exception" title={err} />}

        {raw != null && (
          <textarea
            readOnly
            value={raw}
            onFocus={(e) => e.currentTarget.select()}
            style={{ width: "100%", minHeight: 320, fontFamily: "var(--font-mono)", fontSize: 12, whiteSpace: "pre" }}
          />
        )}

        {!t && !err && raw == null && (
          <EmptyState title="Trace a Case" body="Enter an identifier above and press Trace." />
        )}

        {t && (
          <>
            <div className="trace-summary">
              <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                <strong>{t.case_number || t.sf_id || t.key}</strong>
                {t.outcome && <Tag tone="accent">outcome: {t.outcome}</Tag>}
                {t.human_action && <Tag tone="neutral">human: {t.human_action}</Tag>}
                {t.flow_version != null && <Tag tone="neutral">flow v{t.flow_version}</Tag>}
                {t.degraded_llm && <Tag tone="warn">LLM STUB (quota)</Tag>}
                {t.failed_jobs.length > 0 && <Tag tone="exception">{t.failed_jobs.length} failed job</Tag>}
                {t.stale_jobs.length > 0 && <Tag tone="warn">{t.stale_jobs.length} stale job</Tag>}
                <Tag tone="neutral">{t.total_ms} ms</Tag>
                <Tag tone="neutral">{t.total_tokens} tok</Tag>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
                {t.counts.runs} run(s) · {t.counts.jobs} job(s)
                {t.final_queue ? ` · landed with: ${t.final_queue}` : ""}
              </div>
              {(Object.keys(t.labels_written).length > 0 || Object.keys(t.labels_skipped).length > 0) && (
                <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
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
                <div style={{ display: "grid", gap: 2, marginTop: 8 }}>
                  {t.errors.map((e, i) => (
                    <div key={i} className="err" style={{ fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                      ⚠ {e}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div style={{ display: "grid", gap: 4, marginTop: 12 }}>
              {t.timeline.map((e, i) => {
                const status =
                  e.error || e.status === "error" ? "failed" : e.status === "stub" ? "warn" : "ok";
                const hasData = e.data && Object.keys(e.data).length > 0;
                return (
                  <div key={i} style={{ marginLeft: e.kind === "node" ? 16 : 0 }}>
                    <TraceStep
                      name={`${MARK[e.kind]} ${e.label}`}
                      duration={e.ts ? new Date(e.ts).toLocaleTimeString() : undefined}
                      summary={e.summary || e.error || (e.status && e.kind !== "node" ? String(e.status) : undefined)}
                      status={status}
                      data={hasData ? e.data : undefined}
                    />
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
