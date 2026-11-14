import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, Skeleton, Tag } from "../ui";
import { CopyButton } from "./CopyButton";
import { LastResponse } from "./LastResponse";
import { isWaiting, type Marks } from "./marks";
import { daysSince, QUIET_DAYS, replyTone, toneLabel, toneTag, withoutMirrors } from "./tone";
import { copyText, formatDay } from "./util";
import type { Escalation, Partner, QueueCase, TechIssue } from "./types";

const AGENT_STAGES: Record<string, string> = { act: "Reply or act", close: "Update and close", raise: "Raise or add the link" };
const NO_RCA = ["no_reply", "investigating", "not_read"];

type Labels = { thirdParty: string; customer: string };

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function groupBy<T>(items: T[], key: (t: T) => string) {
  const out = new Map<string, T[]>();
  for (const t of items) out.set(key(t), [...(out.get(key(t)) ?? []), t]);
  return [...out].sort((a, b) => b[1].length - a[1].length);
}

function aggregatorChase(name: string, rows: Escalation[]) {
  return [`Hi ${name} team,`, "",
    `We haven't had an update on these ${rows.length === 1 ? "case" : `${rows.length} cases`} for ${QUIET_DAYS} days or more. `
      + "Could you please share the current status and an ETA?", "",
    ...rows.map((e) => `• ${e.subject ?? "(no subject)"} (our ref ${e.case_number})`
      + `${e.business ? `, ${e.business}${e.biz_id ? ` ${e.biz_id}` : ""}` : ""}: no update for ${e.days_silent} days`),
    "", "Thanks"].join("\n");
}

function engineeringChase(squad: string, rows: { r: TechIssue; days: number }[]) {
  return [`Hi ${squad},`, "",
    `These issues have had no reply for ${QUIET_DAYS}+ days and no RCA yet. Please share the cause and an ETA for the fix:`,
    ...rows.map(({ r, days }) => `• #${r.issue} ${r.title ?? ""} (${Math.floor(days)}d quiet${r.case_number ? `, case ${r.case_number}` : ""}) ${r.url}`),
  ].join("\n");
}

function agentMessage(owner: string, cases: QueueCase[]) {
  return [`Hi ${owner.split(" ")[0]}, please go through these cases today:`,
    ...cases.map((c) => `• ${c.case_number}: ${c.subject ?? ""}. ${c.next_action}`)].join("\n");
}

function Section({ id, title, hint, count, children }: {
  id: string; title: string; hint: string; count: number; children: ReactNode;
}) {
  return (
    <section id={id} className="todo-section">
      <header>
        <h2>{title} <span className="todo-count">{count}</span></h2>
        <p className="monitor-muted">{hint}</p>
      </header>
      {count === 0 ? <p className="todo-clear">Nothing here.</p> : children}
    </section>
  );
}

function CaseLink({ id, number, onOpen }: { id: string; number: string | null; onOpen: (id: string) => void }) {
  return <button type="button" className="monitor-link todo-case" onClick={() => onOpen(id)}>{number ?? "Case"}</button>;
}

/** Copies the case's own suggested draft, built by the sync. */
function DraftButton({ id, tenantId, label = "Copy draft" }: { id: string; tenantId?: string; label?: string }) {
  const [state, setState] = useState<"idle" | "busy" | "done" | "none">("idle");
  const copy = async () => {
    setState("busy");
    try {
      const d = await api.monitor.caseDetail(id, tenantId);
      if (!d.drafts.length) return setState("none");
      await copyText(d.drafts[0].text);
      setState("done");
    } catch {
      setState("none");
    }
    setTimeout(() => setState("idle"), 1500);
  };
  return (
    <Button size="sm" variant="secondary" loading={state === "busy"} onClick={copy}>
      {state === "done" ? "Copied" : state === "none" ? "No draft" : label}
    </Button>
  );
}

