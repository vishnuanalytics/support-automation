import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, EmptyState, Input, Select, Skeleton, Tag } from "../ui";
import { businessLabel, downloadCsv, formatDay, matchesBusiness } from "./util";
import type { Escalation, Escalations } from "./types";

const lastUpdate = (e: Escalation) => e.last_update_at
  ? `${formatDay(e.last_update_at)} (${e.last_update_via})` : "No update yet";

function csvRows(rows: Escalation[]) {
  return [
    ["Case", "Business", "Biz ID", "Waiting on", "Days without update", "Last update", "Last update via", "Last acknowledgement",
      "Our last message", "Our messages since", "Owner", "Priority", "Record type", "Subject",
      "Linked support cases", "Channels checked", "Channels not read", "Link"],
    ...rows.map((e) => [
      e.case_number, e.business, e.biz_id, e.waiting_on.join(", "), e.days_silent, formatDay(e.last_update_at), e.last_update_via,
      formatDay(e.last_ack_at), formatDay(e.our_last_at), e.our_follow_ups, e.owner, e.priority,
      e.record_type, e.subject, e.support_cases.join(" "), e.checked.join(" "), e.not_read.join(" "), e.url,
    ]),
  ];
}

export function EscalationsView({ tenantId, onOpen }: { tenantId?: string; onOpen: (id: string) => void }) {
  const [data, setData] = useState<Escalations | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [who, setWho] = useState("");
  const [search, setSearch] = useState("");

  useEffect(() => {
    api.monitor.escalations(tenantId).then(setData).catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  const rows = useMemo(() => (data?.cases ?? []).filter((e) => (!who || e.waiting_on.includes(who))
    && matchesBusiness(search, [e])), [data, who, search]);

  if (err) return <Banner tone="exception" title={err} />;
  if (!data) return <Skeleton variant="row" lines={6} />;

  const download = () => {
    const scope = who ? `-${who.toLowerCase().replace(/[^a-z0-9]+/g, "-")}` : "";
    downloadCsv(`escalations${scope}-${formatDay(new Date().toISOString())}.csv`, csvRows(rows));
  };

  return (
    <section className="monitor-digest">
      <div className="monitor-fu-owner">
        <div>
          <h2>{rows.length} escalation{rows.length === 1 ? "" : "s"}</h2>
          <p className="monitor-muted">
            No real update from the aggregator for {data.days}+ days across all channels. Acknowledgements don't count.
          </p>
        </div>
        <div className="monitor-actions">
          <Input aria-label="Search business" placeholder="Business name or ID" value={search}
                 onChange={(e) => setSearch(e.target.value)} />
          <Select aria-label="Waiting on" value={who} onChange={(e) => setWho(e.target.value)}
                  options={[{ value: "", label: "Waiting on: all" },
                    ...data.summary.map((s) => ({ value: s.name, label: `${s.name} (${s.cases})` }))]} />
          <Button size="sm" variant="primary" disabled={!rows.length} onClick={download}>Download CSV</Button>
        </div>
      </div>

      {data.total === 0 ? (
        <EmptyState title="No escalations" body={`Everyone we're waiting on has updated in the last ${data.days} days.`} />
      ) : (
        <>
          <div className="monitor-fu-head">
            {data.summary.map((s) => (
              <div key={s.name}>
                <strong>{s.cases}</strong>
                {s.name}
                <div className="monitor-muted">Longest {s.longest_days}d</div>
              </div>
            ))}
          </div>
          <div className="monitor-esc-wrap">
            <table className="monitor-esc">
              <thead>
                <tr>
                  <th>Case</th><th>Waiting on</th><th>No update for</th><th>Last update</th>
                  <th>Our last message</th><th>Owner</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((e) => (
                  <tr key={e.id}>
                    <td>
                      <button type="button" className="monitor-link" onClick={() => onOpen(e.id)}>
                        {e.case_number}
                      </button>
                      <div className="monitor-fu-subject">{e.subject}</div>
                      {businessLabel(e) && <div className="monitor-fu-subject">{businessLabel(e)}</div>}
                      {e.support_cases.length > 0 && (
                        <div className="monitor-fu-subject">Support: {e.support_cases.join(", ")}</div>
                      )}
                      {e.not_read.length > 0 && <Tag tone="warn">Not read: {e.not_read.join(", ")}</Tag>}
                    </td>
                    <td>{e.waiting_on.join(", ")}</td>
                    <td><strong>{e.days_silent} days</strong></td>
                    <td>
                      {lastUpdate(e)}
                      {e.last_ack_at && <div className="monitor-fu-subject">Ack {formatDay(e.last_ack_at)}</div>}
                    </td>
                    <td>
                      {e.our_last_at ? formatDay(e.our_last_at) : "None"}
                      {e.our_follow_ups > 0 && (
                        <div className="monitor-fu-subject">
                          {e.our_follow_ups} {e.last_update_at ? "since their update" : "sent, no reply"}
                        </div>
                      )}
                    </td>
                    <td>{e.owner}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
