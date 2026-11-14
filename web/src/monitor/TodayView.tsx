import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, Skeleton, Tag } from "../ui";
import { CopyButton } from "./CopyButton";
import { LastResponse } from "./LastResponse";
import type { Today, TodayCard } from "./types";

const GROUPS: { key: TodayCard["group"]; title: string; hint: string }[] = [
  { key: "replies", title: "Replies waiting", hint: "Someone answered on GitHub, Slack or email, and nobody on our side has acted." },
  { key: "sla", title: "No response past SLA", hint: "An aggregator or engineer has gone quiet." },
  { key: "agents", title: "Agents", hint: "An agent needs an answer, or a nudge." },
];

function Card({ card, labels, onDone, onOpen }: {
  card: TodayCard;
  labels: { thirdParty: string; customer: string };
  onDone: () => void;
  onOpen: (id: string) => void;
}) {
  const [showCases, setShowCases] = useState(false);
  const [showDraft, setShowDraft] = useState(false);
  const single = card.cases.length === 1 ? card.cases[0] : null;
  return (
    <li className="today-card">
      <div className="today-card-chip"><Tag tone={card.tone}>{card.chip}</Tag></div>
      <div className="today-card-body">
        <h3>{card.title}</h3>
        {card.why && <p>{card.why}</p>}
        {card.meta && <p className="today-meta">{card.meta}</p>}
        {single?.step && <p className="today-step">{single.step}</p>}
        {single && <LastResponse latest={single.latest} {...labels} />}
        {showDraft && card.draft && (
          <div className="today-draft">
            {!!card.to?.length && <p className="today-meta">To: {card.to.join(", ")}</p>}
            {!!card.cc?.length && <p className="today-meta">Cc: {card.cc.join(", ")}</p>}
            {!!card.contacts?.length && <p className="today-meta">Contacts: {card.contacts.join(", ")}</p>}
            <pre>{card.draft}</pre>
          </div>
        )}
        {showCases && (
          <ul className="today-cases">
            {card.cases.map((c) => (
              <li key={c.case_id}>
                <p className="today-case-title">
                  <button type="button" className="monitor-link" onClick={() => onOpen(c.case_id)}>{c.case_number}</button>{" "}
                  {c.subject}
                </p>
                {c.step && <p className="today-step">{c.step}</p>}
                <LastResponse latest={c.latest} {...labels} />
              </li>
            ))}
          </ul>
        )}
        <div className="today-card-actions">
          {card.draft && <CopyButton primary text={card.draft} label={`Copy ${card.draft_label?.toLowerCase()}`} />}
          {card.draft && (
            <Button size="sm" variant="ghost" onClick={() => setShowDraft(!showDraft)}>
              {showDraft ? "Hide message" : "Preview"}
            </Button>
          )}
          {card.cases.length > 1 && (
            <Button size="sm" variant="ghost" aria-expanded={showCases} onClick={() => setShowCases(!showCases)}>
              {showCases ? "Hide cases" : `Show ${card.cases.length} cases`}
            </Button>
          )}
          {single && (
            <Button size="sm" variant={card.draft ? "ghost" : "primary"} onClick={() => onOpen(single.case_id)}>
              Open case
            </Button>
          )}
          {card.url && <a className="today-link" href={card.url} target="_blank" rel="noreferrer">{card.url_label}</a>}
          <span className="today-spacer" />
          <Button size="sm" variant="ghost" onClick={onDone}>Done</Button>
        </div>
      </div>
    </li>
  );
}

export function TodayView({ tenantId, onOpen, thirdParty, customer }: {
  tenantId?: string;
  onOpen: (id: string) => void;
  thirdParty: string;
  customer: string;
}) {
  const [data, setData] = useState<Today | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    api.monitor.today(tenantId).then(setData).catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);
  useEffect(load, [load]);

  const done = (key: string) => {
    setData((d) => d && { ...d, cards: d.cards.filter((c) => c.key !== key), done: d.done + 1 });
    api.monitor.todayDone(key, tenantId).catch((e: ApiError) => { setErr(e.message); load(); });
  };

  if (err) return <Banner tone="exception" title={err} />;
  if (!data) return <Skeleton variant="row" lines={6} />;

  const n = data.cards.length;
  return (
    <div className="today">
      <header className="today-hero">
        <h2>{n ? `${n} thing${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} you` : "Nothing needs you right now"}</h2>
        <p>
          {data.clear} of {data.open_cases} open cases need nothing from you
          {data.done > 0 && `, and you've cleared ${data.done} today`}.
        </p>
      </header>
      {GROUPS.map((g) => {
        const cards = data.cards.filter((c) => c.group === g.key);
        if (!cards.length) return null;
        return (
          <section key={g.key} className="today-group">
            <h2>{g.title} ({cards.length})</h2>
            <p>{g.hint}</p>
            <ol className="today-list">
              {cards.map((c) => (
                <Card key={c.key} card={c} labels={{ thirdParty, customer }} onOpen={onOpen} onDone={() => done(c.key)} />
              ))}
            </ol>
          </section>
        );
      })}
    </div>
  );
}
