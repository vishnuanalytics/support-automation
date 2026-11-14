import { useMemo, useState } from "react";
import { Button, EmptyState, Input, Select, Tag, type TagTone } from "../ui";
import { LastResponse } from "./LastResponse";
import { lastParty, replyTone, toneLabel, toneTag, TONE_ORDER, type Party } from "./tone";
import { downloadCsv, formatAge, formatDay } from "./util";
import type { QueueCase } from "./types";

type Filters = {
  search: string; waiting: string; recordType: string; aggregator: string; owner: string;
  subStatus: string; module: string; bu: string; turn: string; party: string; tone: string;
};
const NONE: Filters = {
  search: "", waiting: "", recordType: "", aggregator: "", owner: "", subStatus: "", module: "", bu: "",
  turn: "", party: "", tone: "",
};
const KEY = "monitor.cases.filters";

const TURN: Record<string, { label: string; tone: TagTone }> = {
  act: { label: "Our move", tone: "exception" },
  close: { label: "Ready to close", tone: "success" },
  raise: { label: "Not raised", tone: "warn" },
  engineering: { label: "Waiting on engineering", tone: "neutral" },
  third_party: { label: "Waiting on aggregator", tone: "neutral" },
  customer: { label: "Waiting on merchant", tone: "neutral" },
  check: { label: "Check by hand", tone: "warn-soft" },
  ai: { label: "AI agent has it", tone: "neutral" },
};

function load(): Filters {
  try {
    return { ...NONE, ...JSON.parse(localStorage.getItem(KEY) || "{}") };
  } catch {
    return NONE;
  }
}

function options(all: string, values: (string | null | undefined)[]) {
  const counts = new Map<string, number>();
  for (const v of values) if (v) counts.set(v, (counts.get(v) ?? 0) + 1);
  return [{ value: "", label: all },
    ...[...counts].sort((a, b) => a[0].localeCompare(b[0])).map(([v, n]) => ({ value: v, label: `${v} (${n})` }))];
}

