import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, EmptyState, Input, Select, Skeleton, Tag } from "../ui";
import { CopyButton } from "./CopyButton";
import { daysSince, QUIET_DAYS } from "./tone";
import { businessLabel, downloadCsv, formatDay, matchesBusiness } from "./util";
import type { TechPending } from "./types";

// The issue reader's statuses where engineering hasn't shared a cause yet.
const NO_RCA = ["no_reply", "investigating", "not_read"];

function nudge(quiet: number) {
  return `Hi team, any update on this? It's been ${Math.floor(quiet)} days without a reply and the merchant is waiting. `
    + "Please share the RCA and an ETA for the fix.";
}

/** GitHub issues where engineering has gone quiet for QUIET_DAYS+ days and hasn't shared an RCA. */
export function EngineeringQuietView({ tenantId, onOpen }: { tenantId?: string; onOpen: (id: string) => void }) {
  const [data, setData] = useState<TechPending | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [squad, setSquad] = useState("");
  const [search, setSearch] = useState("");

  useEffect(() => {
    api.monitor.techPending(tenantId).then(setData).catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  const quiet = useMemo(() => (data?.squads ?? []).flatMap((s) => s.issues)
    .map((r) => ({ r, days: daysSince(r.last_activity_at ?? r.opened_at) ?? r.days_open }))
    .filter(({ r, days }) => NO_RCA.includes(r.status) && days >= QUIET_DAYS)
    .sort((a, b) => b.days - a.days), [data]);
  const rows = quiet.filter(({ r }) => (!squad || r.squad === squad) && matchesBusiness(search, r.businesses ?? []));

  if (err) return <Banner tone="exception" title={err} />;
  if (!data) return <Skeleton variant="row" lines={6} />;

  const squads = [...new Set(quiet.map(({ r }) => r.squad))].sort();
  const noReply = quiet.filter(({ r }) => r.status === "no_reply").length;
  const download = () => downloadCsv(`engineering-no-rca-${formatDay(new Date().toISOString())}.csv`, [
    ["Issue", "Title", "Squad", "Case", "Business", "Quiet days", "Open days", "Last reply by", "Status", "SLA", "Link"],
    ...rows.map(({ r, days }) => [r.issue, r.title, r.squad, r.case_number,
      (r.businesses ?? []).map(businessLabel).join("; "), Math.floor(days), r.days_open, r.last_reply_by,
      r.tech_status, r.sla_status, r.url]),
  ]);

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>Engineering: no response for {QUIET_DAYS}+ days, RCA not shared</h2>
          <p className="monitor-muted">
            Open GitHub issues with no comment for {QUIET_DAYS} days or more where engineering hasn't given a cause.
            The status is the AI's reading of the thread, so open the issue before chasing.
          </p>
        </div>
        <div className="monitor-actions">
          <Input aria-label="Search business" placeholder="Business name or ID" value={search}
                 onChange={(e) => setSearch(e.target.value)} />
          <Select aria-label="Squad" value={squad} onChange={(e) => setSquad(e.target.value)}
                  options={[{ value: "", label: "Squad: all" }, ...squads.map((s) => ({ value: s, label: s }))]} />
          <Button size="sm" variant="ghost" disabled={!rows.length} onClick={download}>Download CSV</Button>
        </div>
      </header>

      <div className="page-stats">
        <div><strong>{quiet.length}</strong>Quiet {QUIET_DAYS}+ days, no RCA</div>
        <div className={noReply ? "is-warn" : ""}><strong>{noReply}</strong>No engineer reply at all</div>
        <div><strong>{quiet.length - noReply}</strong>Investigating, no cause yet</div>
        <div><strong>{quiet.filter(({ r }) => r.sla_status?.startsWith("Breached")).length}</strong>Support SLA breached</div>
      </div>

      {rows.length === 0 ? (
        <EmptyState title="Nothing quiet" body={`Every open issue has a reply or an RCA in the last ${QUIET_DAYS} days.`} />
      ) : (
        <div className="monitor-esc-wrap">
          <table className="monitor-esc">
            <thead>
              <tr><th>Issue</th><th>Case</th><th>Quiet for</th><th>Last reply by</th><th>RCA</th><th>Support SLA</th><th /></tr>
            </thead>
            <tbody>
              {rows.map(({ r, days }) => (
                <tr key={r.url}>
                  <td>
                    <a className="monitor-link" href={r.url} target="_blank" rel="noreferrer">#{r.issue}</a>{" "}
                    <span className="monitor-muted">{r.squad}</span>
                    <div className="monitor-fu-subject">{r.title}</div>
                    {(r.businesses ?? []).map((b) => <div key={businessLabel(b)} className="monitor-fu-subject">{businessLabel(b)}</div>)}
                  </td>
                  <td>
                    {r.case_id
                      ? <button type="button" className="monitor-link" onClick={() => onOpen(r.case_id!)}>{r.case_number}</button>
                      : <span className="monitor-muted">No open case</span>}
                    {r.priority && <div className="monitor-fu-subject">{r.priority}</div>}
                  </td>
                  <td>
                    <Tag tone={days >= 7 ? "exception" : "warn"}>{Math.floor(days)} days</Tag>
                    <div className="monitor-fu-subject">Open {r.days_open}d</div>
                  </td>
                  <td>{r.last_reply_by ?? "No replies"}</td>
                  <td>
                    <Tag tone={r.status === "no_reply" ? "exception" : "warn-soft"}>{r.tech_status}</Tag>
                    {r.reason && <div className="monitor-fu-subject">{r.reason}</div>}
                  </td>
                  <td>{r.sla_status?.startsWith("Breached") ? <Tag tone="exception">{r.sla_status}</Tag> : r.sla_status ?? "None"}</td>
                  <td className="esc-actions"><CopyButton text={nudge(days)} label="Copy nudge" /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
