import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type {
  ActionRequest,
  GraphAskResult,
  JobFailures,
  KbDocWriteback,
  KilDigest,
  KilMetrics,
  ReviewTask,
  TenantHealth,
} from "../types";

const STATUS = ["open", "correct", "wrong", "dismissed", "all"] as const;

export function ReviewView() {
  const [metrics, setMetrics] = useState<KilMetrics | null>(null);
  const [digest, setDigest] = useState<KilDigest | null>(null);
  const [rows, setRows] = useState<ReviewTask[]>([]);
  const [ars, setArs] = useState<ActionRequest[]>([]);
  const [docWb, setDocWb] = useState<KbDocWriteback[]>([]);
  const [showDocWb, setShowDocWb] = useState(false);
  const [health, setHealth] = useState<TenantHealth | null>(null);
  const [jobFails, setJobFails] = useState<JobFailures | null>(null);
  const [status, setStatus] = useState<(typeof STATUS)[number]>("open");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = () => {
    api.review.list(status).then(setRows).catch((e: ApiError) => setErr(e.message));
    api.review.metrics(30).then(setMetrics).catch(() => {});
    api.approvals
      .list()
      .then((r) => setArs(r.action_requests))
      .catch(() => {});
    api.kb.listAllDocWritebacks("open").then(setDocWb).catch(() => {});
    api.review.tenantHealth().then(setHealth).catch(() => {});
    setJobFails(null);
  };
  useEffect(load, [status]);

  const decide = async (ar: ActionRequest, decision: "approve" | "reject") => {
    setBusy(ar.id);
    setErr(null);
    setNote(null);
    try {
      await api.approvals.decide(ar.id, decision);
      setNote(decision === "approve" ? "Approved — the worker will apply it." : "Rejected.");
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  const resolve = async (t: ReviewTask, s: "correct" | "wrong" | "dismissed") => {
    setBusy(t.id);
    setErr(null);
    setNote(null);
    try {
      const res = await api.review.resolve(t.id, s);
      setNote(
        s === "correct"
          ? "Marked correct — a KB update was drafted and sent for approval."
          : s === "wrong"
            ? "Marked wrong — logged for agent coaching."
            : "Dismissed.",
      );
      void res;
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="pane col" style={{ gap: 16, maxWidth: 900 }}>
      <h2 style={{ margin: 0 }}>Approvals</h2>
      <p style={{ margin: 0, color: "var(--muted, #667)" }}>
        Everything waiting on a human — knowledge-base changes and internal task
        requests to approve, plus sent replies the contradiction judge flagged
        against the KB or case history.
      </p>

      {health && (
        <div className="col" style={{ gap: 4 }}>
          <strong style={{ fontSize: 12, color: "var(--muted, #667)" }}>Bot health (24h)</strong>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            <Tile
              label="sources failing"
              value={health.connections.failing}
              warn={health.connections.failing > 0}
            />
            <Tile
              label="stuck reasoning"
              value={health.reasoning_stuck}
              warn={health.reasoning_stuck > 0}
            />
            <Tile
              label="doc write-backs pending"
              value={health.doc_writebacks_pending}
              warn={health.doc_writebacks_pending > 0}
            />
            <Tile
              label="failed jobs (24h)"
              value={health.failed_jobs_24h}
              warn={health.failed_jobs_24h > 0}
            />
            <Tile
              label="review backlog"
              value={
                health.review_backlog.oldest_days != null
                  ? `${health.review_backlog.open} · ${health.review_backlog.oldest_days}d old`
                  : health.review_backlog.open
              }
              warn={(health.review_backlog.oldest_days ?? 0) > 2}
            />
            <Tile label="runs (24h)" value={health.runs_24h.total} />
            <Tile
              label="struggle rate"
              value={pct(health.runs_24h.struggle_rate)}
              warn={health.runs_24h.struggle_rate > 0.25}
            />
            {health.system_stale.length > 0 && (
              <Tile
                label="ingestion stale"
                value={`${health.system_stale.length} job${health.system_stale.length === 1 ? "" : "s"}`}
                warn
              />
            )}
          </div>
          {health.connections.sample && (
            <span style={{ fontSize: 12, color: "var(--crit, #b4432a)" }}>
              ⚠ {health.connections.sample}
            </span>
          )}
          {health.failed_jobs_24h > 0 && (
            <button
              style={{ alignSelf: "flex-start", fontSize: 12 }}
              onClick={() => {
                if (jobFails) {
                  setJobFails(null);
                } else {
                  api.review.jobFailures().then(setJobFails).catch((e: ApiError) => setErr(e.message));
                }
              }}
            >
              {jobFails ? "hide" : "show"} failed jobs
            </button>
          )}
          {jobFails && (
            <div style={{ overflowX: "auto" }}>
              <table className="runs-table" style={{ minWidth: 560 }}>
                <thead>
                  <tr>
                    <th>kind</th>
                    <th>attempts</th>
                    <th>last error</th>
                    <th>failed</th>
                  </tr>
                </thead>
                <tbody>
                  {jobFails.failures.map((j) => (
                    <tr key={j.job_id}>
                      <td>{j.kind}</td>
                      <td className="muted">
                        {j.attempts}/{j.max_attempts}
                      </td>
                      <td className="muted" style={{ maxWidth: 360, whiteSpace: "pre-wrap" }}>
                        {j.error || "—"}
                      </td>
                      <td className="muted">{j.updated_at.slice(0, 16).replace("T", " ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {metrics && (
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          <Tile label="open" value={metrics.review.open} warn={metrics.review.open > 0} />
          <Tile label="resolved (30d)" value={metrics.review.resolved} />
          <Tile
            label="flag precision"
            value={pct(metrics.review.flag_precision)}
          />
          <Tile label="false-flag rate" value={pct(metrics.review.false_flag_rate)} />
          <Tile
            label="agent correction"
            value={pct(metrics.review.agent_correction_rate)}
          />
          <Tile
            label="median review"
            value={
              metrics.review.median_time_to_review_h != null
                ? `${metrics.review.median_time_to_review_h}h`
                : "—"
            }
          />
          <Tile label="KB changes" value={metrics.kb_writeback.entries} />
          <Tile
            label="provisional"
            value={metrics.kb_writeback.provisional}
            warn={metrics.kb_writeback.provisional > 0}
          />
          <Tile
            label="KB freshness"
            value={
              metrics.knowledge_freshness_days != null
                ? `${metrics.knowledge_freshness_days}d`
                : "—"
            }
          />
          <button
            style={{ alignSelf: "center" }}
            onClick={() =>
              digest
                ? setDigest(null)
                : api.review.digest().then(setDigest).catch((e: ApiError) => setErr(e.message))
            }
          >
            {digest ? "hide weekly report" : "📈 weekly report"}
          </button>
        </div>
      )}

      {digest && (
        <pre
          style={{
            margin: 0,
            padding: 12,
            whiteSpace: "pre-wrap",
            fontSize: 13,
            lineHeight: 1.5,
            background: "var(--card, #f6f7f9)",
            borderRadius: 8,
          }}
        >
          {digest.markdown.replace(/\*/g, "")}
        </pre>
      )}

      <GraphAskPanel />

      {metrics && metrics.by_source.length > 0 && (
        <div className="col" style={{ gap: 4 }}>
          <h4 style={{ margin: "4px 0" }}>Knowledge health by source</h4>
          <div style={{ overflowX: "auto" }}>
            <table className="runs-table" style={{ minWidth: 560 }}>
              <thead>
                <tr>
                  <th>source</th>
                  <th>flagged</th>
                  <th>confirmed gap</th>
                  <th>false-flag rate</th>
                  <th>median time to correct</th>
                </tr>
              </thead>
              <tbody>
                {metrics.by_source.map((s) => (
                  <tr key={s.source}>
                    <td>{s.source}</td>
                    <td className="muted">{s.flagged}</td>
                    <td className="muted">{s.confirmed}</td>
                    <td className={s.false_flag_rate != null && s.false_flag_rate >= 0.5 ? "warn" : "muted"}>
                      {pct(s.false_flag_rate)}
                    </td>
                    <td className="muted">
                      {s.median_time_to_correct_h != null ? `${s.median_time_to_correct_h}h` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {docWb.length > 0 && (
        <div className="col" style={{ gap: 6 }}>
          <button
            style={{ alignSelf: "flex-start" }}
            onClick={() => setShowDocWb((v) => !v)}
            title="Automated Google-Doc edits from approved KB corrections, still awaiting a human on GitHub"
          >
            {showDocWb ? "hide" : "📄 show"} {docWb.length} doc write-back
            {docWb.length === 1 ? "" : "s"} awaiting verification
          </button>
          {showDocWb && <DocWritebacksTable rows={docWb} />}
        </div>
      )}

      <div className="row" style={{ gap: 4 }}>
        {STATUS.map((s) => (
          <button key={s} className={status === s ? "primary" : ""} onClick={() => setStatus(s)}>
            {s}
          </button>
        ))}
      </div>

      {note && <div className="banner">{note}</div>}
      {err && <div className="banner err">{err}</div>}

      {ars.length > 0 && (
        <div className="col" style={{ gap: 10 }}>
          <h3 style={{ margin: "4px 0 0" }}>Awaiting approval</h3>
          {ars.map((ar) => {
            const p = ar.payload as Record<string, string>;
            return (
              <div
                key={ar.id}
                className="col"
                style={{
                  gap: 8,
                  border: "1px solid var(--hair, #ddd)",
                  borderRadius: 10,
                  padding: "12px 14px",
                }}
              >
                <div className="row" style={{ gap: 8, alignItems: "center" }}>
                  <Pill tone="mute">
                    {ar.kind === "kb_change" ? "KB update" : ar.kind}
                  </Pill>
                  <strong>{p.title || ar.rule_name || ar.kind}</strong>
                  <span style={{ color: "var(--muted, #667)", fontSize: 12 }}>
                    {new Date(ar.created_at).toLocaleString()}
                  </span>
                </div>
                {p.body_md && (
                  <pre
                    style={{
                      whiteSpace: "pre-wrap",
                      fontSize: 13,
                      background: "var(--sunk, #f0f2f3)",
                      borderRadius: 8,
                      padding: "8px 10px",
                      margin: 0,
                    }}
                  >
                    {p.body_md}
                  </pre>
                )}
                {p.rationale && (
                  <div style={{ fontSize: 12, color: "var(--muted, #667)" }}>
                    why: {p.rationale}
                  </div>
                )}
                <div className="row" style={{ gap: 6 }}>
                  <button
                    className="primary"
                    disabled={busy === ar.id}
                    onClick={() => decide(ar, "approve")}
                  >
                    Approve &amp; publish
                  </button>
                  <button disabled={busy === ar.id} onClick={() => decide(ar, "reject")}>
                    Reject
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <h3 style={{ margin: "8px 0 0" }}>Flagged replies</h3>
      {rows.length === 0 && <p style={{ color: "var(--muted, #667)" }}>Nothing here.</p>}

      <div className="col" style={{ gap: 12 }}>
        {rows.map((t) => (
          <div
            key={t.id}
            className="col"
            style={{
              gap: 8,
              border: "1px solid var(--hair, #ddd)",
              borderRadius: 10,
              padding: "12px 14px",
            }}
          >
            <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <Pill tone={t.trigger === "sample" ? "mute" : "crit"}>{t.trigger}</Pill>
              <strong>Case {t.case_number || t.case_sf_id || "?"}</strong>
              <span style={{ color: "var(--muted, #667)", fontSize: 12 }}>
                {new Date(t.created_at).toLocaleString()}
              </span>
              {t.status !== "open" && <Pill tone="mute">{t.status}</Pill>}
            </div>

            <div style={{ whiteSpace: "pre-wrap", fontSize: 14 }}>{t.statement}</div>

            {t.verdict?.salient?.length > 0 && (
              <div style={{ fontSize: 13 }}>
                <b>Claim at issue:</b> {t.verdict.salient[0]}
              </div>
            )}
            {t.contexts?.length > 0 && (
              <details>
                <summary style={{ cursor: "pointer", fontSize: 13 }}>
                  Judged against {t.contexts.length} passage(s)
                </summary>
                <ul style={{ fontSize: 12, color: "var(--muted, #667)" }}>
                  {t.contexts.map((c, i) => (
                    <li key={i}>
                      <code>{c.ref || c.kind}</code>: {c.text.slice(0, 240)}
                    </li>
                  ))}
                </ul>
              </details>
            )}

            {t.status === "open" && (
              <div className="row" style={{ gap: 6 }}>
                <button
                  className="primary"
                  disabled={busy === t.id}
                  onClick={() => resolve(t, "correct")}
                >
                  Correct → update KB
                </button>
                <button disabled={busy === t.id} onClick={() => resolve(t, "wrong")}>
                  Wrong → coach
                </button>
                <button disabled={busy === t.id} onClick={() => resolve(t, "dismissed")}>
                  Not a conflict
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function pct(v: number | null): string {
  return v == null ? "—" : `${Math.round(v * 100)}%`;
}

const GRAPH_EXAMPLES = [
  "How many cases were escalated last month, by module?",
  "Which accounts have more than 2 duplicate cases?",
  "Average resolution time by tier for the Billing module",
  "List open cases about refunds",
];

function GraphAskPanel() {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<GraphAskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showCypher, setShowCypher] = useState(false);

  const ask = async (question: string) => {
    const text = question.trim();
    if (!text) return;
    setBusy(true);
    setErr(null);
    setRes(null);
    try {
      setRes(await api.graphAsk(text));
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="col" style={{ gap: 6 }}>
      <h4 style={{ margin: "4px 0" }}>Ask the case graph</h4>
      <div className="muted" style={{ fontSize: 12 }}>
        Plain-English questions over your cases, accounts, modules, tiers and agents.
        The question is compiled to a read-only, workspace-scoped query — shown below the answer.
      </div>
      <form
        className="row"
        style={{ gap: 6 }}
        onSubmit={(e) => {
          e.preventDefault();
          ask(q);
        }}
      >
        <input
          style={{ flex: 1 }}
          placeholder="e.g. how many API-module cases were escalated last week"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button type="submit" disabled={busy || !q.trim()}>
          {busy ? "…" : "Ask"}
        </button>
      </form>
      <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
        {GRAPH_EXAMPLES.map((ex) => (
          <button
            key={ex}
           
            style={{ fontSize: 11 }}
            onClick={() => {
              setQ(ex);
              ask(ex);
            }}
          >
            {ex}
          </button>
        ))}
      </div>

      {err && <div className="banner err">{err}</div>}

      {res && (
        <div className="col" style={{ gap: 4 }}>
          <div style={{ overflowX: "auto" }}>
            <table className="runs-table" style={{ minWidth: 360 }}>
              <thead>
                <tr>
                  {res.columns.map((col) => (
                    <th key={col}>{col}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {res.rows.length === 0 && (
                  <tr>
                    <td colSpan={res.columns.length} className="muted">
                      no matching cases
                    </td>
                  </tr>
                )}
                {res.rows.map((row, i) => (
                  <tr key={i}>
                    {res.columns.map((col) => (
                      <td key={col} className="muted">
                        {row[col] == null ? "—" : String(row[col])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {res.truncated && (
            <div className="muted" style={{ fontSize: 11 }}>
              showing the first {res.spec.limit} rows
            </div>
          )}
          <button
           
            style={{ alignSelf: "flex-start", fontSize: 11 }}
            onClick={() => setShowCypher((v) => !v)}
          >
            {showCypher ? "hide" : "show"} the compiled query
          </button>
          {showCypher && (
            <pre
              style={{
                margin: 0,
                padding: 8,
                whiteSpace: "pre-wrap",
                fontSize: 11,
                background: "var(--card, #f6f7f9)",
                borderRadius: 6,
              }}
            >
              {res.cypher}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function DocWritebacksTable({ rows }: { rows: KbDocWriteback[] }) {
  const color = (s: string) =>
    s === "verified"
      ? "#2b6a2b"
      : s === "suggested" || s === "applied"
        ? "#33608a"
        : s === "partial"
          ? "#8a5a00"
          : s === "reverted"
            ? "#555"
            : "#9b2c2c";
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="runs-table" style={{ minWidth: 640 }}>
        <thead>
          <tr>
            <th>doc</th>
            <th>status</th>
            <th>blocks</th>
            <th>when</th>
            <th>review issue</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((w) => (
            <tr key={w.id}>
              <td style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis" }}>
                {w.doc_url ? (
                  <a href={w.doc_url} target="_blank" rel="noreferrer">
                    {w.connection_label || "Google Doc"}
                  </a>
                ) : (
                  w.connection_label || "Google Doc"
                )}
              </td>
              <td>
                <span
                  title={w.error ?? undefined}
                  style={{
                    fontSize: 11, padding: "1px 6px", borderRadius: 8, color: "#fff",
                    background: color(w.status),
                  }}
                >
                  {w.status}
                </span>
              </td>
              <td style={{ color: "var(--muted, #667)" }}>
                {w.blocks.filter((b) => b.applied).length}/{w.blocks.length}
              </td>
              <td style={{ color: "var(--muted, #667)" }}>
                {new Date(w.applied_at).toLocaleString()}
              </td>
              <td>
                {w.github_issue_url ? (
                  <a href={w.github_issue_url} target="_blank" rel="noreferrer">
                    {w.github_repo}#{w.github_issue_number}
                  </a>
                ) : (
                  <span style={{ color: "var(--muted, #667)" }}>—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Tile({ label, value, warn }: { label: string; value: number | string; warn?: boolean }) {
  return (
    <div className="tile" style={warn ? { borderColor: "var(--crit, #b4432a)" } : undefined}>
      <div className="v">{value}</div>
      <div className="l">{label}</div>
    </div>
  );
}

function Pill({ tone, children }: { tone: "crit" | "mute"; children: React.ReactNode }) {
  const bg = tone === "crit" ? "var(--crit-bg, #f6e4df)" : "var(--surface-2, #eee)";
  const fg = tone === "crit" ? "var(--crit, #b4432a)" : "var(--muted, #667)";
  return (
    <span
      style={{
        background: bg,
        color: fg,
        borderRadius: 6,
        padding: "1px 8px",
        fontSize: 12,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      {children}
    </span>
  );
}
