import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, EmptyState, Skeleton, Tag } from "../ui";
import { copyText, downloadCsv, formatDay, formatDayTime } from "./util";
import type { SlaReport } from "./types";

function hours(h: number) {
  return h % 24 === 0 ? `${h / 24}d` : `${h}h`;
}

function reportText(d: SlaReport) {
  return [
    `Aggregator SLA, week from ${formatDay(d.since)}`,
    ...d.partners.map((p) => `${p.name}: ${p.open_late} of ${p.open} open past SLA, ${p.closed_in_time} of ${p.closed_timed} SLA cases closed in time`),
  ].join("\n");
}

export function SlaReportView({ tenantId, onOpen }: { tenantId?: string; onOpen: (id: string) => void }) {
  const [data, setData] = useState<SlaReport | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.monitor.slaReport(tenantId).then(setData).catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  const late = useMemo(() => (data?.rows ?? [])
    .filter((r) => r.state !== "on_track").sort((a, b) => a.hours_left - b.hours_left), [data]);

  if (err) return <Banner tone="exception" title={err} />;
  if (!data) return <Skeleton variant="row" lines={6} />;

  const download = () => downloadCsv(`aggregator-sla-${formatDay(new Date().toISOString())}.csv`, [
    ["Aggregator", "Case", "Subject", "Agent", "Priority", "SLA", "Raised", "Due", "State", "Last update"],
    ...late.map((r) => [r.partner, r.case_number, r.subject, r.owner, r.priority, r.rule, formatDayTime(r.raised_at),
      formatDayTime(r.deadline), r.state === "breached" ? "Past SLA" : "Escalate", formatDayTime(r.last_update_at)]),
  ]);

  return (
    <section className="monitor-digest">
      <div className="monitor-fu-owner">
        <div>
          <h2>Aggregator SLA this week</h2>
          <p className="monitor-muted">Each kind of issue has its own clock. Rules tighten once most cases beat them.</p>
        </div>
        <div className="monitor-actions">
          <Button size="sm" variant="secondary" onClick={() => void copyText(reportText(data))}>Copy report</Button>
          <Button size="sm" variant="primary" disabled={!late.length} onClick={download}>Download CSV</Button>
        </div>
      </div>

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
              <tr><th>Aggregator</th><th>Open</th><th>Open past SLA</th><th>Closed this week</th><th>SLA cases closed in time</th></tr>
            </thead>
            <tbody>
              {data.partners.map((p) => (
                <tr key={p.name}>
                  <td>{p.name}</td><td>{p.open}</td>
                  <td>{p.open_late ? <Tag tone="exception">{p.open_late}</Tag> : 0}</td>
                  <td>{p.closed}</td><td>{p.closed_timed ? `${p.closed_in_time} of ${p.closed_timed}` : "None"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {late.length > 0 && (
        <div className="monitor-esc-wrap">
          <table className="monitor-esc">
            <thead>
              <tr><th>Case</th><th>Aggregator</th><th>Issue</th><th>SLA</th><th>Due</th><th>State</th><th>Agent</th></tr>
            </thead>
            <tbody>
              {late.map((r) => (
                <tr key={r.case_id}>
                  <td><button type="button" className="monitor-link" onClick={() => onOpen(r.case_id)}>{r.case_number}</button></td>
                  <td>{r.partner}</td><td>{r.subject}</td><td>{r.rule}</td><td>{formatDayTime(r.deadline)}</td>
                  <td><Tag tone={r.state === "breached" ? "exception" : "warn"}>{r.state === "breached" ? "Past SLA" : "Escalate"}</Tag></td>
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
