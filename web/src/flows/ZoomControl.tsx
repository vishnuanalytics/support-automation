import { useCallback, useEffect } from "react";
import { useReactFlow, useViewport } from "@xyflow/react";

const STEPS = [0.25, 0.5, 0.8, 1, 1.5, 2];

/**
 * Explicit canvas zoom — a percentage readout, Fit and 100%, discrete steps,
 * and `mod +` / `mod -` / `mod 0`. The viewport itself is persisted per flow
 * id by FlowEditor. Must render inside a ReactFlowProvider.
 */
export function ZoomControl() {
  const { zoomTo, fitView, getZoom } = useReactFlow();
  const { zoom } = useViewport();

  const step = useCallback(
    (dir: 1 | -1) => {
      const z = getZoom();
      const next =
        dir === 1
          ? STEPS.find((s) => s > z + 1e-3) ?? STEPS[STEPS.length - 1]
          : [...STEPS].reverse().find((s) => s < z - 1e-3) ?? STEPS[0];
      zoomTo(next, { duration: 120 });
    },
    [getZoom, zoomTo],
  );

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey)) return;
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      if (e.key === "=" || e.key === "+") {
        e.preventDefault();
        step(1);
      } else if (e.key === "-") {
        e.preventDefault();
        step(-1);
      } else if (e.key === "0") {
        e.preventDefault();
        zoomTo(1, { duration: 120 });
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, zoomTo]);

  return (
    <div className="zoomctl" role="group" aria-label="Zoom">
      <button type="button" aria-label="Zoom out" onClick={() => step(-1)}>
        −
      </button>
      <span className="zoomctl__pct" aria-live="polite">
        {Math.round(zoom * 100)}%
      </span>
      <button type="button" aria-label="Zoom in" onClick={() => step(1)}>
        ＋
      </button>
      <button
        type="button"
        onClick={() => fitView({ duration: 200 })}
        title="Fit all nodes in view (reset)"
      >
        Fit
      </button>
      <button
        type="button"
        onClick={() => zoomTo(1, { duration: 120 })}
        title="Reset zoom to 100%"
      >
        100%
      </button>
    </div>
  );
}
