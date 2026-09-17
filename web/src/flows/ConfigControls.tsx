import type { CSSProperties, ReactNode } from "react";

/**
 * The two layout primitives every per-node-type config form in
 * Inspector.tsx used to hand-roll from scratch: a bordered, labeled group
 * of controls (`ConfigField`) and a single label + control row inside one
 * (`ConfigRow`). Before this, ~15 form functions each repeated the same
 * `<div className="field" style={{ borderBottom: "1px solid var(--border)",
 * paddingBottom: 8 }}>` wrapper and the same `<div className="row"><span
 * className="muted" style={{ width: N }}>` row byte-for-byte, with N picked
 * by feel per form (90/110/130 — no shared convention, easy to drift
 * further as new node types get their own forms). Not a visual redesign —
 * every existing form's chosen width is preserved as a `width` prop, so
 * this is pure de-duplication, not a re-skin.
 */

export function ConfigField({
  label,
  hint,
  bordered = true,
  children,
  style,
}: {
  label?: ReactNode;
  /** small muted note below the content, e.g. "edited in the raw config below" */
  hint?: ReactNode;
  /** the border/padding that marks "this is one section of the form" — off for a lone field nested inside a section that already has one */
  bordered?: boolean;
  children?: ReactNode;
  style?: CSSProperties;
}) {
  return (
    <div className={`field${bordered ? " field--section" : ""}`} style={style}>
      {label && <label>{label}</label>}
      {children}
      {hint && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

const WIDTH_CLASS: Record<number, string> = { 90: "field-label", 110: "field-label--md", 130: "field-label--lg" };

export function ConfigRow({
  label,
  width = 90,
  children,
  style,
}: {
  label: ReactNode;
  /** 90 (default) / 110 / 130 reuse a shared class; any other value falls back to an inline width */
  width?: number;
  children?: ReactNode;
  style?: CSSProperties;
}) {
  const cls = WIDTH_CLASS[width];
  return (
    <div className="row" style={style}>
      <span className={cls ? `muted ${cls}` : "muted"} style={cls ? undefined : { width, flex: "none" }}>
        {label}
      </span>
      {children}
    </div>
  );
}
