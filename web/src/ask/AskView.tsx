import { useState } from "react";
import { api, ApiError } from "../api";
import { Banner } from "../ui";
import type { AskResult, GraphAskResult, RelatedCasesResult } from "../types";

function pct(v: number | null): string {
  return v == null ? "\u2014" : `${Math.round(v * 100)}%`;
}

const GRAPH_EXAMPLES = [
  "How many cases were escalated last month, by module?",
  "Which accounts have more than 2 duplicate cases?",
  "Which modules have cases spanning more than one sub-module?",
  "Cases where the root cause was a Salesforce sync failure",
  "What usually causes the webhook failures?",
];

function GraphTable({ res, tenantId }: { res: GraphAskResult; tenantId: string }) {
  const [showCypher, setShowCypher] = useState(false);
  return (
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
                <td colSpan={res.columns.length || 1} className="muted">
                  no matching cases
                </td>
              </tr>
            )}
            {res.rows.map((row, i) => (
              <tr key={i}>
                {res.columns.map((col) => {
                  const v = row[col];
                  return (
                    <td key={col} className="muted">
                      {col === "case_number" && v != null ? (
                        <RelatedToggle caseNumber={String(v)} tenantId={tenantId} />
                      ) : v == null ? (
                        "\u2014"
                      ) : (
                        String(v)
                      )}
                    </td>
                  );
                })}
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
            background: "var(--ground-sunk)",
            border: "1px solid var(--line)",
            borderRadius: "var(--radius-md)",
          }}
        >
          {res.cypher}
        </pre>
      )}
    </div>
  );
}

function RelatedToggle({ caseNumber, tenantId }: { caseNumber: string; tenantId: string }) {
  const [open, setOpen] = useState(false);
  const [rel, setRel] = useState<RelatedCasesResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !rel && !busy) {
      setBusy(true);
      setErr(null);
      try {
        setRel(await api.graphRelated(caseNumber, tenantId));
      } catch (e) {
        setErr((e as ApiError).message);
      } finally {
        setBusy(false);
      }
    }
  };

  return (
    <span>
      <button
        style={{ fontSize: 11, padding: "1px 6px" }}
        onClick={toggle}
        title="cases with the same root cause / a duplicate link"
      >
        {caseNumber} {open ? "\u25be" : "\u203a"}
      </button>
      {open && (
        <div
          className="col"
          style={{
            gap: 2,
            marginTop: 4,
            paddingLeft: 8,
            borderLeft: "2px solid var(--line)",
          }}
        >
          {busy && <span className="muted" style={{ fontSize: 11 }}>{"\u2026"}</span>}
          {err && <span className="err" style={{ fontSize: 11 }}>{err}</span>}
          {rel && rel.related.length === 0 && (
            <span className="muted" style={{ fontSize: 11 }}>
              nothing linked yet {"\u2014"} run the issue clustering pass
            </span>
          )}
          {rel && rel.issue_title && (
            <span className="muted" style={{ fontSize: 11 }}>
              issue: <strong>{rel.issue_title}</strong>
            </span>
          )}
          {rel?.related.map((r) => (
            <span key={r.case_number} style={{ fontSize: 11 }}>
              <strong>{r.case_number}</strong>{" "}
              <span className="muted">{r.subject || "(no subject)"}</span>{" "}
              {r.same_issue && <span style={{ color: "var(--accent)" }}>same issue</span>}
              {r.duplicate && <span style={{ color: "var(--warn)" }}> duplicate</span>}
            </span>
          ))}
        </div>
      )}
    </span>
  );
}

function GraphAskPanel({ tenantId }: { tenantId: string }) {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<AskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const ask = async (question: string) => {
    const text = question.trim();
    if (!text) return;
    setBusy(true);
    setErr(null);
    setRes(null);
    try {
      setRes(await api.ask(text, tenantId));
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="col" style={{ gap: 6 }}>
      <h4 style={{ margin: "4px 0" }}>Ask</h4>
      <div className="muted" style={{ fontSize: 12 }}>
        Plain-English questions over your cases. Answerable as a graph query {"\u2192"} a
        read-only, workspace-scoped query (shown below the answer). Otherwise it falls
        back to the closest resolved cases.
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
          placeholder="e.g. cases where the root cause was a Salesforce sync failure"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button type="submit" disabled={busy || !q.trim()}>
          {busy ? "\u2026" : "Ask"}
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

      {err && <Banner tone="exception" title={err} />}

      {res && (
        <div className="col" style={{ gap: 6 }}>
          <span
            className="pill"
            style={{ alignSelf: "flex-start", fontSize: 11 }}
            title={
              res.mode === "graph"
                ? "answered from the case graph"
                : "the question isn't a graph query \u2014 closest resolved cases"
            }
          >
            {res.mode === "graph" ? "graph" : "similar cases"}
          </span>

          {res.mode === "graph" && <GraphTable res={res} tenantId={tenantId} />}

          {res.mode === "rag" && (
            <div className="col" style={{ gap: 6 }}>
              {res.graph && (
                <div className="muted" style={{ fontSize: 11 }}>
                  the graph had nothing for this {"\u2014"} showing resolved cases instead
                </div>
              )}
              {res.citable.length === 0 && res.hints.length === 0 && (
                <div className="muted" style={{ fontSize: 12 }}>
                  no similar resolved cases (scanned {res.scanned})
                </div>
              )}
              {res.citable.map((c, i) => (
                <div
                  key={i}
                  className="col"
                  style={{
                    gap: 2,
                    padding: 8,
                    border: "1px solid var(--line)",
                    borderRadius: "var(--radius-md)",
                    background: "var(--surface-raised)",
                  }}
                >
                  <div className="row" style={{ gap: 6, alignItems: "baseline" }}>
                    {c.case_number ? (
                      <RelatedToggle caseNumber={c.case_number} tenantId={tenantId} />
                    ) : (
                      <strong style={{ fontSize: 12 }}>(case)</strong>
                    )}
                    <span className="muted" style={{ fontSize: 11 }}>
                      {c.kind}
                      {c.duplicate ? " \u00b7 duplicate" : ""} {"\u00b7"} {pct(c.relevance)}
                    </span>
                  </div>
                  {c.subject && (
                    <div style={{ fontSize: 12, fontWeight: 600 }}>{c.subject}</div>
                  )}
                  <div className="muted" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
                    {c.resolution_text.slice(0, 400)}
                    {c.resolution_text.length > 400 ? "\u2026" : ""}
                  </div>
                </div>
              ))}
              {res.hints.length > 0 && (
                <ul style={{ margin: 0, paddingLeft: 16 }}>
                  {res.hints.map((h, i) => (
                    <li key={i} className="muted" style={{ fontSize: 11 }}>
                      {h}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function AskView({ tenantId }: { tenantId: string }) {
  return (
    <div className="pane" style={{ padding: "var(--space-4)", maxWidth: 860 }}>
      <GraphAskPanel tenantId={tenantId} />
    </div>
  );
}
