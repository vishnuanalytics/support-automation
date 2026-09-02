import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { ActionRequest, KilDigest, KilMetrics, ReviewTask } from "../types";

const STATUS = ["open", "correct", "wrong", "dismissed", "all"] as const;

export function ReviewView() {
  const [metrics, setMetrics] = useState<KilMetrics | null>(null);
  const [digest, setDigest] = useState<KilDigest | null>(null);
  const [rows, setRows] = useState<ReviewTask[]>([]);
  const [ars, setArs] = useState<ActionRequest[]>([]);
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
