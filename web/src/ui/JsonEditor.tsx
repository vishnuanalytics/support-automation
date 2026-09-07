import { useEffect, useState } from "react";
import { Field } from "./Field";
import { Input } from "./Input";
import { Toggle } from "./Toggle";

type Json = Record<string, unknown>;

/**
 * A generated key/value form is the default view; JSON is one tab away and
 * validates on blur, showing the key count when valid. Never the only way to
 * edit — the Form tab always works for flat objects; nested values fall back
 * to the JSON tab. A per-node-kind schema-driven form replaces the Form tab
 * in the editor (chunk 3); this is the generic default.
 */
export function JsonEditor({
  value,
  onChange,
  schema = null,
}: {
  value: Json;
  onChange: (value: Json) => void;
  schema?: Record<string, unknown> | null;
}) {
  const [tab, setTab] = useState<"form" | "json">("form");
  const [draft, setDraft] = useState(() => JSON.stringify(value, null, 2));
  const [error, setError] = useState<string | null>(null);

  // keep the JSON draft in sync when the value changes from outside
  useEffect(() => {
    setDraft(JSON.stringify(value, null, 2));
    setError(null);
  }, [value]);

  function commitJson() {
    try {
      const parsed = JSON.parse(draft);
      if (parsed == null || typeof parsed !== "object" || Array.isArray(parsed)) {
        setError("Top level must be a JSON object.");
        return;
      }
      setError(null);
      onChange(parsed as Json);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invalid JSON");
    }
  }

  function setKey(key: string, next: unknown) {
    onChange({ ...value, [key]: next });
  }

  const keys = schema && typeof schema.properties === "object" && schema.properties
    ? Object.keys(schema.properties as object)
    : Object.keys(value);
  const keyCount = Object.keys(value).length;

  return (
    <div className="ui-json">
      <div className="ui-json__tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "form"}
          className={"ui-json__tab" + (tab === "form" ? " ui-json__tab--on" : "")}
          onClick={() => setTab("form")}
        >
          Form
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "json"}
          className={"ui-json__tab" + (tab === "json" ? " ui-json__tab--on" : "")}
          onClick={() => setTab("json")}
        >
          JSON
        </button>
      </div>

      {tab === "form" ? (
        <div style={{ display: "grid", gap: 14 }}>
          {keys.length === 0 && <div className="ui-field__hint">No keys yet — add them in the JSON tab.</div>}
          {keys.map((k) => {
            const v = value[k];
            if (typeof v === "boolean") {
              return (
                <Field key={k} label={k}>
                  <Toggle checked={v} onChange={(next) => setKey(k, next)}>
                    <span className="muted">{v ? "on" : "off"}</span>
                  </Toggle>
                </Field>
              );
            }
            if (typeof v === "number") {
              return (
                <Field key={k} label={k}>
                  <Input
                    type="number"
                    value={String(v)}
                    onChange={(e) => setKey(k, e.target.value === "" ? "" : Number(e.target.value))}
                  />
                </Field>
              );
            }
            if (typeof v === "string" || v == null) {
              return (
                <Field key={k} label={k}>
                  <Input value={v ?? ""} onChange={(e) => setKey(k, e.target.value)} />
                </Field>
              );
            }
            // nested object / array — read-only pointer to the JSON tab
            return (
              <Field key={k} label={k} hint="Edit this nested value in the JSON tab.">
                <Input value={JSON.stringify(v)} readOnly />
              </Field>
            );
          })}
        </div>
      ) : (
        <>
          <textarea
            className={"ui-json__code" + (error ? " ui-json__code--invalid" : "")}
            value={draft}
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commitJson}
          />
          <div className={"ui-json__status " + (error ? "ui-json__status--err" : "ui-json__status--ok")}>
            {error ? `✕ ${error}` : `✓ valid JSON · ${keyCount} key${keyCount === 1 ? "" : "s"}`}
          </div>
        </>
      )}
    </div>
  );
}
