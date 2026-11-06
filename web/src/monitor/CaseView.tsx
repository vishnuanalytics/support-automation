import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, Input, Skeleton, Tag, Textarea, type TagTone } from "../ui";
import { copyText, formatTime } from "./util";
import type { AiDraft, CaseDetail, Source, Summary, TimelineItem } from "./types";

const STALE_MS = 15 * 60 * 1000;
const LONG_MESSAGE = 420;
const LEVEL_TONE: Record<string, TagTone> = {
  critical: "exception", high: "warn", medium: "warn-soft", low: "neutral",
};

type Pane = "conversation" | "linked" | "past" | "related";
type Channel = "all" | "email" | "github" | "slack" | "notes";

function channelOf(t: TimelineItem): Channel {
  if (t.source === "GitHub") return "github";
  if (t.source === "Slack") return "slack";
  return t.source.includes("mail") ? "email" : "notes";
}

/** A source link. The full field name is on hover, so it stays out of the text. */
function Cite({ source }: { source: Source }) {
  const short = source.label.startsWith("Salesforce") ? "Salesforce" : source.label;
  return source.url
    ? <a className="monitor-cite" href={source.url} title={source.label} target="_blank" rel="noreferrer">{short}</a>
    : <span className="monitor-cite" title={source.label}>{short}</span>;
}

