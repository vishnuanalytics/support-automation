import { Button, Tag } from "../ui";
import { formatTime } from "./util";
import type { Investigation as Write } from "./types";

/** A debug-with-proof write-up: the cause, a quote behind each claim, and what wasn't verified. */
export function InvestigationCard({ w, title, onCopy }: {
  w: Write; title: string; onCopy: (text: string, what: string) => void;
}) {
  return (
    <article className="monitor-card" aria-label={title}>
      <h3>{title} <Tag tone="warn-soft">AI-assisted, verify</Tag></h3>
      <p><strong>Root cause:</strong> {w.root_cause}</p>
      {w.evidence.length > 0 && (
        <>
          <p className="monitor-muted">Evidence</p>
          <ul>
            {w.evidence.map((e, i) => (
              <li key={i}>
                {e.claim}{" "}
                <span className="monitor-muted">
                  {e.url?.startsWith("http")
                    ? <a href={e.url} target="_blank" rel="noreferrer">{e.source_name}</a> : e.source_name}
                  : “{e.quote}”
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
      {w.not_verified.length > 0 && (
        <>
          <p className="monitor-muted">Not verified</p>
          <ul>{w.not_verified.map((x, i) => <li key={i}>{x}</li>)}</ul>
        </>
      )}
      {w.next_action && <p><strong>Next:</strong> {w.next_action}</p>}
      {w.draft_reply && (
        <>
          <pre>{w.draft_reply}</pre>
          <Button size="sm" onClick={() => onCopy(w.draft_reply, "Draft reply")}>Copy reply</Button>
        </>
      )}
      {w.created_at && (
        <p className="monitor-muted">{w.saved_by || "Unknown"} · {formatTime(w.created_at)}</p>
      )}
    </article>
  );
}
