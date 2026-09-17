// A compact always-visible key for the canvas's only two color signals:
// a node's border (trigger/terminal/invalid|won't-build — see .nodecard--*
// in ui.css) and a conditional edge's label (pass/fail — see EdgeLabel.tsx).
// Exists so someone new to the editor doesn't have to reverse-engineer what
// the colors mean. "invalid" (unregistered type) and "won't build" (>1
// unconditional outgoing edge) share one swatch here — both are the same
// red exception color on the card, since both are build-time errors
// (interpreter/builder.py's FlowBuildError), not two different signals.
const NODE_ITEMS: { swatch: string; label: string }[] = [
  { swatch: "var(--accent)", label: "trigger" },
  { swatch: "var(--warn)", label: "terminal" },
  { swatch: "var(--exception)", label: "invalid / won't build" },
];

const EDGE_ITEMS: { swatch: string; label: string }[] = [
  { swatch: "var(--success)", label: "pass" },
  { swatch: "var(--exception)", label: "fail" },
];

export function CanvasLegend() {
  return (
    <div className="canvas-legend" aria-label="Node and edge color key">
      {NODE_ITEMS.map((it) => (
        <span className="canvas-legend__item" key={it.label}>
          <span className="canvas-legend__dot" style={{ background: it.swatch }} />
          {it.label}
        </span>
      ))}
      <span className="canvas-legend__sep" aria-hidden="true" />
      {EDGE_ITEMS.map((it) => (
        <span className="canvas-legend__item" key={it.label}>
          <span className="canvas-legend__dash" style={{ background: it.swatch }} />
          {it.label}
        </span>
      ))}
    </div>
  );
}