export function CasesView({ cases, onOpen, labels }: {
  cases: QueueCase[]; onOpen: (id: string) => void; labels: { thirdParty: string; customer: string };
}) {
  const [f, setF] = useState<Filters>(load);
  const [showReviewed, setShowReviewed] = useState(false);
  const set = (patch: Partial<Filters>) => {
    const next = { ...f, ...patch };
    setF(next);
    try { localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* convenience only */ }
  };
  const partyLabel: Record<Party, string> = {
    customer: labels.customer, third_party: labels.thirdParty, engineering: "Engineering", us: "Agent (us)", none: "No reply",
  };

  const base = useMemo(() => cases.filter((c) => showReviewed || !c.hidden), [cases, showReviewed]);
  const rows = useMemo(() => {
    const q = f.search.trim().toLowerCase();
    return base.filter((c) =>
      (!f.waiting || c.waiting_raw === f.waiting) && (!f.recordType || c.record_type === f.recordType)
      && (!f.aggregator || c.third_parties.includes(f.aggregator)) && (!f.owner || c.owner === f.owner)
      && (!f.subStatus || c.sub_status === f.subStatus) && (!f.module || c.module === f.module)
      && (!f.bu || c.bu === f.bu) && (!f.turn || c.stage === f.turn)
      && (!f.party || lastParty(c) === f.party) && (!f.tone || replyTone(c) === f.tone)
      && (!q || `${c.case_number} ${c.subject} ${c.owner}`.toLowerCase().includes(q)));
  }, [base, f]);
  const active = (Object.keys(f) as (keyof Filters)[]).filter((k) => f[k] && k !== "search").length;

  const download = () => downloadCsv(`cases-${formatDay(new Date().toISOString())}.csv`, [
    ["Case", "Subject", "Record type", "Waiting on", "Sub-status", labels.thirdParty, "Owner", "Whose turn",
      "Last reply from", "Reply", "Last reply at", "Overdue hours", "Link"],
    ...rows.map((c) => [c.case_number, c.subject, c.record_type, c.waiting_raw, c.sub_status, c.third_parties.join(", "),
      c.owner, TURN[c.stage]?.label, partyLabel[lastParty(c)], replyTone(c) ? toneLabel(replyTone(c)!) : "",
      c.latest?.at ?? "", Math.round(c.overdue_hours), c.url]),
  ]);

  const parties: Party[] = ["customer", "third_party", "engineering", "us", "none"];

  return (
    <div className="cases">
      <section className="reply-strip" aria-label="Last reply from">
        {parties.map((p) => {
          const own = base.filter((c) => lastParty(c) === p);
          if (!own.length) return null;
          const on = f.party === p;
          const tones = TONE_ORDER.map((t) => [t, own.filter((c) => replyTone(c) === t).length] as const).filter(([, n]) => n);
          return (
            <article key={p} className={on ? "is-active" : ""}>
              <button type="button" className="reply-strip-title" aria-pressed={on && !f.tone}
                      onClick={() => set({ party: on && !f.tone ? "" : p, tone: "" })}>
                <span>Last reply: {partyLabel[p]}</span><strong>{own.length}</strong>
              </button>
              {tones.length > 0 && (
                <div className="reply-strip-tones">
                  {tones.map(([t, n]) => (
                    <button key={t} type="button" aria-pressed={on && f.tone === t}
                            className={`reply-tone reply-tone--${toneTag(t)}${on && f.tone === t ? " is-active" : ""}`}
                            onClick={() => set(on && f.tone === t ? { party: "", tone: "" } : { party: p, tone: t })}>
                      {toneLabel(t)} <span>{n}</span>
                    </button>
                  ))}
                </div>
              )}
            </article>
          );
        })}
      </section>

      <div className="cases-filters" aria-label="Salesforce filters">
        <Input aria-label="Search cases" placeholder="Case, subject or owner" value={f.search} onChange={(e) => set({ search: e.target.value })} />
        <Select aria-label="Waiting on" value={f.waiting} onChange={(e) => set({ waiting: e.target.value })} options={options("Waiting on: all", base.map((c) => c.waiting_raw))} />
        <Select aria-label="Record type" value={f.recordType} onChange={(e) => set({ recordType: e.target.value })} options={options("Record type: all", base.map((c) => c.record_type))} />
        <Select aria-label={labels.thirdParty} value={f.aggregator} onChange={(e) => set({ aggregator: e.target.value })} options={options(`${labels.thirdParty}: all`, base.flatMap((c) => c.third_parties))} />
        <Select aria-label="Owner" value={f.owner} onChange={(e) => set({ owner: e.target.value })} options={options("Owner: all", base.map((c) => c.owner))} />
        <Select aria-label="Sub-status" value={f.subStatus} onChange={(e) => set({ subStatus: e.target.value })} options={options("Sub-status: all", base.map((c) => c.sub_status))} />
        <Select aria-label="Module" value={f.module} onChange={(e) => set({ module: e.target.value })} options={options("Module: all", base.map((c) => c.module))} />
        <Select aria-label="BU" value={f.bu} onChange={(e) => set({ bu: e.target.value })} options={options("BU: all", base.map((c) => c.bu))} />
        <Select aria-label="Whose turn" value={f.turn} onChange={(e) => set({ turn: e.target.value })}
                options={[{ value: "", label: "Whose turn: all" }, ...Object.entries(TURN).map(([v, t]) => ({ value: v, label: `${t.label} (${base.filter((c) => c.stage === v).length})` }))]} />
        <Select aria-label="Reply" value={f.tone} onChange={(e) => set({ tone: e.target.value })}
                options={[{ value: "", label: "Reply: all" }, ...TONE_ORDER.map((t) => ({ value: t, label: toneLabel(t) }))]} />
      </div>
      <div className="cases-bar">
        <span><strong>{rows.length}</strong> of {base.length} cases</span>
        {active > 0 && <Button size="sm" variant="ghost" onClick={() => set({ ...NONE, search: f.search })}>Clear {active} filter{active === 1 ? "" : "s"}</Button>}
        <label className="monitor-check">
          <input type="checkbox" checked={showReviewed} onChange={(e) => setShowReviewed(e.target.checked)} /> Show reviewed
        </label>
        <span className="today-spacer" />
        <Button size="sm" variant="ghost" disabled={!rows.length} onClick={download}>Download CSV</Button>
      </div>

      {rows.length === 0 ? <EmptyState title="No cases match" body="Clear a filter to see more." /> : (
        <div className="monitor-esc-wrap">
          <table className="monitor-esc cases-table">
            <thead><tr><th>Case</th><th>Salesforce</th><th>Whose turn</th><th>Last reply</th><th>Owner</th></tr></thead>
            <tbody>
              {rows.map((c) => {
                const tone = replyTone(c);
                const turn = TURN[c.stage];
                return (
                  <tr key={c.id} onClick={() => onOpen(c.id)}>
                    <td>
                      <button type="button" className="monitor-link" onClick={(e) => { e.stopPropagation(); onOpen(c.id); }}>{c.case_number}</button>
                      <div className="monitor-fu-subject">{c.subject}</div>
                    </td>
                    <td className="monitor-fu-subject">
                      {[c.record_type, c.waiting_raw && `Waiting on ${c.waiting_raw}`, c.sub_status !== "None" && c.sub_status,
                        c.third_parties.join(", "), c.module].filter(Boolean).join(" · ")}
                    </td>
                    <td>
                      {turn && <Tag tone={turn.tone}>{turn.label}</Tag>}
                      {c.overdue && <div><Tag tone="exception">Overdue {formatAge(c.overdue_hours)}</Tag></div>}
                    </td>
                    <td>
                      {tone && <div><Tag tone={toneTag(tone)}>{toneLabel(tone)}</Tag></div>}
                      <LastResponse latest={c.latest} {...labels} brief />
                    </td>
                    <td>{c.owner}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