/** One chase message for every quiet case with one aggregator, with the recipients from the aggregator directory. */
function AggregatorGroup({ name, rows, marks, tenantId, onOpen }: {
  name: string; rows: Escalation[]; marks: Marks; tenantId?: string; onOpen: (id: string) => void;
}) {
  const [partner, setPartner] = useState<Partner | null | undefined>(undefined);
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await copyText(aggregatorChase(name, rows));
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
    if (partner === undefined) {
      api.monitor.caseDetail(rows[0].id, tenantId).then((d) => setPartner(d.partner)).catch(() => setPartner(null));
    }
  };
  return (
    <article className="todo-group">
      <div className="todo-group-head">
        <div>
          <strong>{name}</strong> <span className="monitor-muted">{plural(rows.length, "case")} · longest {rows[0].days_silent}d</span>
          {partner && (partner.to_emails.length > 0 || partner.portal_url) && (
            <div className="todo-recipients">
              {partner.to_emails.length > 0 && <>To: {partner.to_emails.join(", ")}</>}
              {partner.cc_emails.length > 0 && <> · Cc: {partner.cc_emails.join(", ")}</>}
              {partner.portal_url && <> · <a href={partner.portal_url} target="_blank" rel="noreferrer">Portal</a></>}
            </div>
          )}
        </div>
        <div className="todo-actions">
          <Button size="sm" variant="primary" onClick={copy}>{copied ? "Copied" : `Copy chase for all ${rows.length}`}</Button>
          <Button size="sm" variant="secondary" onClick={() => Promise.all(rows.map((e) => marks.act(e.id, "escalate")))}>
            Mark all escalated
          </Button>
        </div>
      </div>
      <ul className="todo-rows">
        {rows.map((e) => (
          <li key={e.id}>
            <CaseLink id={e.id} number={e.case_number} onOpen={onOpen} />
            <span className="todo-subject">{e.subject}{e.business && <span className="monitor-muted"> · {e.business}</span>}</span>
            <Tag tone={e.days_silent >= 7 ? "exception" : "warn"}>{e.days_silent}d quiet</Tag>
            <span className="monitor-muted todo-meta">
              {e.last_update_at ? `Their last update ${formatDay(e.last_update_at)}` : "No update yet"}
              {e.our_follow_ups > 0 && ` · ${plural(e.our_follow_ups, "chase")} sent`} · {e.owner}
            </span>
            <Button size="sm" variant="ghost" onClick={() => marks.act(e.id, "escalate")}>Escalated</Button>
          </li>
        ))}
      </ul>
    </article>
  );
}

