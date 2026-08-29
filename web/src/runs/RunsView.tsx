import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { RunDetail, RunRow, RunStats, TraceStep } from "../types";

const OUTCOMES = ["", "auto_reply", "ask_human", "handover"] as const;

export function RunsView() {
  const [stats, setStats] = useState<RunStats | null>(null);
  const [rows, setRows] = useState<RunRow[]>([]);
  const [filter, setFilter] = useState<(typeof OUTCOMES)[number]>("");
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

  return (
    <div className="runs-view">
      <div className="runs-list col">
        {stats && (
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            <Tile label="runs" value={stats.total} />
            {Object.entries(stats.by_outcome).map(([k, v]) => (
              <Tile key={k} label={k} value={v} />
            ))}
            <Tile label="low-confidence" value={stats.low_confidence} warn />
            {stats.draft_acceptance != null && (
              <div className="tile">
                <div className="v">{Math.round(stats.draft_acceptance * 100)}%</div>
                <div className="l">draft kept</div>
              </div>
            )}
            {(stats.by_human_action?.pending ?? 0) > 0 && (
              <Tile label="awaiting human" value={stats.by_human_action.pending} />
            )}
          </div>
        )}

        <div className="row" style={{ gap: 4 }}>
          {OUTCOMES.map((o) => (
            <button
              key={o || "all"}
              className={filter === o ? "primary" : ""}
              onClick={() => setFilter(o)}
            >
              {o || "all"}
            </button>
          ))}
        </div>

        {err && <div className="banner err">{err}</div>}

        <table className="runs-table">
          <thead>
            <tr>
              <th>when</th>
              <th>team</th>
              <th>tier</th>
              <th>outcome</th>
              <th>conf.</th>
              <th>human</th>
              <th>subject</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.run_id}
                className={sel?.run_id === r.run_id ? "active" : ""}
                onClick={() => api.getRun(r.run_id).then(setSel)}
              >
                <td className="muted">{new Date(r.created_at).toLocaleString()}</td>
                <td>{r.team}</td>
                <td>{r.tier ?? "—"}</td>
                <td>
                  <span className={`pill ${r.outcome ?? ""}`}>{r.outcome ?? "—"}</span>
                </td>
                <td className={(r.confidence ?? 1) < 0.4 ? "err" : ""}>
                  {r.confidence?.toFixed(3) ?? "—"}
                </td>
                <td className="muted">
                  {r.human_action
                    ? r.human_action === "pending"
                      ? "…"
                      : `${r.human_action}${r.edit_distance != null ? ` (${r.edit_distance.toFixed(2)})` : ""}`
                    : "—"}
                </td>
                <td className="muted" style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {r.subject ?? ""}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  no runs — run a flow from the editor or `python -m interpreter.run`
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="run-detail">
        {sel ? <Detail run={sel} /> : <div className="muted">select a run to see why the bot decided</div>}
      </div>
    </div>
  );
}

function Tile({ label, value, warn }: { label: string; value: number; warn?: boolean }) {
  return (
    <div className={`tile${warn && value > 0 ? " warn" : ""}`}>
      <div className="v">{value}</div>
      <div className="l">{label}</div>
    </div>
  );
}

function Detail({ run }: { run: RunDetail }) {
  const gate = run.gate as
    | { pass?: boolean; score?: number; threshold?: number; tier?: string; retrieval_score?: number; draft_confidence?: number }
    | null;
  return (
    <div className="col">
      <h4>
        <span className="muted">{run.team}</span> · {run.subject ?? "(no subject)"}
      </h4>
      <div className="muted" style={{ fontSize: 12 }}>
        {run.source} · {new Date(run.created_at).toLocaleString()} · tier {run.tier ?? "—"} ·{" "}
        outcome <strong>{run.outcome ?? "—"}</strong>
      </div>

      <h5>why</h5>
      {run.trace.map((s: TraceStep, i) => (
        <details key={i} className="trace-step">
          <summary>
            <span className="ty">{s.type}</span> — {s.summary}
          </summary>
          <pre style={{ fontSize: 11, overflow: "auto" }}>{JSON.stringify(s.data, null, 2)}</pre>
        </details>
      ))}

      {gate && (
        <div className="banner ok">
          gate: {gate.retrieval_score?.toFixed(3)} retrieval · {gate.draft_confidence?.toFixed(2)} draft →
          score <strong>{gate.score?.toFixed(3)}</strong> vs threshold {gate.threshold} ({gate.tier}) →{" "}
          {gate.pass ? "PASS" : "FAIL"}
        </div>
      )}

      {run.retrieval?.length > 0 && (
        <div className="col">
          <h5>retrieved</h5>
          {run.retrieval.map((r, i) => (
            <div key={i} style={{ fontSize: 12 }}>
              <a href={r.doc_url} target="_blank" rel="noreferrer">
                {r.doc_url.replace("https://docs.zapier.com", "")}
              </a>{" "}
              <span className="muted">
                {r.heading_path ?? ""} {r.rerank_score != null ? `(${r.rerank_score.toFixed(2)})` : ""}
              </span>
            </div>
          ))}
        </div>
      )}

      {run.sf_writeback && (
        <div className="muted" style={{ fontSize: 12 }}>
          salesforce: {JSON.stringify(run.sf_writeback)}
        </div>
      )}

      {run.human_action && run.human_action !== "pending" && (
        <>
          <h5>human resolution</h5>
          <div className="banner ok">
            <strong>{run.human_action}</strong>
            {run.edit_distance != null ? ` · edit distance ${run.edit_distance.toFixed(2)}` : ""}
          </div>
          {run.draft && (
            <details className="trace-step">
              <summary>bot draft vs. what the human sent</summary>
              <div style={{ fontSize: 12 }}>
                <div className="muted">draft</div>
                <pre style={{ whiteSpace: "pre-wrap" }}>{run.draft}</pre>
                <div className="muted">sent</div>
                <pre style={{ whiteSpace: "pre-wrap" }}>{run.human_reply ?? "(none)"}</pre>
              </div>
            </details>
          )}
        </>
      )}
      {run.human_action === "pending" && (
        <div className="muted" style={{ fontSize: 12 }}>awaiting human resolution…</div>
      )}
    </div>
  );
}
