import { useState } from "react";
import { api, ApiError } from "../api";
import type { RunResult } from "../types";

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

export function RunPanel({ flowId }: { flowId: string }) {
  const [text, setText] = useState(SAMPLE);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<RunResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setErr(null);
    setRes(null);
    try {
      const c = JSON.parse(text);
      setRes(await api.runFlow(flowId, c));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : (e as Error).message);
    }
    setBusy(false);
  }

  return (
    <div className="run-panel col">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong>Run a case</strong>
        <button className="primary" onClick={run} disabled={busy}>
          {busy ? "running…" : "Run"}
        </button>
      </div>
      <textarea rows={10} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="muted" style={{ fontSize: 11 }}>
        Uses the published interpreter. LLM + Salesforce run for real only if the
        server has creds; otherwise stub / dry-run.
      </div>

      {err && <div className="banner err">{err}</div>}

      {res && (
        <div className="col">
          {res.trace.map((s, i) => (
            <div className="trace-step" key={i}>
              <span className="ty">{s.type}</span> — {s.summary}
            </div>
          ))}
          <div className="banner ok">
            outcome: <strong>{(res.outcome as { action?: string })?.action ?? "—"}</strong>
            {res.tier ? ` · tier ${res.tier}` : ""}
            {res.confidence != null ? ` · confidence ${res.confidence}` : ""}
          </div>
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
        </div>
      )}
    </div>
  );
}
