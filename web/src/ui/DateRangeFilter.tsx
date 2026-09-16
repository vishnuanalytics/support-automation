import { useRef, useState } from "react";
import { Calendar as CalendarIcon } from "lucide-react";
import { DayPicker, type DateRange as PickedRange } from "react-day-picker";
// stylesheet is imported once in main.tsx, before ui.css, so our --rdp-*
// overrides there always win the cascade — see the comment there.
import { Button } from "./Button";
import { Popover } from "./Popover";
import { Dialog } from "./Dialog";

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

const pad = (n: number) => String(n).padStart(2, "0");
const toTimeInput = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

function withTime(date: Date, time: string): Date {
  const [h, m] = time.split(":").map(Number);
  const d = new Date(date);
  d.setHours(h || 0, m || 0, 0, 0);
  return d;
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
 * this month/quarter). Presets apply and close immediately. "Custom
 * range…" opens a Dialog with an actual calendar (click a start day, then
 * an end day — react-day-picker's range mode) instead of typing dates by
 * hand, plus two time-of-day inputs to refine the exact from/to instant.
 */
export function DateRangeFilter({
  value,
  onChange,
}: {
  value: DateRange;
  onChange: (range: DateRange) => void;
}) {
  const [open, setOpen] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const [picked, setPicked] = useState<PickedRange | undefined>();
  const [fromTime, setFromTime] = useState("00:00");
  const [toTime, setToTime] = useState("23:59");
  const anchorRef = useRef<HTMLButtonElement>(null);

  function applyPreset(key: PresetKey) {
    onChange(presetRange(key));
    setOpen(false);
  }

  function openCustom() {
    setOpen(false);
    const from = value.since ? new Date(value.since) : undefined;
    const to = value.until ? new Date(value.until) : undefined;
    setPicked(from ? { from, to } : undefined);
    setFromTime(from ? toTimeInput(from) : "00:00");
    setToTime(to ? toTimeInput(to) : "23:59");
    setCustomOpen(true);
  }

  function applyCustom() {
    if (!picked?.from) return;
    const since = withTime(picked.from, fromTime).toISOString();
    const until = picked.to ? withTime(picked.to, toTime).toISOString() : null;
    onChange({ since, until });
    setCustomOpen(false);
  }

  const active = !!(value.since || value.until);

  return (
    <>
      <Button
        ref={anchorRef}
        variant={active ? "secondary" : "ghost"}
        size="sm"
        onClick={() => setOpen(true)}
      >
        <CalendarIcon size={14} style={{ marginRight: 6 }} />
        {rangeLabel(value)}
      </Button>
      <Popover anchorRef={anchorRef} open={open} onClose={() => setOpen(false)} width={220}>
        <div className="col" style={{ gap: 2 }}>
          <button className="palette__item" onClick={() => applyPreset("all")}>All time</button>
          {PRESETS.map((p) => (
            <button key={p.key} className="palette__item" onClick={() => applyPreset(p.key)}>
              {p.label}
            </button>
          ))}
          <button className="palette__item" onClick={openCustom} style={{ borderTop: "1px solid var(--line)", marginTop: 2, paddingTop: 10 }}>
            Custom range…
          </button>
        </div>
      </Popover>

      <Dialog
        open={customOpen}
        onClose={() => setCustomOpen(false)}
        title="Custom date range"
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setCustomOpen(false) },
          { label: "Apply", variant: "primary", onClick: applyCustom, disabled: !picked?.from },
        ]}
      >
        <div className="col" style={{ gap: 12 }}>
          <div className="muted" style={{ fontSize: 12 }}>
            Click a start day, then an end day — or just a start day for an open-ended range.
          </div>
          <DayPicker
            mode="range"
            selected={picked}
            onSelect={setPicked}
            numberOfMonths={1}
            showOutsideDays
            captionLayout="dropdown-years"
            endMonth={new Date()}
          />
          <div className="row" style={{ gap: 10 }}>
            <div style={{ flex: 1 }}>
              <div className="ui-field__label">Start time</div>
              <input
                type="time"
                className="ui-input"
                value={fromTime}
                disabled={!picked?.from}
                onChange={(e) => setFromTime(e.target.value)}
              />
            </div>
            <div style={{ flex: 1 }}>
              <div className="ui-field__label">End time</div>
              <input
                type="time"
                className="ui-input"
                value={toTime}
                disabled={!picked?.to}
                onChange={(e) => setToTime(e.target.value)}
              />
            </div>
          </div>
        </div>
      </Dialog>
    </>
  );
}
