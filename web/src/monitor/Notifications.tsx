import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Button, EmptyState, Input, Segmented, Tag } from "../ui";
import { daysSince, withoutMirrors } from "./tone";
import { formatAge, formatTime } from "./util";
import type { QueueCase, TechIssue } from "./types";

// Replies older than this aren't news any more.
const WINDOW_DAYS = 14;
const NOTIFY_KEY = "monitor.notify";
const SOUND_KEY = "monitor.sound";

export type NoticeKind = "aggregator" | "engineering" | "unlinked";
export type Notice = {
  id: string; // what a view is saved against: the case, or the issue when no case links it
  caseId: string | null;
  caseNumber: string | null;
  subject: string | null;
  owner: string | null;
  kind: NoticeKind;
  who: string | null;
  source: string;
  at: string;
  text: string | null;
  url: string | null;
  unread: boolean;
  issues: { number: string; url: string }[]; // the GitHub issues behind it
  // Still waiting on us, however old: it stays listed until the case moves on.
  pending: boolean;
  why: string | null;
};

// An engineer has given the cause or the fix, so the next step is ours.
const TECH_DONE = new Set(["answered", "cause_given"]);
const ACT = new Set(["act", "close"]);

function stored(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function store(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* the choice just won't be remembered */
  }
}

let audio: AudioContext | null = null;

/** Browsers only allow sound after a click, so this runs on the first one. */
function unlockAudio() {
  audio = audio ?? new AudioContext();
  void audio.resume();
}

function chime() {
  if (!audio) return;
  [0, 0.18].forEach((delay, i) => {
    const tone = audio!.createOscillator();
    const volume = audio!.createGain();
    const start = audio!.currentTime + delay;
    tone.frequency.value = i ? 1046 : 784;
    volume.gain.setValueAtTime(0.2, start);
    volume.gain.exponentialRampToValueAtTime(0.001, start + 0.3);
    tone.connect(volume).connect(audio!.destination);
    tone.start(start);
    tone.stop(start + 0.3);
  });
}

function desktopOn() {
  return stored(NOTIFY_KEY, "0") === "1" && "Notification" in window && Notification.permission === "granted";
}

// Held on to, or the browser can drop the notification and its click handler.
const shown = new Set<Notification>();

function desktop(title: string, body: string, onClick: () => void) {
  const n = new Notification(title, { body });
  n.onclick = () => {
    window.focus();
    n.close();
    onClick();
  };
  n.onclose = () => shown.delete(n);
  shown.add(n);
}

/** A case's GitHub issues, by number. */
export function githubIssues(c: Pick<QueueCase, "links">) {
  const seen = new Map<string, string>();
  for (const l of c.links ?? []) {
    const m = l.kind === "github" ? /\/(?:issues|pull)\/(\d+)/.exec(l.url) : null;
    if (m && !seen.has(m[1])) seen.set(m[1], l.url);
  }
  return [...seen].map(([number, url]) => ({ number, url }));
}