function Message({ item }: { item: TimelineItem }) {
  const [open, setOpen] = useState(false);
  const long = item.text.length > LONG_MESSAGE;
  const ours = item.party === "us";
  return (
    <li className={`monitor-msg${ours ? " is-ours" : ""}`}>
      <div className="monitor-msg-head">
        <Tag tone={ours ? "neutral" : "warn-soft"}>{item.party_label}</Tag>
        <span className="monitor-msg-who">{item.who}</span>
        <span className="monitor-muted">{formatTime(item.at)}</span>
        <Cite source={{ label: item.source, url: item.url }} />
      </div>
      {item.subject && <div className="monitor-msg-subject">{item.subject}</div>}
      <p className={`monitor-msg-body${long && !open ? " is-clamped" : ""}`}>{item.text}</p>
      {long && (
        <button type="button" className="monitor-more" onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </li>
  );
}

function Conversation({ items }: { items: TimelineItem[] }) {
  const [channel, setChannel] = useState<Channel>("all");
  const [oldest, setOldest] = useState(false);
  const [changes, setChanges] = useState(false);
  const talk = useMemo(() => items.filter((t) => t.party !== "system"), [items]);
  const changeCount = items.length - talk.length;
  const shown = useMemo(() => {
    const rows = items.filter((t) => (t.party === "system" ? changes : channel === "all" || channelOf(t) === channel));
    return oldest ? rows : [...rows].reverse();
  }, [items, channel, oldest, changes]);
  const options: [Channel, string][] = [["all", "All"], ["email", "Email"], ["github", "GitHub"],
    ["slack", "Slack"], ["notes", "Notes"]];
  return (
    <>
      <div className="monitor-msg-bar">
        <div className="monitor-switch" role="group" aria-label="Filter by source">
          {options.map(([key, label]) => {
            const n = key === "all" ? talk.length : talk.filter((t) => channelOf(t) === key).length;
            if (!n) return null;
            return (
              <button key={key} type="button" aria-pressed={channel === key}
                      className={channel === key ? "is-active" : ""} onClick={() => setChannel(key)}>
                {label} {n}
              </button>
            );
          })}
        </div>
        <div className="monitor-switch">
          <button type="button" onClick={() => setOldest(!oldest)}>
            {oldest ? "Oldest first" : "Newest first"}
          </button>
        </div>
        {changeCount > 0 && (
          <label className="monitor-check">
            <input type="checkbox" checked={changes} onChange={(e) => setChanges(e.target.checked)} />
            Field changes ({changeCount})
          </label>
        )}
      </div>
      {shown.length === 0 && <p className="monitor-muted">Nothing recorded yet.</p>}
      <ol className="monitor-msgs">
        {shown.map((t, i) => (
          t.party === "system" ? (
            <li key={i} className="monitor-msg-change">
              {t.text} · {t.who} · {formatTime(t.at)}
            </li>
          ) : <Message key={`${t.at}-${i}`} item={t} />
        ))}
      </ol>
    </>
  );
}

export function CaseView({ caseId, tenantId, slackConnected, onBack, onReviewed, onConnectSlack }: {
  caseId: string;
  tenantId?: string;
  slackConnected: boolean;
  onBack: () => void;
  onReviewed: () => void;
  onConnectSlack: () => void;
}) {
  const [data, setData] = useState<CaseDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [snooze, setSnooze] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [tagText, setTagText] = useState<string | null>(null);
  const [ai, setAi] = useState<AiDraft | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pane, setPane] = useState<Pane>("conversation");

  const load = useCallback(() => {
    return api.monitor.caseDetail(caseId, tenantId).then((d) => { setData(d); return d; });
  }, [caseId, tenantId]);

  // Show what's stored straight away, then refresh a stale timeline behind it.
  useEffect(() => {
    let live = true;
    load().then((d) => {
      const age = d.timeline_at ? Date.now() - new Date(d.timeline_at).getTime() : Infinity;
      if (d.source !== "salesforce" || age < STALE_MS) return;
      api.monitor.refreshCase(caseId, tenantId)
        .then((r) => { if (live && r.refreshed) void load(); })
        .catch(() => {});
    }).catch((e: ApiError) => setErr(e.message));
    return () => { live = false; };
  }, [caseId, tenantId, load]);

  const copy = useCallback(async (text: string, title: string) => {
    try {
      await copyText(text);
      setNote(`Copied "${title}"`);
    } catch {
      setErr("Could not copy to the clipboard");
    }
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
      if (e.metaKey || e.ctrlKey || e.altKey || !data) return;
      if (e.key === "c" && data.drafts[0]) void copy(data.drafts[0].text, data.drafts[0].title);
      else if (e.key === "o" && data.case.url) window.open(data.case.url, "_blank", "noopener");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [data, copy]);

  const review = async (body: { reviewed?: boolean; snooze_until?: string }, done: string) => {
    try {
      await api.monitor.review(caseId, body, tenantId);
      if (body.reviewed) onReviewed();
      else setNote(done);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };

  const summarize = async () => {
    setBusy(true);
    try {
      setSummary(await api.monitor.summary(caseId, tenantId));
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const writeDraft = async (audience: "merchant" | "aggregator") => {
    setAiBusy(true);
    setErr(null);
    try {
      setAi(await api.monitor.aiDraft(caseId, audience, tenantId));
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setAiBusy(false);
    }
  };

  const postTag = async () => {
    if (!data?.slack_threads[0] || !tagText) return;
    const { channel, thread_ts } = data.slack_threads[0];
    setBusy(true);
    try {
      await api.monitor.tagTeam(caseId, { channel, thread_ts, text: tagText }, tenantId);
      setNote("Posted to the Slack thread");
      setTagText(null);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  if (!data) {
    return (
      <>
        <Button size="sm" variant="ghost" onClick={onBack}>Back to queue</Button>
        {err ? <Banner tone="exception" title={err} /> : <Skeleton lines={8} />}
      </>
    );
  }

  const { brief, verdict, case: c } = data;
  const talk = data.timeline.filter((t) => t.party !== "system");
  const last = talk[talk.length - 1];
  const related = [...(c.parent ? [{ ...c.parent, rel: "Parent" }] : []),
    ...c.children.map((x) => ({ ...x, rel: "Child" }))];
  const stats = c.brand_stats;
  const panes: [Pane, string, number][] = [
    ["conversation", "Conversation", talk.length],
    ["linked", "Linked items", data.evidence.length],
    ["past", "Past cases", data.similar.length],
    ["related", "Related cases", related.length + data.duplicates.length],
  ];

  return (
    <>
      <div className="monitor-case-top">
        <Button size="sm" variant="ghost" onClick={onBack}>Back to queue</Button>
        <div className="monitor-actions">
          {c.url && <a className="ui-btn ui-btn--secondary ui-btn--sm" href={c.url} target="_blank" rel="noreferrer">Open in Salesforce</a>}
          <Button size="sm" onClick={() => review({ reviewed: true }, "Marked reviewed")}>Mark reviewed</Button>
          <Input type="datetime-local" aria-label="Snooze until" value={snooze}
                 onChange={(e) => setSnooze(e.target.value)} />
          <Button size="sm" disabled={!snooze}
                  onClick={() => review({ snooze_until: new Date(snooze).toISOString() }, "Snoozed")}>
            Snooze
          </Button>
        </div>
      </div>
      {err && <Banner tone="exception" title={err} />}
      {note && <p className="monitor-note" role="status">{note}</p>}

      <section className="monitor-case-head" aria-label="Case brief">
        <div>
          <p className="monitor-muted">Case {c.case_number || c.id}</p>
          <h1>{brief.problem.text}</h1>
          <div className="monitor-flags">
            <Tag tone={LEVEL_TONE[verdict.level]}>{verdict.level}</Tag>
            {brief.ball.mismatch && <Tag tone="warn">Salesforce status out of date</Tag>}
          </div>
        </div>
        <div className="monitor-strip">
          <div>
            <h2>Whose turn</h2>
            <strong>{brief.ball.text}</strong>
            {brief.waiting && <p className="monitor-muted">{brief.waiting.text}</p>}
          </div>
          <div className="monitor-strip-next">
            <h2>Next step</h2>
            <strong>{brief.next_action}</strong>
          </div>
          <div>
            <h2>Last response</h2>
            {last ? (
              <>
                <strong>{last.party_label}</strong>
                <p className="monitor-muted">{last.who} · {formatTime(last.at)}</p>
              </>
            ) : <p className="monitor-muted">None found</p>}
          </div>
        </div>
      </section>

      <div className="monitor-columns">
        <div>
          <div className="monitor-panes" role="tablist" aria-label="Case sections">
            {panes.map(([key, label, n]) => (
              <button key={key} type="button" role="tab" aria-selected={pane === key}
                      className={pane === key ? "is-active" : ""} onClick={() => setPane(key)}>
                {label} <span>{n}</span>
              </button>
            ))}
          </div>

          {pane === "conversation" && <Conversation items={data.timeline} />}

          {pane === "linked" && (
            <>
              {data.evidence.length === 0 && <p className="monitor-muted">No linked items.</p>}
              {data.evidence.map((e) => (
                <article key={e.url} className="monitor-card">
                  <h3>{e.label || e.kind} {e.state && <Tag>{e.state}</Tag>}</h3>
                  {e.title && <p>{e.title}</p>}
                  {e.error && <Banner tone="warn" title={e.error} />}
                  {e.note && <p className="monitor-muted">{e.note}</p>}
                  {e.url.startsWith("http") && <a href={e.url} target="_blank" rel="noreferrer">Open</a>}
                </article>
              ))}
            </>
          )}

          {pane === "past" && (
            <>
              {data.similar.length === 0 && <p className="monitor-muted">None found.</p>}
              {data.similar.map((s) => (
                <article key={s.id} className="monitor-card">
                  <h3>
                    {s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.case_number}</a> : s.case_number}
                    {" "}{s.subject}
                  </h3>
                  <p className="monitor-muted">
                    {s.match_count} matching field{s.match_count === 1 ? "" : "s"}
                    {s.hours_to_resolve != null && ` · resolved in ${s.hours_to_resolve}h`}
                  </p>
                  {s.resolution_note && <p>{s.resolution_note}</p>}
                </article>
              ))}
            </>
          )}

          {pane === "related" && (
            <>
              {related.length + data.duplicates.length === 0 && <p className="monitor-muted">No related cases.</p>}
              {related.length > 0 && (
                <ul>
                  {related.map((r) => (
                    <li key={r.id}>
                      {r.rel}{r.record_type && ` (${r.record_type})`}:{" "}
                      {r.url ? <a href={r.url} target="_blank" rel="noreferrer">{r.case_number || r.id}</a>
                        : r.case_number || r.id}
                      {r.status && ` · ${r.status}`}
                    </li>
                  ))}
                </ul>
              )}
              {data.duplicates.length > 0 && (
                <>
                  <h3>Same GitHub issue, other open cases</h3>
                  <ul>
                    {data.duplicates.map((d) => (
                      <li key={d.id}>{d.case_number}: {d.subject}{d.owner && ` · ${d.owner}`}</li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </div>

        <aside>
          <details className="monitor-card" open>
            <summary>Case details</summary>
            <dl className="monitor-details">
              {brief.facts.map((f) => (
                <div key={f.label}><dt>{f.label}</dt><dd>{f.value}</dd></div>
              ))}
              {stats && (
                <div>
                  <dt>This brand</dt>
                  <dd>{stats.open ?? 0} open · {stats.last_30_days ?? 0} in 30 days</dd>
                </div>
              )}
            </dl>
            <p className="monitor-cite">From Salesforce</p>
          </details>

          <details className="monitor-card">
            <summary>Why it's flagged ({verdict.reasons.length})</summary>
            <ul className="monitor-reasons-list">
              {verdict.reasons.map((r) => (
                <li key={r.code + r.text}>{r.text} <Cite source={r.source} /></li>
              ))}
            </ul>
          </details>

          {data.partner && (
            <details className="monitor-card">
              <summary>How to raise: {data.partner.name}</summary>
              <dl className="monitor-details">
                <div><dt>Channel</dt><dd>{data.partner.channel}</dd></div>
                {data.partner.to_emails.length > 0 && <div><dt>To</dt><dd>{data.partner.to_emails.join(", ")}</dd></div>}
                {data.partner.cc_emails.length > 0 && <div><dt>Cc</dt><dd>{data.partner.cc_emails.join(", ")}</dd></div>}
                {data.partner.contacts.length > 0 && <div><dt>Tag</dt><dd>{data.partner.contacts.join(", ")}</dd></div>}
              </dl>
              {data.partner.portal_url && (
                <p><a href={data.partner.portal_url} target="_blank" rel="noreferrer">Open portal</a></p>
              )}
              {data.partner.notes && <p className="monitor-muted">{data.partner.notes}</p>}
              {data.partner.escalation_note && <p className="monitor-muted">{data.partner.escalation_note}</p>}
              <p className="monitor-cite">From the aggregator directory</p>
            </details>
          )}

          {data.llm_enabled && (
            <article className="monitor-card">
              <h3>AI help</h3>
              <div className="monitor-actions">
                <Button size="sm" loading={aiBusy} onClick={() => writeDraft("merchant")}>
                  Draft for {data.labels.customer.toLowerCase()}
                </Button>
                <Button size="sm" loading={aiBusy} onClick={() => writeDraft("aggregator")}>
                  Draft for {data.labels.third_party.toLowerCase()}
                </Button>
                <Button size="sm" variant="ghost" loading={busy} onClick={summarize}>Summarise</Button>
              </div>
              {summary?.text && (
                <div className="monitor-ai">
                  <Tag tone="warn-soft">AI-assisted, verify</Tag>
                  <p>{summary.text}</p>
                </div>
              )}
              {ai?.error && <Banner tone="warn" title={ai.error} />}
              {ai?.draft && (
                <div className="monitor-ai">
                  <Tag tone="warn-soft">AI-assisted, verify</Tag>
                  {ai.situation && <p>{ai.situation}</p>}
                  <pre>{ai.draft}</pre>
                  {ai.missing && ai.missing.length > 0 && (
                    <>
                      <p className="monitor-muted">Fill in before sending:</p>
                      <ul>{ai.missing.map((m) => <li key={m}>{m}</li>)}</ul>
                    </>
                  )}
                  <Button size="sm" onClick={() => copy(ai.draft!, "AI draft")}>Copy</Button>
                  <details>
                    <summary>Sources used ({ai.sources?.length ?? 0}, {ai.similar_used ?? 0} past)</summary>
                    <ol>
                      {ai.sources?.map((s) => (
                        <li key={s.n}><Cite source={{ label: s.source, url: s.url?.startsWith("http") ? s.url : null }} /></li>
                      ))}
                    </ol>
                  </details>
                </div>
              )}
            </article>
          )}

          {data.drafts.map((d, i) => (
            <details key={d.id} className="monitor-card" open={i === 0}>
              <summary>
                {d.title} {i === 0 && <Tag>Suggested</Tag>}
                {d.review_required && <Tag tone="warn-soft">Review first</Tag>}
              </summary>
              {d.to && d.to.length > 0 && <p className="monitor-muted">To: {d.to.join(", ")}</p>}
              {d.cc && d.cc.length > 0 && <p className="monitor-muted">Cc: {d.cc.join(", ")}</p>}
              <pre>{d.text}</pre>
              <Button size="sm" onClick={() => copy(d.text, d.title)}>Copy</Button>
              <details>
                <summary>Facts used</summary>
                <ul>
                  {d.facts.map((f) => (
                    <li key={f.label}>{f.label}: {f.value} <Cite source={f.source} /></li>
                  ))}
                </ul>
              </details>
            </details>
          ))}

          {data.slack_threads.length > 0 && (
            <article className="monitor-card">
              <h3>Tag the team</h3>
              {data.slack_via_claude ? (
                <p className="monitor-muted">
                  Slack is read through Claude. Say "refresh Slack" in a Claude session to update
                  this thread, or ask Claude to post a message.
                </p>
              ) : !slackConnected ? (
                <Button size="sm" onClick={onConnectSlack}>Connect Slack</Button>
              ) : tagText === null ? (
                <Button size="sm" onClick={() => setTagText(
                  `${data.slack_team_mention} please take a look: case ${c.case_number || c.id}`.trim(),
                )}>
                  Write a message
                </Button>
              ) : (
                <>
                  <p className="monitor-muted">
                    Posts as you in this case's Slack thread. Everyone in the channel sees it,
                    including external members.
                  </p>
                  <Textarea aria-label="Message" rows={3} value={tagText}
                            onChange={(e) => setTagText(e.target.value)} />
                  <div className="monitor-actions">
                    <Button size="sm" variant="primary" loading={busy} onClick={postTag}>Post to Slack</Button>
                    <Button size="sm" variant="ghost" onClick={() => setTagText(null)}>Cancel</Button>
                  </div>
                </>
              )}
            </article>
          )}
        </aside>
      </div>
    </>
  );
}
