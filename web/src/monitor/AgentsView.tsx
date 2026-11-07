import { Fragment, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, EmptyState, Skeleton, Tag } from "../ui";
import { CopyButton } from "./CopyButton";
import type { AgentRow, AgentsReport } from "./types";

function Detail({ a, onOpen }: { a: AgentRow; onOpen: (id: string) => void }) {
  return (
    <div className="agent-detail">
      <ul>
        {[...a.cases, ...a.check].map((c) => (
          <li key={c.case_id}>
            <p>
              <button type="button" className="monitor-link" onClick={() => onOpen(c.case_id)}>{c.case_number}</button>{" "}
              {c.subject}
            </p>
            {c.problems.map((p) => <p key={p.text}><Tag tone="exception">{p.kind}</Tag> {p.text}</p>)}
            {c.check.map((t) => <p key={t}><Tag tone="warn-soft">Please check</Tag> {t}</p>)}
          </li>
        ))}
        {a.github.map((g) => (
          <li key={g.url}>
            <p><a href={g.url} target="_blank" rel="noreferrer">{g.title}</a></p>
            <p><Tag tone="exception">Didn't need engineering</Tag> {g.reasons.join(", ")}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AgentsView({ tenantId, onOpen }: { tenantId?: string; onOpen: (id: string) => void }) {
  const [data, setData] = useState<AgentsReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    api.monitor.agents(tenantId).then(setData).catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  if (err) return <Banner tone="exception" title={err} />;
  if (!data) return <Skeleton variant="row" lines={6} />;

  const agents = data.agents.filter((a) => a.wrong || a.check.length);
  return (
    <div className="week">
      <header className="today-hero">
        <h2>{data.wrong} of {data.raised} raises had a problem</h2>
        <p>
          Open aggregator cases raised in the last {data.days} days, plus GitHub issues closed as not needing an
          engineer. Every flag shows its reason, so talk it through before treating it as a score.
        </p>
      </header>
      {agents.length === 0 ? <EmptyState title="No wrong raises found" body="Nothing to guide anyone on right now." /> : (
        <div className="week-table-wrap">
          <table className="monitor-table week-table">
            <thead><tr><th>Agent</th><th>Raised</th><th>Wrong</th><th>Please check</th><th>Main mistake</th><th>Guide them</th></tr></thead>
            <tbody>
              {agents.map((a) => (
                <Fragment key={a.agent}>
                  <tr>
                    <td>
                      <Button size="sm" variant="ghost" aria-expanded={open === a.agent}
                              onClick={() => setOpen(open === a.agent ? null : a.agent)}>{a.agent}</Button>
                    </td>
                    <td>{a.raised}</td>
                    <td>{a.wrong}</td>
                    <td>{a.check.length}</td>
                    <td>{a.main_mistake}</td>
                    <td>{a.message && <CopyButton text={a.message} label="Copy message" />}</td>
                  </tr>
                  {open === a.agent && <tr><td colSpan={6}><Detail a={a} onOpen={onOpen} /></td></tr>}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
