import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, EmptyState, Input, Segmented, Tag } from "../ui";
import { LastResponse } from "./LastResponse";
import { daysSince, withoutMirrors } from "./tone";
import { formatDay } from "./util";
import type { EscalationMark, QueueCase } from "./types";

export type Marks = {
  marks: Map<string, EscalationMark>;
  err: string | null;
  act: (caseId: string, action: "escalate" | "responded" | "reopen", note?: string) => Promise<void>;
  clear: (caseId: string) => Promise<void>;
};

/** The escalation log, shared by the aggregator pages. Writes only Case Monitor's own table. */
export function useEscalationMarks(tenantId?: string): Marks {
  const [marks, setMarks] = useState<Map<string, EscalationMark>>(new Map());
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.monitor.escalationMarks(tenantId)
      .then((r) => setMarks(new Map(r.marks.map((m) => [m.case_id, m]))))
      .catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);
  const act = useCallback(async (caseId: string, action: "escalate" | "responded" | "reopen", note?: string) => {
    setErr(null);
    try {
      const saved = await api.monitor.setEscalationMark(caseId, action, note, tenantId);
      setMarks((m) => new Map(m).set(caseId, saved));
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [tenantId]);
  const clear = useCallback(async (caseId: string) => {
    setErr(null);
    try {
      await api.monitor.clearEscalationMark(caseId, tenantId);
      setMarks((m) => { const next = new Map(m); next.delete(caseId); return next; });
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [tenantId]);
  return { marks, err, act, clear };
}

/** Asks for an optional note, then records the escalation. */
export function EscalateButton({ caseId, marks, label = "I escalated this" }: {
  caseId: string; marks: Marks; label?: string;
}) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const mark = marks.marks.get(caseId);
  if (mark?.manual && mark.escalated_at && !mark.responded_at && !open) {
    return <Tag tone="accent">Escalated {formatDay(mark.escalated_at)}</Tag>;
  }
  if (!open) return <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>{label}</Button>;
  const save = async () => {
    setBusy(true);
    await marks.act(caseId, "escalate", note);
    setBusy(false);
    setOpen(false);
    setNote("");
  };
  return (
    <div className="esc-note">
      <Input aria-label="Where you escalated" placeholder="Where and to whom (optional)" value={note}
             onChange={(e) => setNote(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void save()} autoFocus />
      <Button size="sm" variant="primary" loading={busy} onClick={save}>Save</Button>
      <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
    </div>
  );
}

type Row = { c: QueueCase; mark: EscalationMark | undefined; since: string | null; repliedAfter: boolean };

function aggregatorReplied(c: QueueCase, since: string | null): boolean {
  if (c.latest?.party !== "third_party" || c.signal?.code === "third_party_ack") return false;
  return !since || new Date(c.latest.at) > new Date(since);
}

export function EscalatedView({ cases, tenantId, onOpen, thirdParty, customer, marks }: {
  cases: QueueCase[];
  tenantId?: string;
  onOpen: (id: string) => void;
  thirdParty: string;
  customer: string;
  marks: Marks;
}) {
  void tenantId;
  const [tab, setTab] = useState<"open" | "responded">("open");
  const [find, setFind] = useState("");
  const [addErr, setAddErr] = useState<string | null>(null);

  const rows = useMemo(() => {
    const byId = new Map(cases.map((c) => [c.id, c]));
    const auto = withoutMirrors(cases.filter((c) => c.waiting_party === "third_party"));
    const ids = new Set([...auto.map((c) => c.id), ...[...marks.marks.values()].filter((m) => m.manual).map((m) => m.case_id)]);
    const out: Row[] = [];
    for (const id of ids) {
      const c = byId.get(id);
      if (!c) continue; // closed since, so it has left the open list
      const mark = marks.marks.get(id);
      const since = mark?.manual ? mark.escalated_at : null;
      out.push({ c, mark, since, repliedAfter: aggregatorReplied(c, since) });
    }
    return out.sort((a, b) => Number(b.repliedAfter) - Number(a.repliedAfter)
      || (daysSince(b.c.latest?.at) ?? 0) - (daysSince(a.c.latest?.at) ?? 0));
  }, [cases, marks.marks]);

  const open = rows.filter((r) => !r.mark?.responded_at);
  const responded = rows.filter((r) => r.mark?.responded_at)
    .sort((a, b) => (b.mark!.responded_at! > a.mark!.responded_at! ? 1 : -1));
  const shown = tab === "open" ? open : responded;
  const replied = open.filter((r) => r.repliedAfter).length;

  const add = async () => {
    const q = find.trim().replace(/^0+/, "");
    const hit = cases.find((c) => (c.case_number ?? "").replace(/^0+/, "") === q);
    if (!hit) {
      setAddErr(`No open case ${find.trim()} in the last sync`);
      return;
    }
    setAddErr(null);
    await marks.act(hit.id, "escalate");
    setFind("");
    setTab("open");
  };

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>Escalated to {thirdParty}</h2>
          <p className="monitor-muted">
            Every case waiting on {thirdParty.toLowerCase()} in Salesforce, plus the ones you added. When their team
            responds, press <strong>Team responded</strong> and it moves to the Responded tab. Salesforce isn't changed.
          </p>
        </div>
        <div className="monitor-actions">
          <Input aria-label="Case number to add" placeholder="Add a case number" value={find}
                 onChange={(e) => setFind(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void add()} />
          <Button size="sm" variant="secondary" disabled={!find.trim()} onClick={add}>Add as escalated</Button>
        </div>
      </header>
      {marks.err && <Banner tone="exception" title={marks.err} />}
      {addErr && <Banner tone="warn" title={addErr} />}

      <div className="page-stats">
        <div><strong>{open.length}</strong>Waiting on {thirdParty.toLowerCase()}</div>
        <div className={replied ? "is-warn" : ""}><strong>{replied}</strong>They replied: check and close</div>
        <div><strong>{open.filter((r) => r.mark?.manual).length}</strong>Added by you</div>
        <div><strong>{responded.length}</strong>Responded</div>
      </div>

      <Segmented aria-label="Escalation state" value={tab} onChange={setTab}
                 options={[{ value: "open", label: `Waiting (${open.length})` },
                   { value: "responded", label: `Responded (${responded.length})` }]} />

      {shown.length === 0 ? (
        <EmptyState title={tab === "open" ? "Nothing waiting" : "Nothing responded yet"}
                    body={tab === "open" ? `No open case is waiting on ${thirdParty.toLowerCase()}.`
                      : "Cases you close with Team responded show here."} />
      ) : (
        <div className="monitor-esc-wrap">
          <table className="monitor-esc">
            <thead>
              <tr>
                <th>Case</th><th>{thirdParty}</th><th>Escalated</th><th>Last reply</th><th>Owner</th><th />
              </tr>
            </thead>
            <tbody>
              {shown.map(({ c, mark, since, repliedAfter }) => (
                <tr key={c.id} className={repliedAfter && tab === "open" ? "is-flag" : ""}>
                  <td>
                    <button type="button" className="monitor-link" onClick={() => onOpen(c.id)}>{c.case_number}</button>
                    <div className="monitor-fu-subject">{c.subject}</div>
                    {mark?.note && <div className="esc-note-text">{mark.note}</div>}
                  </td>
                  <td>{c.third_parties.join(", ") || "Not set"}</td>
                  <td>
                    {since ? formatDay(since) : <span className="monitor-muted">Waiting on {thirdParty} in Salesforce</span>}
                    {mark?.manual && <div><Tag>Added by you</Tag></div>}
                  </td>
                  <td>
                    {repliedAfter && tab === "open" && <div><Tag tone="warn">{thirdParty} replied</Tag></div>}
                    <LastResponse latest={c.latest} thirdParty={thirdParty} customer={customer} brief />
                  </td>
                  <td>{c.owner}</td>
                  <td className="esc-actions">
                    {tab === "open" ? (
                      <>
                        <Button size="sm" variant={repliedAfter ? "primary" : "secondary"}
                                onClick={() => marks.act(c.id, "responded")}>Team responded</Button>
                        {!mark?.manual && <EscalateButton caseId={c.id} marks={marks} label="Add note" />}
                        {mark?.manual && (
                          <Button size="sm" variant="ghost" onClick={() => marks.clear(c.id)}>Remove</Button>
                        )}
                      </>
                    ) : (
                      <>
                        <span className="monitor-muted">Closed {formatDay(mark?.responded_at)}</span>
                        <Button size="sm" variant="ghost" onClick={() => marks.act(c.id, "reopen")}>Reopen</Button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
