import type { SfMeta } from "../types";
import {
  ANSWER_MODE_VALUES,
  EDGE_CHANNELS,
  FIELD_OPTIONS,
  OP_LABELS,
  OP_ORDER,
  OP_TITLES,
  ROUTED_TEAMS,
  URGENCY_VALUES,
  type Clause,
  type Op,
} from "./conditionBuilder";
import { useState } from "react";

/** Fields whose values come from a real, known list — a dropdown (or a
 *  multi-select for "is one of") instead of a free-text box. `null` means
 *  "no known set", so the caller falls back to plain text/number entry. */
function knownOptionsFor(field: string, sfMeta: SfMeta): { value: string; label: string }[] | null {
  switch (field) {
    case "case.channel":
      return EDGE_CHANNELS;
    case "routed_team":
      return ROUTED_TEAMS.map((t) => ({ value: t, label: t }));
    case "classification.case_type":
      return sfMeta.case_types.length ? sfMeta.case_types.map((t) => ({ value: t, label: t })) : null;
    case "classification.urgency":
      return URGENCY_VALUES.map((t) => ({ value: t, label: t }));
    case "classification.answer_mode":
      return ANSWER_MODE_VALUES.map((t) => ({ value: t, label: t }));
    case "confidence_gate.pass":
      return [{ value: "true", label: "true" }, { value: "false", label: "false" }];
    default:
      return null;
  }
}

const NUMERIC_FIELDS = new Set(["confidence", "retrieval_score", "draft_confidence"]);

function FieldPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const known = FIELD_OPTIONS.some((f) => f.value === value);
  const [customMode, setCustomMode] = useState(!known && value !== "");
  if (customMode) {
    return (
      <span className="row" style={{ gap: 2, flex: 1, minWidth: 0 }}>
        <input
          style={{ flex: 1, minWidth: 0 }}
          value={value}
          placeholder="e.g. policy.task"
          onChange={(e) => onChange(e.target.value)}
        />
        <button
          type="button"
          style={{ width: "auto", flex: "none" }}
          title="pick from the list instead"
          onClick={() => { setCustomMode(false); onChange(""); }}
        >
          ▾
        </button>
      </span>
    );
  }
  return (
    <select
      style={{ flex: 1, minWidth: 0 }}
      value={known ? value : ""}
      onChange={(e) => (e.target.value === "__custom__" ? setCustomMode(true) : onChange(e.target.value))}
    >
      <option value="" disabled>field…</option>
      {FIELD_OPTIONS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
      <option value="__custom__">Other (type it)…</option>
    </select>
  );
}

function ValuePicker({
  field,
  op,
  value,
  onChange,
  sfMeta,
}: {
  field: string;
  op: Op;
  value: string;
  onChange: (v: string) => void;
  sfMeta: SfMeta;
}) {
  if (op === "is_set" || op === "is_not_set") return null;
  const options = knownOptionsFor(field, sfMeta);

  if (op === "in" || op === "not_in") {
    if (options) {
      const selected = value ? value.split(",").map((s) => s.trim()).filter(Boolean) : [];
      return (
        <select
          multiple
          size={Math.min(4, options.length)}
          style={{ flex: 1, minWidth: 0 }}
          value={selected}
          onChange={(e) => onChange(Array.from(e.target.selectedOptions).map((o) => o.value).join(", "))}
        >
          {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      );
    }
    return (
      <input
        style={{ flex: 1, minWidth: 0 }}
        value={value}
        placeholder="comma-separated values"
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  if (options) {
    return (
      <select style={{ flex: 1, minWidth: 0 }} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="" disabled>value…</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    );
  }
  const numeric = NUMERIC_FIELDS.has(field);
  return (
    <input
      type={numeric ? "number" : "text"}
      step={numeric ? "0.01" : undefined}
      style={{ flex: 1, minWidth: 0 }}
      value={value}
      placeholder="value"
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

/**
 * A row per AND-ed condition — field / operator / value, instead of a
 * hand-typed expression. Only renders when `parseCondition` (conditionBuilder.ts)
 * could represent the whole `if` string this way; `or`/`not`/anything nested
 * falls back to the raw expression box in EdgeInspector, not here.
 */
export function ConditionBuilder({
  clauses,
  onChange,
  sfMeta,
}: {
  clauses: Clause[];
  onChange: (clauses: Clause[]) => void;
  sfMeta: SfMeta;
}) {
  const setClause = (i: number, patch: Partial<Clause>) => {
    const next = clauses.slice();
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  const removeClause = (i: number) => onChange(clauses.filter((_, idx) => idx !== i));
  const addClause = () => onChange([...clauses, { field: "routed_team", op: "eq", value: "" }]);

  return (
    <div className="condbuilder">
      {clauses.length > 1 && (
        <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>if ALL of these are true:</div>
      )}
      {clauses.map((c, i) => {
        const hasValue = c.op !== "is_set" && c.op !== "is_not_set";
        return (
          <div className="condbuilder__row" key={i}>
            <div className="row" style={{ gap: 6 }}>
              <FieldPicker value={c.field} onChange={(v) => setClause(i, { field: v, value: "" })} />
              <select
                style={{ flex: hasValue ? 1 : "none", width: hasValue ? undefined : "auto", minWidth: 0 }}
                title={OP_TITLES[c.op]}
                value={c.op}
                onChange={(e) => setClause(i, { op: e.target.value as Op, value: "" })}
              >
                {OP_ORDER.map((op) => <option key={op} value={op} title={OP_TITLES[op]}>{OP_LABELS[op]}</option>)}
              </select>
              {!hasValue && (
                <button
                  type="button"
                  className="condbuilder__remove"
                  title="remove this condition"
                  onClick={() => removeClause(i)}
                >
                  ✕
                </button>
              )}
            </div>
            {hasValue && (
              <div className="row" style={{ gap: 6, marginTop: 6 }}>
                <ValuePicker field={c.field} op={c.op} value={c.value} onChange={(v) => setClause(i, { value: v })} sfMeta={sfMeta} />
                <button
                  type="button"
                  className="condbuilder__remove"
                  title="remove this condition"
                  onClick={() => removeClause(i)}
                >
                  ✕
                </button>
              </div>
            )}
          </div>
        );
      })}
      <button type="button" onClick={addClause}>+ add condition</button>
    </div>
  );
}
