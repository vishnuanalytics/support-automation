import { useEffect, useState } from "react";
import { api } from "../api";
import type { KbCollection } from "../types";
import type { RFEdge, RFNode } from "./graph";

export function NodeInspector({
  node,
  config,
  onLabel,
  onConfig,
  onDelete,
}: {
  node: RFNode;
  config: Record<string, unknown>;
  onLabel: (v: string) => void;
  onConfig: (v: Record<string, unknown>) => void;
  onDelete: () => void;
}) {
  return (
    <div>
      <h4>
        <span className="muted">{node.data.nodeType}</span> node
      </h4>
      <div className="field">
        <label>label</label>
        <input value={node.data.label} onChange={(e) => onLabel(e.target.value)} />
      </div>

      {node.data.nodeType === "confidence_gate" && (
        <GateForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "kb_lookup" && (
        <KbLookupForm config={config} onConfig={onConfig} />
      )}

      {node.data.nodeType === "extract" && (
        <ExtractForm config={config} onConfig={onConfig} />
      )}

      {(node.data.nodeType === "policy_gate" || node.data.nodeType === "task_dispatch") && (
        <div className="muted" style={{ fontSize: 11, borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
          {node.data.nodeType === "policy_gate"
            ? "evaluates this team's rules (Rules tab) against the run; route on policy.action == 'ask_human' etc."
            : "raises the matched rule's task for Slack approval; wire it after policy_gate on policy.task != None"}
        </div>
      )}

      <JsonField label="config (jsonb)" value={config} onChange={onConfig} />

      <button className="err" onClick={onDelete}>
        delete node
      </button>
    </div>
  );
}

function GateForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const to = (config.tier_overrides as Record<string, number>) || {};
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });
  const setTier = (tier: string, v: number) =>
    set({ tier_overrides: { ...to, [tier]: v } });
  const num = (v: unknown, d = 0) => (typeof v === "number" ? v : d);

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <label>thresholds</label>
      <div className="row">
        <span className="muted" style={{ width: 90 }}>default</span>
        <input
          type="number" step="0.05" min="0" max="1"
          value={num(config.default_threshold, 0.35)}
          onChange={(e) => set({ default_threshold: parseFloat(e.target.value) })}
        />
      </div>
      {["basic", "premium", "enterprise"].map((t) => (
        <div className="row" key={t}>
          <span className="muted" style={{ width: 90 }}>{t}</span>
          <input
            type="number" step="0.05" min="0" max="1"
            value={num(to[t], 0.35)}
            onChange={(e) => setTier(t, parseFloat(e.target.value))}
          />
        </div>
      ))}
      <div className="row">
        <span className="muted" style={{ width: 90 }}>retr. weight</span>
        <input
          type="number" step="0.1" min="0" max="1"
          value={num(config.retrieval_weight, 0.5)}
          onChange={(e) => set({ retrieval_weight: parseFloat(e.target.value) })}
        />
      </div>
    </div>
  );
}

function ExtractForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const fields = (config.fields as Record<string, string>) || {};
  const rows = Object.entries(fields);
  const setFields = (f: Record<string, string>) => onConfig({ ...config, fields: f });

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <label>fields to extract into state.entities</label>
      {rows.map(([k, v], i) => (
        <div className="row" key={i} style={{ gap: 4 }}>
          <input
            value={k}
            placeholder="report_period_years"
            style={{ maxWidth: 150 }}
            onChange={(e) => {
              const next: Record<string, string> = {};
              rows.forEach(([kk, vv], j) => (next[j === i ? e.target.value : kk] = vv));
              setFields(next);
            }}
          />
          <input
            value={v}
            placeholder="how old are the requested reports, in years"
            onChange={(e) => setFields({ ...fields, [k]: e.target.value })}
          />
          <button
            className="err"
            onClick={() => {
              const next = { ...fields };
              delete next[k];
              setFields(next);
            }}
          >
            ×
          </button>
        </div>
      ))}
      <button onClick={() => setFields({ ...fields, "": "" })}>＋ field</button>
    </div>
  );
}

