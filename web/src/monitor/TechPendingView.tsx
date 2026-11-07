import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, EmptyState, Input, Select, Skeleton, Tag } from "../ui";
import { businessLabel, downloadCsv, formatDay, formatDayTime, matchesBusiness } from "./util";
import type { TechIssue, TechPending } from "./types";

const WAITING: Record<string, string> = {
  tech: "Tech", support: "Support", merchant: "Merchant", third_party: "Third party", nobody: "No one",
};

function csvRows(rows: TechIssue[]) {
  return [
    ["#", "Squad", "Issue #", "Title", "Last reply by", "GH Open since", "Case Priority", "Support SLA due",
      "Support SLA status", "Status", "Waiting on", "AI note", "Case", "Other open cases", "Business", "GitHub link"],
    ...rows.map((r, i) => [
      i + 1, r.squad, r.issue, r.title, r.last_reply_by ?? "No replies", `${r.days_open} days`, r.priority,
      formatDayTime(r.sla_due), r.sla_status, r.tech_status, r.waiting_on ? WAITING[r.waiting_on] : "", r.reason,
      r.case_number ?? "No open case", r.other_cases.join(" "), (r.businesses ?? []).map(businessLabel).join("; "), r.url,
    ]),
  ];
}

function count(rows: TechIssue[], match: (r: TechIssue) => boolean) {
  return rows.filter(match).length;
}

export function TechPendingView({ tenantId, onOpen }: { tenantId?: string; onOpen: (id: string) => void }) {
  const [data, setData] = useState<TechPending | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [squad, setSquad] = useState("");
  const [status, setStatus] = useState("");
  const [age, setAge] = useState("");
  const [search, setSearch] = useState("");

  useEffect(() => {
    api.monitor.techPending(tenantId).then(setData).catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  const all = useMemo(() => (data?.squads ?? []).flatMap((s) => s.issues), [data]);
  const groups = useMemo(() => (data?.squads ?? [])
    .filter((s) => !squad || s.squad === squad)
    .map((s) => ({
      ...s,
      issues: s.issues.filter((r) => (!status || r.status === status) && (!age || r.over_days)
        && matchesBusiness(search, r.businesses ?? [])),
    }))
    .filter((s) => s.issues.length > 0), [data, squad, status, age, search]);
  const rows = groups.flatMap((g) => g.issues);

  if (err) return <Banner tone="exception" title={err} />;
  if (!data) return <Skeleton variant="row" lines={6} />;

  const present = Object.entries(data.statuses).filter(([k]) => all.some((r) => r.status === k));
  const download = () => {
    const scope = squad ? `-${squad.toLowerCase().replace(/[^a-z0-9]+/g, "-")}` : "";
    downloadCsv(`engineering-issues${scope}-${formatDay(new Date().toISOString())}.csv`, csvRows(rows));
  };

  return (
    <section className="monitor-digest">
      <div className="monitor-fu-owner">
        <div>
          <h2>{rows.length} open support issue{rows.length === 1 ? "" : "s"}</h2>
          <p className="monitor-muted">
            {present.map(([k, label]) => `${label}: ${count(all, (r) => r.status === k)}`).join(" · ")}
          </p>
          <p className="monitor-muted">
            Status is the AI's reading of the comments, so open the issue before acting on it.
            {data.unread > 0 && ` ${data.unread} couldn't be loaded from GitHub.`}
          </p>
        </div>
        <div className="monitor-actions">
          <Input aria-label="Search business" placeholder="Business name or ID" value={search}
                 onChange={(e) => setSearch(e.target.value)} />
          <Select aria-label="Squad" value={squad} onChange={(e) => setSquad(e.target.value)}
                  options={[{ value: "", label: "Squad: all" },
                    ...data.squads.map((s) => ({ value: s.squad, label: `${s.squad} (${s.issues.length})` }))]} />
          <Select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}
                  options={[{ value: "", label: "Status: all" },
                    ...present.map(([k, label]) => ({ value: k, label: `${label} (${count(all, (r) => r.status === k)})` }))]} />
          <Select aria-label="Age" value={age} onChange={(e) => setAge(e.target.value)}
                  options={[{ value: "", label: "Age: all" },
                    { value: "old", label: `More than ${data.days} days (${count(all, (r) => r.over_days)})` }]} />
          <Button size="sm" variant="primary" disabled={!rows.length} onClick={download}>Download CSV</Button>
        </div>
      </div>

      {data.total === 0 && <EmptyState title="No open support issues" body="The next sync will fill this list." />}
      {data.total > 0 && rows.length === 0 && (
        <EmptyState title="Nothing matches" body="No open support issue fits these filters." />
      )}
      {groups.map((g) => (
        <div className="monitor-esc-wrap" key={g.squad}>
          <table className="monitor-esc monitor-esc--wide">
            <thead>
              <tr>
                <th>#</th><th>Squad</th><th>Issue #</th><th>Title</th><th>Business</th><th>Last reply by</th><th>GH Open since</th>
                <th>Case Priority</th><th>Support SLA due</th><th>Support SLA status</th><th>Status</th><th>Case</th>
              </tr>
            </thead>
            <tbody>
              {g.issues.map((r, i) => (
                <tr key={r.url}>
                  <td>{i + 1}</td>
                  <td>{r.squad}</td>
                  <td><a className="monitor-link" href={r.url} target="_blank" rel="noreferrer">{r.issue}</a></td>
                  <td>{r.title}</td>
                  <td>
                    {(r.businesses ?? []).map((b) => <div key={businessLabel(b)}>{businessLabel(b)}</div>)}
                  </td>
                  <td>{r.last_reply_by ?? "No replies"}</td>
                  <td>{r.days_open} days</td>
                  <td>{r.priority}</td>
                  <td>{formatDayTime(r.sla_due)}</td>
                  <td>
                    {r.sla_status?.startsWith("Breached")
                      ? <Tag tone="exception">{r.sla_status}</Tag> : r.sla_status}
                  </td>
                  <td className="monitor-esc-status">
                    {r.status === "no_reply" ? <Tag tone="warn">{r.tech_status}</Tag> : r.tech_status}
                    {r.waiting_on && <div className="monitor-fu-subject">Waiting on: {WAITING[r.waiting_on]}</div>}
                    {r.reason && <div className="monitor-fu-subject">{r.reason}</div>}
                  </td>
                  <td>
                    {r.case_id ? (
                      <button type="button" className="monitor-link" onClick={() => r.case_id && onOpen(r.case_id)}>
                        {r.case_number}
                      </button>
                    ) : "No open case"}
                    {r.other_cases.length > 0 && (
                      <div className="monitor-fu-subject">+{r.other_cases.length} more</div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}
