import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, Input, Skeleton, Tag, Textarea } from "../ui";
import { copyText, formatTime } from "./util";
import type { CaseDetail, Source, Summary } from "./types";

const STALE_MS = 15 * 60 * 1000;

function Cite({ source }: { source: Source }) {
  return source.url
    ? <a className="monitor-cite" href={source.url} target="_blank" rel="noreferrer">{source.label}</a>
    : <span className="monitor-cite">{source.label}</span>;
}

export function CaseView({ caseId, tenantId, slackConnected, onBack, onConnectSlack }: {
  caseId: string;
  tenantId?: string;
  slackConnected: boolean;
  onBack: () => void;
  onConnectSlack: () => void;
}) {
  const [data, setData] = useState<CaseDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [snooze, setSnooze] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [tagText, setTagText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
      setNote(done);
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
  const stats = c.brand_stats;
  return (
    <>
      <div className="monitor-actions">
        <Button size="sm" variant="ghost" onClick={onBack}>Back to queue</Button>
        {c.url && <a className="ui-btn ui-btn--secondary ui-btn--sm" href={c.url} target="_blank" rel="noreferrer">Open case</a>}
        <Button size="sm" onClick={() => review({ reviewed: true }, "Marked reviewed")}>Mark reviewed</Button>
        <Input type="datetime-local" aria-label="Snooze until" value={snooze}
               onChange={(e) => setSnooze(e.target.value)} />
        <Button size="sm" disabled={!snooze}
                onClick={() => review({ snooze_until: new Date(snooze).toISOString() }, "Snoozed")}>
          Snooze
        </Button>
      </div>
      {err && <Banner tone="exception" title={err} />}
      {note && <p className="monitor-note" role="status">{note}</p>}

      <section className="monitor-brief" aria-label="Case brief">
        <h1>{brief.problem.text}</h1>
        <p className="monitor-muted">Case {c.case_number || c.id} · <Cite source={brief.problem.source} /></p>
        <div className="monitor-turn">
          <strong>{brief.ball.text}</strong>
          {brief.ball.mismatch && <Tag tone="warn">Mismatch</Tag>}
          <Tag>{verdict.level} {verdict.score}</Tag>
        </div>
        {brief.waiting && (
          <p>
            {brief.waiting.text}{brief.waiting.since && ` since ${formatTime(brief.waiting.since)}`}{" "}
            <Cite source={brief.waiting.source} />
          </p>
        )}
        {brief.facts.length > 0 && (
          <dl className="monitor-facts">
            {brief.facts.map((f) => (
              <div key={f.label}><dt>{f.label}</dt><dd>{f.value} <Cite source={f.source} /></dd></div>
            ))}
          </dl>
        )}
        {stats && (
          <p className="monitor-muted">
            This brand: {stats.open ?? 0} open, {stats.last_30_days ?? 0} in the last 30 days
          </p>
        )}
        {brief.done_so_far.length > 0 && (
          <>
            <h2>Done so far</h2>
            <ul>
              {brief.done_so_far.map((d, i) => (
                <li key={i}>{d.text} <Cite source={d.source} /></li>
              ))}
            </ul>
          </>
        )}
        <p className="monitor-next"><strong>Next:</strong> {brief.next_action}</p>
        {data.llm_enabled && (
          <Button size="sm" loading={busy} onClick={summarize}>Summarise with AI</Button>
        )}
        {summary?.text && (
          <div className="monitor-summary">
            <Tag tone="warn-soft">AI-assisted, verify</Tag>
            <p>{summary.text}</p>
            <ol>
              {summary.sources?.map((s) => (
                <li key={s.n}><Cite source={{ label: s.source, url: s.url }} /></li>
              ))}
            </ol>
          </div>
        )}
      </section>

      <div className="monitor-columns">
        <div>
          <section>
            <h2>Why</h2>
            <ul>
              {verdict.reasons.map((r) => (
                <li key={r.code + r.text}>
                  {r.text}{r.points > 0 && ` (+${r.points})`} <Cite source={r.source} />
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h2>Timeline</h2>
            {data.timeline.length === 0 && <p className="monitor-muted">Nothing recorded yet.</p>}
            <ol className="monitor-timeline">
              {data.timeline.map((t, i) => (
                <li key={i}>
                  <div className="monitor-muted">
                    {formatTime(t.at)} · {t.party_label}{t.who && ` (${t.who})`} ·{" "}
                    <Cite source={{ label: t.source, url: t.url }} />
                  </div>
                  <p>{t.text}</p>
                </li>
              ))}
            </ol>
          </section>

          <section>
            <h2>Evidence</h2>
            {data.evidence.length === 0 && <p className="monitor-muted">No linked items.</p>}
            {data.evidence.map((e) => (
              <article key={e.url} className="monitor-card">
                <h3>{e.label || e.kind} {e.state && <Tag>{e.state}</Tag>}</h3>
                {e.title && <p>{e.title}</p>}
                {e.error && <Banner tone="warn" title={e.error} />}
                {e.note && <p className="monitor-muted">{e.note}</p>}
                {e.messages?.slice(-3).map((m, i) => (
                  <p key={i}><strong>{m.who}:</strong> {m.text}</p>
                ))}
                <a href={e.url} target="_blank" rel="noreferrer">Open</a>
              </article>
            ))}
          </section>

          {(c.parent || c.children.length > 0) && (
            <section>
              <h2>Related cases</h2>
              <ul>
                {[...(c.parent ? [{ ...c.parent, rel: "Parent" }] : []),
                  ...c.children.map((x) => ({ ...x, rel: "Child" }))].map((r) => (
                  <li key={r.id}>
                    {r.rel}{r.record_type && ` (${r.record_type})`}:{" "}
                    {r.url ? <a href={r.url} target="_blank" rel="noreferrer">{r.case_number || r.id}</a>
                      : r.case_number || r.id}
                    {r.status && ` · ${r.status}`}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {data.duplicates.length > 0 && (
            <section>
              <h2>Same issue, other open cases</h2>
              <ul>
                {data.duplicates.map((d) => (
                  <li key={d.id}>{d.case_number}: {d.subject}{d.owner && ` · ${d.owner}`}</li>
                ))}
              </ul>
            </section>
          )}

          <section>
            <h2>Similar resolved cases</h2>
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
          </section>
        </div>

        <aside>
          <h2>Drafts</h2>
          {data.drafts.map((d, i) => (
            <article key={d.id} className="monitor-card">
              <h3>
                {d.title} {i === 0 && <Tag>Suggested</Tag>}
                {d.review_required && <Tag tone="warn-soft">Review before sending</Tag>}
              </h3>
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
            </article>
          ))}

          {data.slack_threads.length > 0 && (
            <article className="monitor-card">
              <h3>Tag the team</h3>
              {!slackConnected ? (
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
