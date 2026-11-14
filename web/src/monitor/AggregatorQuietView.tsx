import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, EmptyState, Input, Skeleton, Tag } from "../ui";
import { EscalateButton, type Marks } from "./EscalatedView";
import { QUIET_DAYS } from "./tone";
import { businessLabel, downloadCsv, formatDay, matchesBusiness } from "./util";
import type { Escalation, Escalations } from "./types";

/** Aggregator cases with no real update for QUIET_DAYS or more: the ones to escalate. */
export function AggregatorQuietView({ tenantId, onOpen, marks, thirdParty }: {
  tenantId?: string; onOpen: (id: string) => void; marks: Marks; thirdParty: string;
}) {
  const [data, setData] = useState<Escalations | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [who, setWho] = useState("");
  const [search, setSearch] = useState("");
  const [hideDone, setHideDone] = useState(false);

  useEffect(() => {
    api.monitor.escalations(tenantId, QUIET_DAYS).then(setData).catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  const escalated = (e: Escalation) => {
    const m = marks.marks.get(e.id);
    return !!(m?.manual && m.escalated_at && !m.responded_at);
  };
  const rows = useMemo(() => (data?.cases ?? []).filter((e) => (!who || e.waiting_on.includes(who))
    && matchesBusiness(search, [e]) && !(hideDone && escalated(e))),
  // eslint-disable-next-line react-hooks/exhaustive-deps
  [data, who, search, hideDone, marks.marks]);

  if (err) return <Banner tone="exception" title={err} />;
  if (!data) return <Skeleton variant="row" lines={6} />;

  const notYet = data.cases.filter((e) => !escalated(e)).length;
  const download = () => downloadCsv(`${thirdParty.toLowerCase()}-no-response-${formatDay(new Date().toISOString())}.csv`, [
    ["Case", "Subject", "Business", "Biz ID", thirdParty, "Days without update", "Last update", "Our last message",
      "Owner", "Escalated by us", "Link"],
    ...rows.map((e) => [e.case_number, e.subject, e.business, e.biz_id, e.waiting_on.join(", "), e.days_silent,
      formatDay(e.last_update_at), formatDay(e.our_last_at), e.owner,
      formatDay(marks.marks.get(e.id)?.escalated_at), e.url]),
  ]);

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>{thirdParty} cases with no response for {QUIET_DAYS}+ days</h2>
          <p className="monitor-muted">
            No real update on email, Slack, Jira or GitHub. An acknowledgement doesn't count. Escalate these, then
            press <strong>I escalated this</strong> so they move to the Escalated page.
          </p>
        </div>
        <div className="monitor-actions">
          <Input aria-label="Search business" placeholder="Business name or ID" value={search}
                 onChange={(e) => setSearch(e.target.value)} />
          <Button size="sm" variant={hideDone ? "secondary" : "ghost"} aria-pressed={hideDone}
                  onClick={() => setHideDone(!hideDone)}>Hide escalated</Button>
          <Button size="sm" variant="ghost" disabled={!rows.length} onClick={download}>Download CSV</Button>
        </div>
      </header>
      {marks.err && <Banner tone="exception" title={marks.err} />}

      <div className="page-stats">
        <div><strong>{data.total}</strong>No response {QUIET_DAYS}+ days</div>
        <div className={notYet ? "is-warn" : ""}><strong>{notYet}</strong>Not escalated yet</div>
        {data.summary.map((s) => (
          <button key={s.name} type="button" className={who === s.name ? "is-active" : ""} aria-pressed={who === s.name}
                  onClick={() => setWho(who === s.name ? "" : s.name)}>
            <strong>{s.cases}</strong>{s.name}<small>Longest {s.longest_days}d</small>
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <EmptyState title="Nothing quiet" body={`Every ${thirdParty.toLowerCase()} has updated in the last ${QUIET_DAYS} days.`} />
      ) : (
        <div className="monitor-esc-wrap">
          <table className="monitor-esc">
            <thead>
              <tr><th>Case</th><th>{thirdParty}</th><th>Quiet for</th><th>Their last update</th><th>Our last chase</th><th>Owner</th><th /></tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id}>
                  <td>
                    <button type="button" className="monitor-link" onClick={() => onOpen(e.id)}>{e.case_number}</button>
                    <div className="monitor-fu-subject">{e.subject}</div>
                    {businessLabel(e) && <div className="monitor-fu-subject">{businessLabel(e)}</div>}
                    {e.not_read.length > 0 && <Tag tone="warn">Not read: {e.not_read.join(", ")}</Tag>}
                  </td>
                  <td>{e.waiting_on.join(", ")}</td>
                  <td><Tag tone={e.days_silent >= 7 ? "exception" : "warn"}>{e.days_silent} days</Tag></td>
                  <td>
                    {e.last_update_at ? `${formatDay(e.last_update_at)} · ${e.last_update_via}` : "No update yet"}
                    {e.last_ack_at && <div className="monitor-fu-subject">Ack {formatDay(e.last_ack_at)}</div>}
                  </td>
                  <td>
                    {e.our_last_at ? formatDay(e.our_last_at) : "None"}
                    {e.our_follow_ups > 0 && <div className="monitor-fu-subject">{e.our_follow_ups} chase{e.our_follow_ups === 1 ? "" : "s"}</div>}
                  </td>
                  <td>{e.owner}</td>
                  <td className="esc-actions"><EscalateButton caseId={e.id} marks={marks} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
