import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, EmptyState, Input, Skeleton, Tag } from "../ui";
import { businessLabel, copyText, downloadCsv, formatDay, formatDayTime, matchesBusiness } from "./util";
import type { SlaReport } from "./types";

function hours(h: number) {
  return h % 24 === 0 ? `${h / 24}d` : `${h}h`;
}

const STATES: Record<string, { label: string; tone: "exception" | "warn" | "success" }> = {
  breached: { label: "Past SLA", tone: "exception" },
  escalate: { label: "Escalate", tone: "warn" },
  on_track: { label: "On track", tone: "success" },
};

function reportText(d: SlaReport) {
  return [
    `Aggregator SLA, week from ${formatDay(d.since)}`,
    ...d.partners.map((p) => `${p.name}: ${p.open_late} of ${p.open} new cases past SLA, ${p.old_late} of ${p.old_open} old cases past SLA, ${p.closed_in_time} of ${p.closed_timed} new cases closed in time`),
  ].join("\n");
}

export function SlaReportView({ tenantId, onOpen }: { tenantId?: string; onOpen: (id: string) => void }) {
  const [data, setData] = useState<SlaReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  useEffect(() => {
    api.monitor.slaReport(tenantId).then(setData).catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  // A search looks through every open case; without one only the late ones are listed.
  const shown = useMemo(() => (data?.rows ?? [])
    .filter((r) => (search.trim() ? matchesBusiness(search, [r]) : r.state !== "on_track"))
    .sort((a, b) => Number(b.new) - Number(a.new) || a.hours_left - b.hours_left), [data, search]);

  if (err) return <Banner tone="exception" title={err} />;
  if (!data) return <Skeleton variant="row" lines={6} />;

  const download = () => downloadCsv(`aggregator-sla-${formatDay(new Date().toISOString())}.csv`, [
    ["Aggregator", "Case", "Business", "Biz ID", "Subject", "Agent", "Priority", "SLA", "Raised", "Due", "State", "Last update"],
    ...shown.map((r) => [r.partner, r.case_number, r.business, r.biz_id, r.subject, r.owner, r.priority, r.rule, formatDayTime(r.raised_at),
      formatDayTime(r.deadline), STATES[r.state]?.label, formatDayTime(r.last_update_at)]),
  ]);

  return (
    <section className="monitor-digest">
      <div className="monitor-fu-owner">
        <div>
          <h2>Aggregator SLA this week</h2>
          <p className="monitor-muted">Each kind of issue has its own clock. Rules tighten once most cases beat them.</p>
          {data.sla_start && (
            <p className="monitor-muted">
              Only cases raised from {formatDay(data.sla_start)} are judged by these SLAs. Anything older is old backlog.
            </p>
          )}
        </div>
        <div className="monitor-actions">
          <Input aria-label="Search business" placeholder="Business name or ID" value={search}
                 onChange={(e) => setSearch(e.target.value)} />
          <Button size="sm" variant="secondary" onClick={() => void copyText(reportText(data))}>Copy report</Button>
          <Button size="sm" variant="primary" disabled={!shown.length} onClick={download}>Download CSV</Button>
        </div>
      </div>

      {data.progress.measures.length > 0 && (
        <div className="monitor-esc-wrap">
          <table className="monitor-esc">
            <thead>
              <tr><th>Your workload</th><th>Started {formatDay(data.progress.since)}</th><th>Latest</th><th>Change</th></tr>
            </thead>
            <tbody>
              {data.progress.measures.map((m) => (
                <tr key={m.key}>
                  <td>{m.label}</td><td>{m.start ?? "None yet"}</td><td>{m.now ?? "None yet"}</td>
                  <td>
                    {m.change === "better" && <Tag tone="success">Better</Tag>}
                    {m.change === "worse" && <Tag tone="warn">Worse</Tag>}
                    {m.change === "same" && "Same"}
                    {m.change === "too_early" && "Too early"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="monitor-muted">
            Each number is a daily average over a week, so Change shows after 7 days of data. Recorded so far: {data.progress.days}.
          </p>
        </div>
      )}

      {data.rules.length > 0 && (
        <div className="monitor-esc-wrap">
          <table className="monitor-esc">
            <thead>
              <tr><th>Issue type</th><th>Escalate after</th><th>Resolve within</th><th>Goal</th><th>Closed in time this week</th><th>Next step</th></tr>
            </thead>
            <tbody>
              {data.rules.map((r) => (
                <tr key={r.name}>
                  <td>{r.name}</td><td>{hours(r.escalate_hours)}</td><td>{hours(r.resolve_hours)}</td>
                  <td>{r.goal_resolve_hours ? hours(r.goal_resolve_hours) : ""}</td>
                  <td>{r.closed ? `${r.in_time} of ${r.closed} (${r.share}%)` : "None closed"}</td>
                  <td>{r.ready ? <Tag tone="success">Ready to tighten</Tag> : "Keep for now"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.partners.length === 0 && <EmptyState title="Nothing to report" body="No aggregator cases this week." />}
      {data.partners.length > 0 && (
        <div className="monitor-esc-wrap">
          <table className="monitor-esc">
            <thead>
              <tr>
                <th>Aggregator</th><th>New open</th><th>New past SLA</th><th>Old open</th><th>Old past SLA</th>
                <th>Closed this week</th><th>New cases closed in time</th>
              </tr>
            </thead>
            <tbody>
              {data.partners.map((p) => (
                <tr key={p.name}>
                  <td>{p.name}</td><td>{p.open}</td>
                  <td>{p.open_late ? <Tag tone="exception">{p.open_late}</Tag> : 0}</td>
                  <td>{p.old_open}</td><td>{p.old_late}</td>
                  <td>{p.closed}</td><td>{p.closed_timed ? `${p.closed_in_time} of ${p.closed_timed}` : "None"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {search.trim() && shown.length === 0 && (
        <EmptyState title="Nothing matches" body="No open aggregator case has this business name or ID." />
      )}
      {shown.length > 0 && (
        <div className="monitor-esc-wrap">
          <table className="monitor-esc">
            <thead>
              <tr><th>Case</th><th>Business</th><th>Aggregator</th><th>Issue</th><th>SLA</th><th>Due</th><th>State</th><th>Agent</th></tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.case_id}>
                  <td><button type="button" className="monitor-link" onClick={() => onOpen(r.case_id)}>{r.case_number}</button></td>
                  <td>{businessLabel(r)}</td><td>{r.partner}</td><td>{r.subject}</td><td>{r.rule}</td><td>{formatDayTime(r.deadline)}</td>
                  <td><Tag tone={STATES[r.state]?.tone}>{STATES[r.state]?.label}</Tag>
                    {!r.new && <div className="monitor-fu-subject">Old backlog</div>}
                  </td>
                  <td>{r.owner}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
