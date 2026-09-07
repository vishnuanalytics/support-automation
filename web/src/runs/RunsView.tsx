import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { RunDetail, RunRow, RunStats, TraceStep as TraceStepData } from "../types";
import {
  Toolbar,
  StatTile,
  Segmented,
  DataTable,
  Tag,
  valueToTone,
  Banner,
  GateStrip,
  TraceStep,
  EmptyState,
  type Column,
} from "../ui";

const OUTCOMES = [
  { value: "", label: "All" },
  { value: "auto_reply", label: "auto_reply" },
  { value: "ask_human", label: "ask_human" },
  { value: "need_info", label: "need_info" },
  { value: "handover", label: "handover" },
];

function tileTone(key: string): "neutral" | "accent" | "warn" | "exception" {
  const t = valueToTone(key);
  return t === "warn-soft" ? "warn" : t;
}

function humanCell(r: RunRow): string {
  if (!r.human_action) return "—";
  if (r.human_action === "pending") return "…";
  return r.human_action + (r.edit_distance != null ? ` (${r.edit_distance.toFixed(2)})` : "");
}

export function RunsView() {
  const [stats, setStats] = useState<RunStats | null>(null);
  const [rows, setRows] = useState<RunRow[]>([]);
  const [filter, setFilter] = useState("");
  const [sel, setSel] = useState<RunDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.runStats().then(setStats).catch(() => {});
  }, []);
  useEffect(() => {
    api
      .listRuns({ outcome: filter || undefined, limit: 100 })
      .then(setRows)
      .catch((e: ApiError) => setErr(e.message));
  }, [filter]);

  const columns: Column<RunRow>[] = [
    {
      key: "when",
      header: "when",
      cell: (r) => (
        <span className="muted" style={{ font: "var(--type-mono)" }}>
          {new Date(r.created_at).toLocaleString()}
        </span>
      ),
    },
    { key: "team", header: "team", cell: (r) => r.team },
    { key: "tier", header: "tier", cell: (r) => <span className="muted">{r.tier ?? "—"}</span> },
    {
      key: "outcome",
      header: "outcome",
      cell: (r) =>
        r.outcome ? <Tag tone={valueToTone(r.outcome)}>{r.outcome}</Tag> : <span className="muted">—</span>,
    },
    {
      key: "score",
      header: "score",
      align: "right",
      cell: (r) => (
        <span
          style={{
            font: "var(--type-mono)",
            color: (r.confidence ?? 1) < 0.4 ? "var(--exception-text)" : "var(--text-secondary)",
          }}
        >
          {r.confidence?.toFixed(3) ?? "—"}
        </span>
      ),
    },
    { key: "human", header: "human", cell: (r) => <span className="muted">{humanCell(r)}</span> },
    {
      key: "subject",
      header: "subject",
      cell: (r) => (
        <span
          className="muted"
          style={{ display: "block", maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
        >
          {r.subject ?? ""}
        </span>
      ),
    },
  ];

  return (
    <div className="runs-shell">
      <div className="app-toolbar">
        <Toolbar title="Runs" meta={`last 100 · ${filter || "all outcomes"}`} />
      </div>
      <div className="runs-view">
        <div className="runs-list">
          <div className="runs-summary">
            {stats && (
              <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
                <StatTile label="runs" value={stats.total} />
                {Object.entries(stats.by_outcome).map(([k, v]) => (
                  <StatTile key={k} label={k} value={v} tone={tileTone(k)} />
                ))}
                <StatTile
                  label="low-confidence"
                  value={stats.low_confidence}
                  tone={stats.low_confidence > 0 ? "warn" : "neutral"}
                />
                {stats.draft_acceptance != null && (
                  <StatTile
                    label="draft kept"
                    value={`${Math.round(stats.draft_acceptance * 100)}%`}
                    tone="accent"
                  />
                )}
                {(stats.by_human_action?.pending ?? 0) > 0 && (
                  <StatTile label="awaiting human" value={stats.by_human_action.pending} tone="warn" />
                )}
              </div>
            )}
          </div>
          <div className="runs-filter">
            <Segmented options={OUTCOMES} value={filter} onChange={setFilter} aria-label="Filter by outcome" />
          </div>
          {err && (
            <div style={{ padding: "0 20px 12px" }}>
              <Banner tone="exception" title={err} />
            </div>
          )}
          <div style={{ flex: 1, minHeight: 0 }}>
            <DataTable
              columns={columns}
              rows={rows}
              rowId={(r) => r.run_id}
              selectedId={sel?.run_id ?? null}
              onSelect={(r) => api.getRun(r.run_id).then(setSel).catch(() => {})}
              empty={
                <EmptyState
                  title="No runs yet"
                  body="Run a flow from the editor's Run panel, or from the CLI."
                />
              }
            />
          </div>
        </div>

        <div className="run-detail">
          {sel ? (
            <Detail run={sel} />
          ) : (
            <EmptyState title="Pick a run" body="Select a row to see why the bot decided what it did." />
          )}
        </div>
      </div>
    </div>
  );
}

function Detail({ run }: { run: RunDetail }) {
  const gate = run.gate as {
    pass?: boolean;
    score?: number;
    threshold?: number;
    tier?: string;
    retrieval_score?: number;
    draft_confidence?: number;
  } | null;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div>
        <div className="row" style={{ gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
          <span style={{ font: "var(--type-section)" }}>{run.subject ?? "(no subject)"}</span>
          {run.outcome && <Tag tone={valueToTone(run.outcome)}>{run.outcome}</Tag>}
        </div>
        <div className="muted" style={{ font: "var(--type-mono)", marginTop: 5 }}>
          {run.source} · {new Date(run.created_at).toLocaleString()} · {run.team} · tier {run.tier ?? "—"}
        </div>
      </div>

      {run.outcome === "need_info" &&
        (() => {
          const c = run.trace.find((s) => s.type === "clarify")?.data as
            | { questions?: string[]; ask_identity?: boolean; auto_sent?: boolean }
            | undefined;
          const qs = c?.questions ?? [];
          return qs.length ? (
            <Banner
              tone="warn"
              title={`Waiting on the customer${c?.ask_identity ? " (+ identity check)" : ""} — ${
                c?.auto_sent ? "questions sent" : "for an agent to send"
              }`}
              detail={
                <ol style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                  {qs.map((q, i) => (
                    <li key={i}>{q}</li>
                  ))}
                </ol>
              }
            />
          ) : null;
        })()}

      {gate && gate.score != null && (
        <div>
          <h5>The gate</h5>
          <GateStrip
            retrieval={gate.retrieval_score ?? 0}
            draft={gate.draft_confidence ?? 0}
            score={gate.score ?? 0}
            threshold={gate.threshold ?? 0}
            passed={!!gate.pass}
          />
          {gate.tier && (
            <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
              tier {gate.tier}
            </div>
          )}
        </div>
      )}

      <div>
        <h5>Trace</h5>
        <div style={{ display: "grid", gap: 4 }}>
          {run.trace.map((s: TraceStepData, i) => (
            <TraceStep
              key={i}
              name={s.type}
              summary={s.summary}
              data={s.data}
              status={
                typeof (s.data as { error?: unknown }).error === "string"
                  ? "failed"
                  : s.type.includes("gate") && (s.data as { pass?: boolean }).pass === false
                    ? "warn"
                    : "ok"
              }
            />
          ))}
        </div>
      </div>

      {run.retrieval?.length > 0 && (
        <div>
          <h5>Retrieved</h5>
          <div style={{ display: "grid", gap: 4, fontSize: 12 }}>
            {run.retrieval.map((r, i) => (
              <div key={i}>
                <a href={r.doc_url} target="_blank" rel="noreferrer">
                  {r.doc_url.replace("https://docs.zapier.com", "")}
                </a>{" "}
                <span className="muted">
                  {r.heading_path ?? ""} {r.rerank_score != null ? `(${r.rerank_score.toFixed(2)})` : ""}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {run.sf_writeback && (
        <div className="muted" style={{ fontSize: 12 }}>
          salesforce: <code>{JSON.stringify(run.sf_writeback)}</code>
        </div>
      )}

      {run.human_action && run.human_action !== "pending" && (
        <div>
          <h5>Human resolution</h5>
          <Banner
            tone="accent"
            title={`${run.human_action}${
              run.edit_distance != null ? ` · edit distance ${run.edit_distance.toFixed(2)}` : ""
            }`}
          />
          {run.draft && (
            <div style={{ marginTop: 8 }}>
              <TraceStep
                name="bot draft vs. what the human sent"
                data={{ draft: run.draft, sent: run.human_reply ?? "(none)" }}
              />
            </div>
          )}
        </div>
      )}
      {run.human_action === "pending" && (
        <div className="muted" style={{ fontSize: 12 }}>
          Awaiting human resolution…
        </div>
      )}
    </div>
  );
}