function KbLookupForm({
  config,
  onConfig,
}: {
  config: Record<string, unknown>;
  onConfig: (v: Record<string, unknown>) => void;
}) {
  const [cols, setCols] = useState<KbCollection[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const selected = (config.collections as string[]) || [];
  const set = (patch: Record<string, unknown>) => onConfig({ ...config, ...patch });

  useEffect(() => {
    api.kb
      .listCollections()
      .then(setCols)
      .catch((e) => setErr(String(e)));
  }, []);

  const toggle = (name: string) =>
    set({
      collections: selected.includes(name)
        ? selected.filter((n) => n !== name)
        : [...selected, name],
    });

  return (
    <div className="field" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <label>collections to consult</label>
      {err && <div className="err" style={{ fontSize: 11 }}>{err}</div>}
      {cols.length === 0 && !err && (
        <div className="muted" style={{ fontSize: 11 }}>
          no collections yet — add some in the Knowledge tab
        </div>
      )}
      {cols.map((c) => (
        <label key={c.source_id} className="row" style={{ gap: 6 }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={selected.includes(c.name)}
            onChange={() => toggle(c.name)}
          />
          {c.name} <span className="muted">({c.entry_count})</span>
        </label>
      ))}
      <div className="row" style={{ marginTop: 6 }}>
        <span className="muted" style={{ width: 90 }}>top_k</span>
        <input
          type="number" min="1" max="10"
          value={typeof config.top_k === "number" ? config.top_k : 4}
          onChange={(e) => set({ top_k: parseInt(e.target.value, 10) })}
        />
      </div>
      <div className="field">
        <label>query (optional — {"{{case.subject}}"} etc.; default = case text)</label>
        <input
          value={typeof config.query === "string" ? config.query : ""}
          placeholder="{{case.subject}} {{case.body}}"
          onChange={(e) => set({ query: e.target.value || undefined })}
        />
      </div>
    </div>
  );
}

export function EdgeInspector({
  edge,
  onCondition,
  onDelete,
}: {
  edge: RFEdge;
  onCondition: (c: Record<string, unknown>) => void;
  onDelete: () => void;
}) {
  const ifExpr = (edge.data?.condition as { if?: string })?.if ?? "";
  const conditional = ifExpr !== "";
  return (
    <div>
      <h4>edge</h4>
      <div className="field">
        <label className="row" style={{ gap: 6 }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={conditional}
            onChange={(e) =>
              onCondition(e.target.checked ? { if: "tier == 'enterprise'" } : {})
            }
          />
          conditional
        </label>
      </div>
      {conditional && (
        <div className="field">
          <label>if (expression)</label>
          <textarea
            rows={2}
            value={ifExpr}
            onChange={(e) => onCondition({ if: e.target.value })}
          />
          <div className="muted" style={{ fontSize: 11 }}>
            names: tier, region, confidence, retrieval_score, draft_confidence,
            confidence_gate.pass, classification.urgency
          </div>
        </div>
      )}
      <button className="err" onClick={onDelete}>
        delete edge
      </button>
    </div>
  );
}

function JsonField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(value, null, 2));
  const [err, setErr] = useState<string | null>(null);

  // reflect external changes (e.g. the gate form) unless the user is mid-edit-error
  useEffect(() => {
    if (!err) setText(JSON.stringify(value, null, 2));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(value)]);

  return (
    <div className="field">
      <label>{label}</label>
      <textarea
        rows={10}
        value={text}
        style={err ? { borderColor: "var(--err)" } : undefined}
        onChange={(e) => {
          setText(e.target.value);
          try {
            onChange(JSON.parse(e.target.value));
            setErr(null);
          } catch (x) {
            setErr((x as Error).message);
          }
        }}
      />
      {err && <div className="err" style={{ fontSize: 11 }}>{err}</div>}
    </div>
  );
}
