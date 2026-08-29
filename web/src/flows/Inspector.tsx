import { useEffect, useState } from "react";
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
