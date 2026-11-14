import { useMemo, useState } from "react";
import { EmptyState, Segmented, Tag, type TagTone } from "../ui";
import { AgentsView } from "./AgentsView";
import { CopyButton } from "./CopyButton";
import { LastResponse } from "./LastResponse";
import { TodayView } from "./TodayView";
import { replyTone, toneLabel, toneTag, withoutMirrors } from "./tone";
import { formatAge } from "./util";
import type { QueueCase } from "./types";

// Stages where the case owner has to do something.
const AGENT_STAGES: Record<string, { label: string; tone: TagTone }> = {
  act: { label: "Reply or act", tone: "exception" },
  close: { label: "Update and close", tone: "success" },
  raise: { label: "Raise or add the link", tone: "warn" },
};

function message(owner: string, cases: QueueCase[]) {
  const first = owner.split(" ")[0];
  return [`Hi ${first}, please go through these cases today:`,
    ...cases.map((c) => `• ${c.case_number}: ${c.subject ?? ""}. ${c.next_action}`)].join("\n");
}

function AgentCases({ cases, onOpen, thirdParty, customer }: {
  cases: QueueCase[]; onOpen: (id: string) => void; thirdParty: string; customer: string;
}) {
  const [stage, setStage] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const work = useMemo(() => withoutMirrors(cases.filter((c) => !c.mine && !c.hidden && AGENT_STAGES[c.stage] && c.owner)),
    [cases]);
  const filtered = work.filter((c) => !stage || c.stage === stage);
  const owners = useMemo(() => {
    const by = new Map<string, QueueCase[]>();
    for (const c of filtered) by.set(c.owner!, [...(by.get(c.owner!) ?? []), c]);
    return [...by].map(([owner, list]) => ({ owner, list: list.sort((a, b) => b.score - a.score) }))
      .sort((a, b) => b.list.length - a.list.length);
  }, [filtered]);

  return (
    <>
      <div className="page-stats">
        <button type="button" className={!stage ? "is-active" : ""} onClick={() => setStage("")}>
          <strong>{work.length}</strong>Cases agents must act on
        </button>
        {Object.entries(AGENT_STAGES).map(([k, s]) => (
          <button key={k} type="button" className={stage === k ? "is-active" : ""} aria-pressed={stage === k}
                  onClick={() => setStage(stage === k ? "" : k)}>
            <strong>{work.filter((c) => c.stage === k).length}</strong>{s.label}
          </button>
        ))}
      </div>
      {owners.length === 0 && <EmptyState title="Agents are clear" body="No agent-owned case needs action right now." />}
      <ul className="agent-work">
        {owners.map(({ owner, list }) => {
          const open = expanded === owner;
          const overdue = list.filter((c) => c.overdue).length;
          return (
            <li key={owner}>
              <div className="agent-work-head">
                <button type="button" className="agent-work-name" aria-expanded={open}
                        onClick={() => setExpanded(open ? null : owner)}>
                  <span className="agent-work-caret" aria-hidden>{open ? "▾" : "▸"}</span>
                  <strong>{owner}</strong>
                  <span className="monitor-muted">{list.length} case{list.length === 1 ? "" : "s"}</span>
                  {overdue > 0 && <Tag tone="exception">{overdue} overdue</Tag>}
                </button>
                <CopyButton text={message(owner, list)} label="Copy message to agent" />
              </div>
              {open && (
                <table className="monitor-esc">
                  <thead><tr><th>Case</th><th>What to do</th><th>Last reply</th><th>Age</th></tr></thead>
                  <tbody>
                    {list.map((c) => {
                      const tone = replyTone(c);
                      return (
                        <tr key={c.id}>
                          <td>
                            <button type="button" className="monitor-link" onClick={() => onOpen(c.id)}>{c.case_number}</button>
                            <div className="monitor-fu-subject">{c.subject}</div>
                          </td>
                          <td>
                            <Tag tone={AGENT_STAGES[c.stage].tone}>{AGENT_STAGES[c.stage].label}</Tag>
                            <div className="monitor-fu-subject">{c.next_action}</div>
                          </td>
                          <td>
                            {tone && <div><Tag tone={toneTag(tone)}>{toneLabel(tone)}</Tag></div>}
                            <LastResponse latest={c.latest} thirdParty={thirdParty} customer={customer} brief />
                          </td>
                          <td>{c.overdue ? <Tag tone="exception">Overdue {formatAge(c.overdue_hours)}</Tag> : "On time"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </li>
          );
        })}
      </ul>
    </>
  );
}

/** Cases waiting on you, and cases each agent has to work. */
export function OnMeView({ cases, tenantId, onOpen, thirdParty, customer }: {
  cases: QueueCase[]; tenantId?: string; onOpen: (id: string) => void; thirdParty: string; customer: string;
}) {
  const [tab, setTab] = useState<"me" | "agents" | "quality">("me");
  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>{tab === "me" ? "Waiting on you" : tab === "agents" ? "Cases agents need to fix" : "Raise quality by agent"}</h2>
          <p className="monitor-muted">
            {tab === "me" ? "Replies nobody has acted on, SLA chases and agents who asked for help."
              : tab === "agents" ? "Agent-owned cases that need a reply, a close or a raise. Copy a message and send it to the agent yourself."
                : "Wrongly raised or incomplete aggregator and engineering cases, by agent."}
          </p>
        </div>
        <Segmented aria-label="Whose cases" value={tab} onChange={setTab}
                   options={[{ value: "me", label: "On me" }, { value: "agents", label: "Agents' cases" },
                     { value: "quality", label: "Raise quality" }]} />
      </header>
      {tab === "me" && <TodayView tenantId={tenantId} onOpen={onOpen} thirdParty={thirdParty} customer={customer} />}
      {tab === "agents" && <AgentCases cases={cases} onOpen={onOpen} thirdParty={thirdParty} customer={customer} />}
      {tab === "quality" && <AgentsView tenantId={tenantId} onOpen={onOpen} />}
    </section>
  );
}
