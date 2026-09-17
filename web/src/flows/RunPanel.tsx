import { useState } from "react";
import { api, ApiError } from "../api";
import type { RecentCase, RunResult } from "../types";
import { Button, Banner, TraceStep } from "../ui";

const SAMPLE = JSON.stringify(
  {
    case_id: "DEMO-1",
    subject: "How do I create a webhook trigger in a Zap?",
    body: "I want my Zap to run when my app sends a POST request. How do I get the URL and test it?",
    account: { name: "Acme Co", customer_type: "premium", region: "EMEA" },
    contact: { name: "Dana Lee", email: "dana@acme.example" },
  },
  null,
  2,
);

/** Translates `RunResult.outcome.action` into a plain sentence a
 *  non-technical person can read without knowing what a "confidence gate"
 *  or a "trace" is — this is the primary thing Test Run shows now, with
 *  the raw trace/JSON demoted to an opt-in "technical details" section.
 *  Mirrors every terminal handler's real `outcome.action` value
 *  (interpreter/registry.py: auto_reply/notify/ask_human/handover/
 *  need_info/task_dispatched/task_skipped) — update this alongside a new
 *  terminal node type. */
function describeOutcome(res: RunResult): { tone: "success" | "warn" | "exception"; headline: string; detail?: string } {
  const outcome = (res.outcome ?? {}) as Record<string, unknown>;
  const action = typeof outcome.action === "string" ? outcome.action : null;
  const conf = (v: unknown) => (typeof v === "number" ? ` (confidence ${v.toFixed(2)})` : "");

  switch (action) {
    case "auto_reply":
      return {
        tone: "success",
        headline: `✅ This case would be answered automatically${conf(outcome.confidence)}.`,
        detail: typeof outcome.reply === "string" && outcome.reply ? outcome.reply : undefined,
      };
    case "ask_human":
      return {
        tone: "warn",
        headline: `🙋 This case would be sent to a human for review${conf(outcome.confidence)}.`,
        detail: typeof outcome.draft === "string" && outcome.draft
          ? `Draft it prepared for the human to review:\n${outcome.draft}` : undefined,
      };
    case "handover":
      return {
        tone: "warn",
        headline: `🤝 This case would be fully handed over to a human${conf(outcome.confidence)}.`,
        detail: typeof outcome.reason === "string" ? `Reason: ${outcome.reason}` : undefined,
      };
    case "notify":
      return {
        tone: "success",
        headline: "🔔 This case would ping an internal rep — it stays wherever it already is.",
        detail: typeof outcome.label === "string" && outcome.label ? `Sent to: ${outcome.label}` : undefined,
      };
    case "need_info": {
      const qs = Array.isArray(outcome.questions) ? (outcome.questions as unknown[]) : [];
      return {
        tone: "warn",
        headline: "❓ This case needs more information from the customer before the bot can proceed.",
        detail: qs.length ? `It would ask:\n${qs.map((q) => `• ${String(q)}`).join("\n")}` : undefined,
      };
    }
    case "task_dispatched":
      return { tone: "success", headline: "📋 This case would raise a task for approval (e.g. a GitHub issue)." };
    case "task_skipped":
      return { tone: "success", headline: "⏭️ No policy rule matched — no task was raised." };
    default:
      return {
        tone: "warn",
        headline: "The flow finished, but didn't reach a clear outcome — see the technical details below.",
      };
  }
}

