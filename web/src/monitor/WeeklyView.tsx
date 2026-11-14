import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, EmptyState, Skeleton, Tag } from "../ui";
import { CopyButton } from "./CopyButton";
import { LastResponse } from "./LastResponse";
import { businessLabel, formatAge } from "./util";
import type { Weekly } from "./types";

export function WeeklyView({ tenantId, onOpen, thirdParty, customer }: {
  tenantId?: string;
  onOpen: (id: string) => void;
  thirdParty: string;
  customer: string;
}) {
  const [data, setData] = useState<Weekly | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.monitor.weekly(tenantId).then(setData).catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  if (err) return <Banner tone="exception" title={err} />;
  if (!data) return <Skeleton variant="row" lines={6} />;

  const { engineering, dsp } = data;
  const leftOut = Object.entries(engineering.left_out);
  return (
    <div className="week">
      <header className="today-hero">
        <h2>Wednesday Report</h2>
        <p>Week from {data.week_label}. Copy a message and post it yourself. Nothing is sent from here.</p>
      </header>

      <section className="week-part" aria-label="Pending from engineering">
        <div className="week-head">
          <h2>Pending from engineering ({engineering.total})</h2>
          {engineering.message && <CopyButton primary text={engineering.message} label="Copy full message" />}
        </div>
        <p className="monitor-muted">
          Only issues with an open case where tech still owes the resolution.
          {leftOut.length > 0 && ` Left out: ${leftOut.map(([k, n]) => `${k} (${n})`).join(", ")}.`}
        </p>
        {engineering.squads.length === 0 && <EmptyState title="Nothing pending from engineering" body="No open case is waiting on tech." />}
        {engineering.squads.map((s) => (
          <div key={s.squad}>
            <div className="week-head">
              <h3>{s.squad} ({s.issues.length})</h3>
              <CopyButton text={s.message} label="Copy squad message" />
            </div>
            <div className="week-table-wrap">
              <table className="monitor-table week-table">
                <thead>
                  <tr><th>Issue</th><th>Case</th><th>Business</th><th>Open</th><th>Status</th><th>Last response</th></tr>
                </thead>
                <tbody>
                  {s.issues.map((r) => (
                    <tr key={r.issue}>
                      <td><a href={r.url} target="_blank" rel="noreferrer">#{r.issue}</a><div className="monitor-subject">{r.title}</div></td>
                      <td><button type="button" className="monitor-link" onClick={() => onOpen(r.case_id)}>{r.case_number}</button></td>
                      <td>{(r.businesses ?? []).map(businessLabel).join(", ")}</td>
                      <td>{r.days_open} days</td>
                      <td>{r.tech_status}</td>
                      <td>
                        {r.last_reply_by
                          ? <>{r.last_reply_by} · GitHub{r.last_activity_at && ` · ${formatAge((Date.now() - new Date(r.last_activity_at).getTime()) / 3600000)} ago`}</>
                          : <span className="monitor-muted">No replies</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
      </section>

      <section className="week-part" aria-label="Aggregators past SLA">
        <div className="week-head"><h2>Aggregators past SLA, no response ({dsp.total})</h2></div>
        <p className="monitor-muted">Only cases where the last move is ours and the aggregator owes a reply.</p>
        {dsp.partners.length === 0 && <EmptyState title="Nothing past SLA" body="Every aggregator has replied or is in time." />}
        {dsp.partners.map((p) => (
          <div key={p.name}>
            <div className="week-head">
              <h3>{p.name} ({p.rows.length})</h3>
              <CopyButton text={p.message} label={`Copy ${p.name} message`} />
            </div>
            <div className="week-table-wrap">
              <table className="monitor-table week-table">
                <thead><tr><th>Case</th><th>Business</th><th>No update</th><th>Last response</th></tr></thead>
                <tbody>
                  {p.rows.map((r) => (
                    <tr key={r.case_id}>
                      <td>
                        <button type="button" className="monitor-link" onClick={() => onOpen(r.case_id)}>{r.case_number}</button>
                        {r.new && <> <Tag>New</Tag></>}
                        <div className="monitor-subject">{r.subject}</div>
                      </td>
                      <td>{businessLabel(r)}</td>
                      <td>{r.quiet_days} days</td>
                      <td><LastResponse latest={r.latest} thirdParty={thirdParty} customer={customer} brief /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
      </section>
    </div>
  );
}
