import { useState, type ReactNode } from "react";

/**
 * One step in a run's timeline. Collapsed by default; expands to its data as
 * mono JSON. Shared by TraceView, RunsView and the run panel.
 */
export function TraceStep({
  name,
  duration,
  summary,
  status = "ok",
  data,
  defaultOpen = false,
}: {
  name: string;
  duration?: string;
  summary?: ReactNode;
  status?: "ok" | "warn" | "failed";
  data?: unknown;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const hasData = data !== undefined && data !== null;
  const cls = "ui-trace" + (status === "ok" ? "" : ` ui-trace--${status}`);
  return (
    <div className={cls}>
      <button
        type="button"
        className="ui-trace__head"
        aria-expanded={hasData ? open : undefined}
        onClick={() => hasData && setOpen((o) => !o)}
      >
        <span className="ui-trace__name">{name}</span>
        {(duration || summary) && (
          <span className="ui-trace__meta">
            {[duration, summary].filter(Boolean).map((part, i) => (
              <span key={i}>
                {i > 0 ? " · " : ""}
                {part}
              </span>
            ))}
          </span>
        )}
        {hasData && <span className="ui-trace__caret" aria-hidden>{open ? "▾" : "▸"}</span>}
      </button>
      {hasData && open && (
        <div className="ui-trace__body">
          {typeof data === "string" ? data : JSON.stringify(data, null, 2)}
        </div>
      )}
    </div>
  );
}