export function TodoView({ cases, tenantId, onOpen, labels, marks }: {
  cases: QueueCase[]; tenantId?: string; onOpen: (id: string) => void; labels: Labels; marks: Marks;
}) {
  const [quiet, setQuiet] = useState<Escalation[] | null>(null);
  const [issues, setIssues] = useState<TechIssue[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [openAgent, setOpenAgent] = useState<string | null>(null);

  useEffect(() => {
    api.monitor.escalations(tenantId, QUIET_DAYS).then((d) => setQuiet(d.cases)).catch((e: ApiError) => setErr(e.message));
    api.monitor.techPending(tenantId).then((d) => setIssues(d.squads.flatMap((s) => s.issues)))
      .catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);

  const open = useMemo(() => cases.filter((c) => !c.hidden), [cases]);
  const byId = useMemo(() => new Map(open.map((c) => [c.id, c])), [open]);

  const mine = open.filter((c) => c.mine && AGENT_STAGES[c.stage]).sort((a, b) => b.score - a.score);
  const chaseAgg = (quiet ?? []).filter((e) => !isWaiting(marks.marks.get(e.id)));
  const aggGroups = groupBy(chaseAgg, (e) => e.waiting_on[0] ?? labels.thirdParty);
  const chaseEng = (issues ?? [])
    .map((r) => ({ r, days: daysSince(r.last_activity_at ?? r.opened_at) ?? r.days_open }))
    .filter(({ r, days }) => NO_RCA.includes(r.status) && days >= QUIET_DAYS)
    .sort((a, b) => b.days - a.days);
  const engGroups = groupBy(chaseEng, ({ r }) => r.squad);
  const waiting = [...marks.marks.values()].filter((m) => isWaiting(m) && byId.has(m.case_id))
    .map((m) => {
      const c = byId.get(m.case_id)!;
      const replied = c.latest?.party === "third_party" && c.signal?.code !== "third_party_ack"
        && new Date(c.latest.at) > new Date(m.escalated_at!);
      return { c, m, replied, days: daysSince(m.escalated_at) ?? 0 };
    })
    .sort((a, b) => Number(b.replied) - Number(a.replied) || b.days - a.days);
  const agentCases = withoutMirrors(open.filter((c) => !c.mine && AGENT_STAGES[c.stage] && c.owner));
  const agents = groupBy(agentCases, (c) => c.owner!).map(([owner, list]) =>
    [owner, list.sort((a, b) => b.score - a.score)] as const);
  const repliedCount = waiting.filter((w) => w.replied).length;

  const jumps = [
    { id: "todo-mine", label: "Your cases", n: mine.length },
    { id: "todo-agg", label: `Chase ${labels.thirdParty.toLowerCase()}`, n: chaseAgg.length },
    { id: "todo-eng", label: "Chase engineering", n: chaseEng.length },
    { id: "todo-waiting", label: "Escalated, waiting", n: waiting.length, flag: repliedCount },
    { id: "todo-agents", label: "Agents' cases", n: agentCases.length },
  ];

  return (
    <div className="todo">
      {(err || marks.err) && <Banner tone="exception" title={err ?? marks.err!} />}
      <nav className="todo-jump" aria-label="To-do groups">
        {jumps.map((j) => (
          <a key={j.id} href={`#${j.id}`} className={j.n ? "" : "is-empty"}>
            <strong>{j.n}</strong>
            <span>{j.label}</span>
            {!!j.flag && <Tag tone="warn">{j.flag} replied</Tag>}
          </a>
        ))}
      </nav>

      <Section id="todo-mine" title="Your cases" count={mine.length}
               hint="Cases you own where someone replied, or that are ready to close or raise.">
        <ul className="todo-rows todo-rows--boxed">
          {mine.map((c) => {
            const tone = replyTone(c);
            return (
              <li key={c.id}>
                <CaseLink id={c.id} number={c.case_number} onOpen={onOpen} />
                <span className="todo-subject">{c.subject}</span>
                <Tag tone={c.stage === "close" ? "success" : "exception"}>{AGENT_STAGES[c.stage]}</Tag>
                <span className="todo-meta">
                  {tone && <><Tag tone={toneTag(tone)}>{toneLabel(tone)}</Tag>{" "}</>}
                  <LastResponse latest={c.latest} {...labels} brief />
                </span>
                <DraftButton id={c.id} tenantId={tenantId} />
              </li>
            );
          })}
        </ul>
      </Section>

      <Section id="todo-agg" title={`Chase ${labels.thirdParty.toLowerCase()}`} count={chaseAgg.length}
               hint={`No real update for ${QUIET_DAYS}+ days on any channel (acks don't count). Copy one message per ${labels.thirdParty.toLowerCase()}, send it, then mark escalated: they move to Escalated, waiting.`}>
        {quiet === null ? <Skeleton variant="row" lines={3} /> : aggGroups.map(([name, rows]) => (
          <AggregatorGroup key={name} name={name} rows={rows} marks={marks} tenantId={tenantId} onOpen={onOpen} />
        ))}
      </Section>

      <Section id="todo-eng" title="Chase engineering" count={chaseEng.length}
               hint={`GitHub issues with no comment for ${QUIET_DAYS}+ days and no RCA shared. A comment on the issue takes it off this list for ${QUIET_DAYS} days.`}>
        {issues === null ? <Skeleton variant="row" lines={3} /> : engGroups.map(([squad, rows]) => (
          <article key={squad} className="todo-group">
            <div className="todo-group-head">
              <div><strong>{squad}</strong> <span className="monitor-muted">{plural(rows.length, "issue")}</span></div>
              <div className="todo-actions"><CopyButton primary text={engineeringChase(squad, rows)} label={`Copy chase for all ${rows.length}`} /></div>
            </div>
            <ul className="todo-rows">
              {rows.map(({ r, days }) => (
                <li key={r.url}>
                  <a className="monitor-link todo-case" href={r.url} target="_blank" rel="noreferrer">#{r.issue}</a>
                  <span className="todo-subject">{r.title}</span>
                  <Tag tone={days >= 7 ? "exception" : "warn"}>{Math.floor(days)}d quiet</Tag>
                  <span className="monitor-muted todo-meta">
                    {r.tech_status} · last reply {r.last_reply_by ?? "none"}
                    {r.case_id && <> · <button type="button" className="monitor-link" onClick={() => onOpen(r.case_id!)}>{r.case_number}</button></>}
                  </span>
                  <CopyButton text={`Hi team, any update on this? No reply for ${Math.floor(days)} days. Please share the RCA and an ETA for the fix.`} label="Copy nudge" />
                </li>
              ))}
            </ul>
          </article>
        ))}
      </Section>

      <Section id="todo-waiting" title="Escalated, waiting on them" count={waiting.length}
               hint={`Cases you marked escalated. When their team answers, press They responded. After ${QUIET_DAYS} days with no answer, chase again.`}>
        <ul className="todo-rows todo-rows--boxed">
          {waiting.map(({ c, m, replied, days }) => (
            <li key={c.id} className={replied ? "is-flag" : ""}>
              <CaseLink id={c.id} number={c.case_number} onOpen={onOpen} />
              <span className="todo-subject">
                {c.subject}
                {m.note && <span className="monitor-muted"> · {m.note}</span>}
              </span>
              {replied ? <Tag tone="warn">{labels.thirdParty} replied</Tag>
                : <Tag tone={days >= QUIET_DAYS ? "exception" : "neutral"}>Escalated {Math.floor(days)}d ago</Tag>}
              <span className="todo-meta"><LastResponse latest={c.latest} {...labels} brief /></span>
              <span className="todo-actions">
                <Button size="sm" variant={replied ? "primary" : "secondary"} onClick={() => marks.act(c.id, "responded")}>
                  They responded
                </Button>
                {!replied && days >= QUIET_DAYS && (
                  <Button size="sm" variant="ghost" onClick={() => marks.act(c.id, "escalate", m.note ?? undefined)}>Chased again</Button>
                )}
              </span>
            </li>
          ))}
        </ul>
      </Section>

      <Section id="todo-agents" title="Agents' cases" count={agentCases.length}
               hint="Agent-owned cases that need a reply, a close or a raise. Copy the message and send it to the agent yourself.">
        {agents.map(([owner, list]) => {
          const expanded = openAgent === owner;
          const overdue = list.filter((c) => c.overdue).length;
          return (
            <article key={owner} className="todo-group">
              <div className="todo-group-head">
                <button type="button" className="todo-agent" aria-expanded={expanded} onClick={() => setOpenAgent(expanded ? null : owner)}>
                  <span aria-hidden>{expanded ? "▾" : "▸"}</span> <strong>{owner}</strong>{" "}
                  <span className="monitor-muted">{plural(list.length, "case")}</span>
                  {overdue > 0 && <> <Tag tone="exception">{overdue} overdue</Tag></>}
                </button>
                <div className="todo-actions"><CopyButton text={agentMessage(owner, list)} label="Copy message to agent" /></div>
              </div>
              {expanded && (
                <ul className="todo-rows">
                  {list.map((c) => (
                    <li key={c.id}>
                      <CaseLink id={c.id} number={c.case_number} onOpen={onOpen} />
                      <span className="todo-subject">{c.subject}</span>
                      <Tag tone={c.stage === "close" ? "success" : c.stage === "act" ? "exception" : "warn"}>{AGENT_STAGES[c.stage]}</Tag>
                      <span className="monitor-muted todo-meta">{c.next_action}</span>
                    </li>
                  ))}
                </ul>
              )}
            </article>
          );
        })}
      </Section>
    </div>
  );
}