export function RunPanel({ flowId, tenantId }: { flowId: string; tenantId: string }) {
  const [text, setText] = useState(SAMPLE);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<RunResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [showTechnical, setShowTechnical] = useState(false);

  const [recentOpen, setRecentOpen] = useState(false);
  const [recentCases, setRecentCases] = useState<RecentCase[] | null>(null);
  const [recentBusy, setRecentBusy] = useState(false);
  const [recentErr, setRecentErr] = useState<string | null>(null);

  async function run(caseJson?: Record<string, unknown>) {
    setBusy(true);
    setErr(null);
    setRes(null);
    setShowTechnical(false);
    try {
      const c = caseJson ?? JSON.parse(text);
      setRes(await api.runFlow(flowId, c));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : (e as Error).message);
    }
    setBusy(false);
  }

  async function openRecent() {
    setRecentOpen((o) => !o);
    if (recentCases !== null || recentBusy) return;
    setRecentBusy(true);
    setRecentErr(null);
    try {
      const { cases } = await api.caseConnectorRecentCases(tenantId);
      setRecentCases(cases);
    } catch (e) {
      setRecentErr(e instanceof ApiError ? e.message : (e as Error).message);
    }
    setRecentBusy(false);
  }

  async function tryRecentCase(c: RecentCase) {
    setRecentOpen(false);
    setText(JSON.stringify(c, null, 2));
    await run(c);
  }

  const outcome = res ? describeOutcome(res) : null;

  return (
    <div className="run-panel col">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong>Test run</strong>
        <Button variant="secondary" size="sm" onClick={openRecent} loading={recentOpen && recentBusy}>
          Try a real recent case
        </Button>
      </div>

      {recentOpen && (
        <div className="col" style={{ gap: 4, padding: "6px 0" }}>
          {recentBusy && <div className="muted" style={{ fontSize: 12 }}>looking up recent cases…</div>}
          {recentErr && <Banner tone="exception" title={recentErr} />}
          {recentCases?.length === 0 && (
            <div className="muted" style={{ fontSize: 12 }}>
              No connected case system to pull a real case from yet — connect one in the
              Connections tab, or edit the sample case below instead.
            </div>
          )}
          {recentCases?.map((c, i) => (
            <button
              key={i}
              type="button"
              className="palette__item"
              onClick={() => tryRecentCase(c)}
              disabled={busy}
            >
              {c.subject || "(no subject)"}
              <span className="muted" style={{ fontSize: 10.5 }}>
                {c.contact?.name || c.from_name || c.from || c.account?.name || ""}
              </span>
            </button>
          ))}
        </div>
      )}

      {err && <Banner tone="exception" title={err} />}

      {outcome && (
        <Banner
          tone={outcome.tone}
          title={outcome.headline}
          detail={outcome.detail && <div style={{ whiteSpace: "pre-wrap", fontSize: 12.5 }}>{outcome.detail}</div>}
        />
      )}

      {res && (
        <button
          type="button"
          className="link"
          style={{ fontSize: 11, textAlign: "left" }}
          onClick={() => setShowTechnical((s) => !s)}
        >
          {showTechnical ? "« hide" : "show"} technical details (trace, JSON, scores)
        </button>
      )}

      {res && showTechnical && (
        <div className="col">
          {res.trace.map((s, i) => (
            <TraceStep key={i} name={s.type} summary={s.summary} />
          ))}
          <Banner
            tone="success"
            title={
              <>
                outcome: <strong>{(res.outcome as { action?: string })?.action ?? "—"}</strong>
                {res.tier ? ` · tier ${res.tier}` : ""}
                {res.confidence != null ? ` · confidence ${res.confidence}` : ""}
              </>
            }
          />
          {res.confidence_gate && (
            <div className="muted" style={{ fontSize: 12 }}>
              gate: {JSON.stringify(res.confidence_gate)}
            </div>
          )}
          {res.sf_writeback && (
            <div className="muted" style={{ fontSize: 12 }}>
              salesforce: {JSON.stringify(res.sf_writeback)}
            </div>
          )}
          {res.retrieval?.length > 0 && (
            <div className="col">
              <span className="muted">retrieved</span>
              {res.retrieval.map((r, i) => (
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
          <div className="field">
            <label>case JSON (edit and re-run, or paste your own)</label>
            <textarea rows={10} value={text} onChange={(e) => setText(e.target.value)} />
            <Button variant="primary" size="sm" onClick={() => run()} loading={busy} style={{ marginTop: 6 }}>
              Run
            </Button>
          </div>
        </div>
      )}

      {!res && (
        <div className="col">
          <div className="field">
            <label>or edit a sample case and run it yourself</label>
            <textarea rows={10} value={text} onChange={(e) => setText(e.target.value)} />
          </div>
          <div className="muted" style={{ fontSize: 11 }}>
            Uses the published interpreter. LLM + your connected case system run for real
            only if the server has creds; otherwise stub / dry-run.
          </div>
          <Button variant="primary" size="sm" onClick={() => run()} loading={busy}>
            Run
          </Button>
        </div>
      )}
    </div>
  );
}
