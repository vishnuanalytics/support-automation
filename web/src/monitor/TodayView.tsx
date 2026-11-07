import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, Skeleton, Tag } from "../ui";
import { copyText } from "./util";
import type { CoachAgent, Today, TodayCard } from "./types";

const FIRST = 7;

function CopyButton({ text, label, primary }: { text: string; label: string; primary?: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void copyText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <Button size="sm" variant={primary ? "primary" : "secondary"} onClick={copy}>
      {copied ? "Copied" : label}
    </Button>
  );
}

function Card({ card, onDone, onOpen, onTab }: {
  card: TodayCard;
  onDone: () => void;
  onOpen: (id: string) => void;
  onTab: (tab: string) => void;
}) {
  const [showDraft, setShowDraft] = useState(false);
  return (
    <li className="today-card">
      <div className="today-card-chip"><Tag tone={card.tone}>{card.chip}</Tag></div>
      <div className="today-card-body">
        <h3>{card.title}</h3>
        {card.why && <p>{card.why}</p>}
        {card.meta && <p className="today-meta">{card.meta}</p>}
        {showDraft && card.draft && (
          <div className="today-draft">
            {!!card.to?.length && <p className="today-meta">To: {card.to.join(", ")}</p>}
            {!!card.cc?.length && <p className="today-meta">Cc: {card.cc.join(", ")}</p>}
            {!!card.contacts?.length && <p className="today-meta">Contacts: {card.contacts.join(", ")}</p>}
            <pre>{card.draft}</pre>
          </div>
        )}
        <div className="today-card-actions">
          {card.draft && <CopyButton primary text={card.draft} label={`Copy ${card.draft_label?.toLowerCase()}`} />}
          {card.draft && (
            <Button size="sm" variant="ghost" onClick={() => setShowDraft(!showDraft)}>
              {showDraft ? "Hide" : "Preview"}
            </Button>
          )}
          {card.kind !== "escalate" && card.case_id && (
            <Button size="sm" variant={card.draft ? "ghost" : "primary"} onClick={() => onOpen(card.case_id as string)}>
              Open case
            </Button>
          )}
          {card.url && (
            <a className="today-link" href={card.url} target="_blank" rel="noreferrer">{card.url_label}</a>
          )}
          {card.tab && <Button size="sm" variant="primary" onClick={() => onTab(card.tab as string)}>Open messages</Button>}
          <span className="today-spacer" />
          <Button size="sm" variant="ghost" onClick={onDone}>Done</Button>
        </div>
      </div>
    </li>
  );
}

function Coaching({ data }: { data: Today["coaching"] }) {
  if (!data.closed) return null;
  return (
    <section className="today-section">
      <h2>Raised without needing an engineer</h2>
      <p className="today-meta">
        {data.wrong} of {data.closed} issues closed in the last {data.days} days
        {data.reasons.length > 0 && ` (${data.reasons.map(([r, n]) => `${r}: ${n}`).join(", ")})`}.
        These are the labels engineers used, so talk it through rather than treating it as a score.
      </p>
      <ul className="today-coach">
        {data.agents.slice(0, 6).map((a: CoachAgent) => (
          <li key={a.agent}>
            <span><strong>{a.agent}</strong> {a.wrong} of {a.raised}</span>
            <CopyButton text={a.message} label="Copy coaching note" />
          </li>
        ))}
      </ul>
    </section>
  );
}

export function TodayView({ tenantId, onOpen, onTab }: {
  tenantId?: string;
  onOpen: (id: string) => void;
  onTab: (tab: string) => void;
}) {
  const [data, setData] = useState<Today | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [all, setAll] = useState(false);

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

  const shown = all ? data.cards : data.cards.slice(0, FIRST);
  const n = data.cards.length;
  return (
    <div className="today">
      <header className="today-hero">
        <h2>{n ? `${n} thing${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} you` : "Nothing needs you right now"}</h2>
        <p>
          {data.clear} of {data.open_cases} open cases need nothing from you
          {data.done > 0 && `, and you've cleared ${data.done} today`}.
        </p>
        {data.help.away.total > 0 && (
          <p>
            While you were away, agents raised {data.help.away.total} help request{data.help.away.total === 1 ? "" : "s"},
            and {data.help.away.answered} {data.help.away.answered === 1 ? "was" : "were"} answered.
          </p>
        )}
      </header>

      <dl className="today-stats">
        <div>
          <dt>GitHub</dt>
          <dd>{data.github.with_tech}</dd>
          <p>of {data.github.open} open are with engineers</p>
        </div>
        <div>
          <dt>Aggregators</dt>
          <dd>{data.dsp.breached}</dd>
          <p>of {data.dsp.total} are past SLA</p>
        </div>
        <div>
          <dt>Raised wrongly</dt>
          <dd>{data.coaching.share}%</dd>
          <p>of issues closed this month</p>
        </div>
      </dl>

      {n > 0 && (
        <ol className="today-list">
          {shown.map((c) => (
            <Card key={c.key} card={c} onOpen={onOpen} onTab={onTab} onDone={() => done(c.key)} />
          ))}
        </ol>
      )}
      {!all && n > FIRST && (
        <Button size="sm" variant="secondary" onClick={() => setAll(true)}>Show {n - FIRST} more</Button>
      )}

      <Coaching data={data.coaching} />
      {data.help.modules.length > 0 && (
        <section className="today-section">
          <h2>What agents ask about most</h2>
          <p className="today-meta">
            Help requests this month: {data.help.modules.map(([m, c]) => `${m} (${c})`).join(", ")}.
            These are the topics most worth training on.
          </p>
        </section>
      )}
    </div>
  );
}