function day(at: string) {
  return new Date(at).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/** Replies that matter to the lead: an aggregator answering a DSP case, or engineering
 * answering. A reply still waiting on us is listed however old it is, so after days away
 * one refresh shows everything that's pending. */
export function notices(cases: QueueCase[], views: Record<string, string>, issues: TechIssue[] = [],
                        dependencyType = "DSP"): Notice[] {
  const since = Date.now() - WINDOW_DAYS * 86400000;
  const open = withoutMirrors(cases, dependencyType);
  const byId = new Map(open.map((c) => [c.id, c]));
  const out = new Map<string, Notice>();
  const unread = (id: string, at: string) => !views[id] || new Date(at) > new Date(views[id]);
  const add = (raw: Omit<Notice, "unread" | "issues">, issueList: Notice["issues"]) => {
    const prior = out.get(raw.id);
    const merged = [...(prior?.issues ?? []), ...issueList].filter((x, i, a) => a.findIndex((y) => y.number === x.number) === i);
    const n = { ...raw, issues: merged };
    if (prior && prior.at >= n.at) {
      out.set(n.id, { ...prior, issues: merged, ...(n.pending && !prior.pending ? { pending: true, why: n.why } : {}) });
      return;
    }
    out.set(n.id, { ...n, pending: n.pending || !!prior?.pending, why: n.why ?? prior?.why ?? null,
                    unread: unread(n.id, n.at) });
  };

  for (const c of open) {
    const l = c.latest;
    if (!l) continue;
    let kind: NoticeKind | null = null;
    if (l.party === "engineering") kind = "engineering";
    else if (l.party === "third_party" && c.record_type === dependencyType && c.signal?.code !== "third_party_ack") kind = "aggregator";
    if (!kind) continue;
    const pending = ACT.has(c.stage);
    if (!pending && new Date(l.at).getTime() < since) continue;
    add({ id: c.id, caseId: c.id, caseNumber: c.case_number, subject: c.subject, owner: c.owner, kind,
          who: l.who, source: l.source, at: l.at, text: l.text, url: l.url, pending,
          why: pending ? `Replied ${day(l.at)} and nobody has acted on it since.` : null }, githubIssues(c));
  }

  for (const i of issues) {
    const done = TECH_DONE.has(i.status) && i.waiting_on !== "tech";
    const at = i.last_tech_reply_at ?? i.last_activity_at ?? i.opened_at ?? "";
    if (!at) continue;
    const linked = (i.case_ids ?? []).map((id) => byId.get(id)).filter((c): c is QueueCase => !!c);
    if (!linked.length) {
      add({ id: `gh:${i.url}`, caseId: null, caseNumber: null, subject: i.title, owner: i.reporter ?? null,
            kind: "unlinked", who: i.last_reply_by, source: `GitHub #${i.issue}`, at, text: i.reason || null,
            url: i.url, pending: true,
            why: `No open case links this issue.${i.reporter ? ` Raised by ${i.reporter}` : ""}`
               + `${i.biz_id ? ` for ${i.biz_name ?? "Biz"} (${i.biz_id})` : ""}.`
               + (done ? " Engineering has answered it." : "") }, [{ number: i.issue, url: i.url }]);
      continue;
    }
    if (!done) continue;
    const told = i.last_support_reply_at && i.last_tech_reply_at && i.last_support_reply_at > i.last_tech_reply_at
      ? ` Support replied on GitHub ${day(i.last_support_reply_at)} but the case wasn't moved on.` : "";
    for (const c of linked) {
      // Still marked Waiting on Engineer after engineering answered: nobody told the merchant.
      if (c.waiting_party !== "engineering" && !ACT.has(c.stage)) continue;
      add({ id: c.id, caseId: c.id, caseNumber: c.case_number, subject: c.subject, owner: c.owner,
            kind: "engineering", who: i.last_reply_by, source: `GitHub #${i.issue}`, at,
            text: i.reason || null, url: i.url, pending: true,
            why: `${i.tech_status} on GitHub ${day(at)}. The case still says it's waiting on engineering.${told}` },
          [{ number: i.issue, url: i.url }]);
    }
  }
  return [...out.values()].sort((a, b) => Number(b.unread) - Number(a.unread)
    || Number(b.pending) - Number(a.pending) || b.at.localeCompare(a.at));
}

/** Notifications from the synced cases, what this person has viewed, and alerts for new ones. */
export function useNotifications(tenantId: string | undefined, cases: QueueCase[] | null,
                                 onClick: (caseId: string | null) => void) {
  const [views, setViews] = useState<Record<string, string> | null>(null);
  const [issues, setIssues] = useState<TechIssue[]>([]);
  const [desktopEnabled, setDesktopEnabled] = useState(desktopOn);
  const [sound, setSound] = useState(() => stored(SOUND_KEY, "1") !== "0");
  const known = useRef<Set<string> | null>(null);
  const clicked = useRef(onClick);
  clicked.current = onClick;

  useEffect(() => {
    api.monitor.notificationViews(tenantId).then((r) => setViews(r.views)).catch(() => setViews({}));
  }, [tenantId]);
  // Re-read with every new queue, so a refresh brings the issues' latest state too.
  useEffect(() => {
    if (!cases) return;
    api.monitor.techPending(tenantId).then((d) => setIssues(d.squads.flatMap((s) => s.issues))).catch(() => {});
  }, [tenantId, cases]);
  useEffect(() => {
    window.addEventListener("pointerdown", unlockAudio, { once: true });
    return () => window.removeEventListener("pointerdown", unlockAudio);
  }, []);

  const list = useMemo(() => (cases && views ? notices(cases, views, issues) : null), [cases, views, issues]);
  const unread = list?.filter((n) => n.unread) ?? [];
  const pending = list?.filter((n) => n.pending).length ?? 0;

  // Alert only for replies that arrive while the app is open, not for what was already there.
  useEffect(() => {
    if (!list) return;
    const keys = new Set(list.filter((n) => n.unread).map((n) => `${n.id}@${n.at}`));
    if (known.current) {
      const fresh = list.filter((n) => n.unread && !known.current!.has(`${n.id}@${n.at}`));
      if (fresh.length) {
        if (stored(SOUND_KEY, "1") !== "0") chime();
        if (desktopOn()) {
          const first = fresh[0];
          const who = first.kind === "aggregator" ? first.who ?? "Aggregator" : "Engineering";
          desktop(fresh.length === 1 ? `${who} replied on ${first.caseNumber ?? first.source}` : `${fresh.length} new replies`,
            `${first.subject ?? ""}${first.text ? `: ${first.text.slice(0, 120)}` : ""}`,
            () => clicked.current(fresh.length === 1 ? first.caseId : null));
        }
      }
    }
    known.current = keys;
  }, [list]);

  const markViewed = useCallback(async (caseIds: string[]) => {
    if (!caseIds.length) return;
    const stamp = new Date().toISOString();
    setViews((v) => ({ ...(v ?? {}), ...Object.fromEntries(caseIds.map((id) => [id, stamp])) }));
    try {
      const r = await api.monitor.markNotificationsViewed(caseIds, tenantId);
      setViews((v) => ({ ...(v ?? {}), ...Object.fromEntries(caseIds.map((id) => [id, r.viewed_at])) }));
    } catch {
      api.monitor.notificationViews(tenantId).then((r) => setViews(r.views)).catch(() => {});
    }
  }, [tenantId]);

  const enableDesktop = async () => {
    if (!("Notification" in window)) return;
    const result = await Notification.requestPermission();
    store(NOTIFY_KEY, result === "granted" ? "1" : "0");
    setDesktopEnabled(result === "granted");
  };
  const toggleSound = () => {
    store(SOUND_KEY, sound ? "0" : "1");
    setSound(!sound);
  };
  const test = () => {
    unlockAudio();
    if (sound) chime();
    if (desktopOn()) desktop("Case Monitor: test notification", "Notifications are working.", () => clicked.current(null));
  };

  return { list, unread: unread.length, pending, markViewed, desktopEnabled, enableDesktop, sound, toggleSound, test };
}

const KIND: Record<NoticeKind, string> = {
  aggregator: "Aggregator responded", engineering: "Engineering replied", unlinked: "No case linked",
};
type Filter = "all" | "pending" | NoticeKind;

export function NotificationsView({ state, onOpen, thirdParty }: {
  state: ReturnType<typeof useNotifications>; onOpen: (id: string) => void; thirdParty: string;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [q, setQ] = useState("");
  const { list, unread, pending, markViewed, desktopEnabled, enableDesktop, sound, toggleSound, test } = state;
  if (!list) return null;
  const query = q.trim().replace(/^#/, "").replace(/^0+/, "");
  const matches = (n: Notice) => !query || (n.caseNumber ?? "").replace(/^0+/, "").includes(query)
    || n.issues.some((x) => x.number.endsWith(query));
  const shownList = list.filter((n) => matches(n)
    && (query !== "" || filter === "all" || (filter === "pending" ? n.pending : n.kind === filter)));
  const count = (k: NoticeKind) => list.filter((n) => n.kind === k && (n.unread || n.pending)).length;
  const unreadShown = shownList.filter((n) => n.unread).map((n) => n.id);
  const title = [unread ? `${unread} new` : "", pending ? `${pending} pending with us` : ""].filter(Boolean).join(" · ");

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>{title || "Nothing new or pending"}</h2>
          <p className="monitor-muted">
            Replies from {thirdParty.toLowerCase()}s on DSP cases and from engineering. Anything still waiting on us
            stays here however old it is, so after time away one refresh shows what's pending. Mark viewed only
            clears the highlight. A pending item leaves the list once the case moves on.
          </p>
        </div>
        <div className="monitor-actions">
          {!desktopEnabled
            ? <Button size="sm" variant="secondary" onClick={enableDesktop}>Turn on desktop notifications</Button>
            : <span className="monitor-muted">Desktop notifications on</span>}
          <Button size="sm" variant="ghost" onClick={toggleSound}>Sound: {sound ? "on" : "off"}</Button>
          <Button size="sm" variant="ghost" onClick={test}>Test</Button>
          <Button size="sm" variant="primary" disabled={!unreadShown.length} onClick={() => markViewed(unreadShown)}>
            Mark all viewed
          </Button>
        </div>
      </header>

      <Segmented aria-label="Notification type" value={filter} onChange={setFilter}
                 options={[{ value: "all", label: `All (${list.filter((n) => n.unread || n.pending).length})` },
                   { value: "pending", label: `Pending with us (${pending})` },
                   { value: "aggregator", label: `${thirdParty} responded (${count("aggregator")})` },
                   { value: "engineering", label: `Engineering (${count("engineering")})` },
                   { value: "unlinked", label: `No case linked (${count("unlinked")})` }]} />

      <Input aria-label="Find by case or GitHub number" placeholder="Case or GitHub number, e.g. 8742"
             value={q} onChange={(e) => setQ(e.target.value)} className="notice-find" />

      {shownList.length === 0 ? (
        <EmptyState title="Nothing here" body="New replies, and anything still waiting on us, appear here after each refresh." />
      ) : (
        <ul className="notice-list">
          {shownList.map((n) => (
            <li key={`${n.kind}:${n.id}`} className={n.unread ? "is-unread" : ""}>
              <span className="notice-dot" aria-hidden />
              <div className="notice-body">
                <div className="notice-head">
                  <Tag tone={n.kind === "aggregator" ? "warn" : n.kind === "unlinked" ? "neutral" : "accent"}>{KIND[n.kind]}</Tag>
                  {n.caseId
                    ? <button type="button" className="monitor-link" onClick={() => onOpen(n.caseId!)}>{n.caseNumber ?? "Case"}</button>
                    : <span className="monitor-muted">No case</span>}
                  <span className="notice-subject">{n.subject}</span>
                  {n.issues.map((x) => (
                    <a key={x.number} className="notice-issue" href={x.url} target="_blank" rel="noreferrer"
                       aria-label={`GitHub issue ${x.number}`}>GitHub #{x.number}</a>
                  ))}
                  {n.unread && <span className="notice-new">New</span>}
                  {n.pending && <Tag tone="exception">Pending {Math.floor(daysSince(n.at) ?? 0)}d</Tag>}
                </div>
                <div className="monitor-muted notice-meta">
                  {n.who && <>{n.who} · </>}{n.source} · {formatAge((Date.now() - new Date(n.at).getTime()) / 3600000)} ago ({formatTime(n.at)})
                  {n.caseId && n.owner && <> · Agent: {n.owner}</>}
                  {n.caseId && n.url && <> · <a href={n.url} target="_blank" rel="noreferrer">Open reply</a></>}
                </div>
                {n.why && <p className="notice-why">{n.why}</p>}
                {n.text && <p className="notice-text">{n.text.slice(0, 300)}</p>}
              </div>
              <div className="notice-actions">
                {n.unread
                  ? <Button size="sm" variant="secondary" onClick={() => markViewed([n.id])}>Mark viewed</Button>
                  : <span className="monitor-muted">Viewed</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
