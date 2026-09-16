import { useRef, useState } from "react";
import { Calendar } from "lucide-react";
import { Button } from "./Button";
import { Popover } from "./Popover";

// `label` is set when the range came from a preset, so the button can show
// "Last 1 hour" verbatim instead of re-deriving it later by comparing
// stored timestamps against a freshly-computed `presetRange()` — that
// comparison drifts out of exact equality the moment any time passes
// between picking the preset and rendering the button, since presetRange()
// always measures from "now". Cleared (undefined) for a custom range, so
// rangeLabel() falls back to formatting the actual since/until instead.
export type DateRange = { since: string | null; until: string | null; label?: string };

type PresetKey =
  | "1h" | "4h" | "12h" | "today" | "yesterday" | "7d" | "30d" | "month" | "quarter" | "all";

const PRESETS: { key: PresetKey; label: string }[] = [
  { key: "1h", label: "Last 1 hour" },
  { key: "4h", label: "Last 4 hours" },
  { key: "12h", label: "Last 12 hours" },
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "7d", label: "Last 7 days" },
  { key: "30d", label: "Last 30 days" },
  { key: "month", label: "This month" },
  { key: "quarter", label: "This quarter" },
];

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function presetRange(key: PresetKey): DateRange {
  const now = new Date();
  const label = PRESETS.find((p) => p.key === key)?.label ?? "All time";
  switch (key) {
    case "1h":
      return { since: new Date(now.getTime() - 3600e3).toISOString(), until: null, label };
    case "4h":
      return { since: new Date(now.getTime() - 4 * 3600e3).toISOString(), until: null, label };
    case "12h":
      return { since: new Date(now.getTime() - 12 * 3600e3).toISOString(), until: null, label };
    case "today":
      return { since: startOfDay(now).toISOString(), until: null, label };
    case "yesterday": {
      const end = startOfDay(now);
      const start = new Date(end.getTime() - 24 * 3600e3);
      return { since: start.toISOString(), until: end.toISOString(), label };
    }
    case "7d":
      return { since: new Date(now.getTime() - 7 * 86400e3).toISOString(), until: null, label };
    case "30d":
      return { since: new Date(now.getTime() - 30 * 86400e3).toISOString(), until: null, label };
    case "month":
      return { since: new Date(now.getFullYear(), now.getMonth(), 1).toISOString(), until: null, label };
    case "quarter": {
      const q = Math.floor(now.getMonth() / 3);
      return { since: new Date(now.getFullYear(), q * 3, 1).toISOString(), until: null, label };
    }
    case "all":
    default:
      return { since: null, until: null, label };
  }
}

// datetime-local wants "YYYY-MM-DDTHH:mm" in *local* time, with no
// timezone suffix — new Date(iso).toISOString() would silently shift it.
function toLocalInputValue(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function rangeLabel(range: DateRange): string {
  if (range.label) return range.label;
  if (!range.since && !range.until) return "All time";
  const fmt = (iso: string) =>
    new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  if (range.since && range.until) return `${fmt(range.since)} – ${fmt(range.until)}`;
  if (range.since) return `Since ${fmt(range.since)}`;
  return `Until ${fmt(range.until!)}`;
}

/**
 * Preset + custom date/time range picker — a Button showing the current
 * range opens a Popover of presets (last N hours/days, today, yesterday,
 * this month/quarter) plus a custom `datetime-local` from/to pair. Presets
 * apply and close immediately; custom range needs an explicit Apply since
 * both ends need to be set before it means anything.
 */
export function DateRangeFilter({
  value,
  onChange,
}: {
  value: DateRange;
  onChange: (range: DateRange) => void;
}) {
  const [open, setOpen] = useState(false);
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const anchorRef = useRef<HTMLButtonElement>(null);

  function openPopover() {
    setCustomFrom(value.since ? toLocalInputValue(value.since) : "");
    setCustomTo(value.until ? toLocalInputValue(value.until) : "");
    setOpen(true);
  }

  function applyPreset(key: PresetKey) {
    onChange(presetRange(key));
    setOpen(false);
  }

  function applyCustom() {
    onChange({
      since: customFrom ? new Date(customFrom).toISOString() : null,
      until: customTo ? new Date(customTo).toISOString() : null,
    });
    setOpen(false);
  }

  const active = !!(value.since || value.until);

  return (
    <>
      <Button
        ref={anchorRef}
        variant={active ? "secondary" : "ghost"}
        size="sm"
        onClick={openPopover}
      >
        <Calendar size={14} style={{ marginRight: 6 }} />
        {rangeLabel(value)}
      </Button>
      <Popover anchorRef={anchorRef} open={open} onClose={() => setOpen(false)} width={260}>
        <div className="col" style={{ gap: 2 }}>
          <button className="palette__item" onClick={() => applyPreset("all")}>All time</button>
          {PRESETS.map((p) => (
            <button key={p.key} className="palette__item" onClick={() => applyPreset(p.key)}>
              {p.label}
            </button>
          ))}
        </div>
        <div className="date-range-custom">
          <div className="ui-field__label" style={{ marginBottom: 6 }}>Custom range</div>
          <div className="col" style={{ gap: 8 }}>
            <input
              type="datetime-local"
              className="ui-input"
              value={customFrom}
              onChange={(e) => setCustomFrom(e.target.value)}
            />
            <input
              type="datetime-local"
              className="ui-input"
              value={customTo}
              onChange={(e) => setCustomTo(e.target.value)}
            />
            <Button variant="primary" size="sm" onClick={applyCustom} disabled={!customFrom && !customTo}>
              Apply
            </Button>
          </div>
        </div>
      </Popover>
    </>
  );
}
