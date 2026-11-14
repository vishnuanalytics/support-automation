import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Button, EmptyState, Segmented, Tag } from "../ui";
import { withoutMirrors } from "./tone";
import { formatAge, formatTime } from "./util";
import type { QueueCase } from "./types";

// Replies older than this aren't news any more.
const WINDOW_DAYS = 14;
const NOTIFY_KEY = "monitor.notify";
const SOUND_KEY = "monitor.sound";

export type NoticeKind = "aggregator" | "engineering";
export type Notice = {
  caseId: string;
  caseNumber: string | null;
  subject: string | null;
  kind: NoticeKind;
  who: string | null;
  source: string;
  at: string;
  text: string | null;
  url: string | null;
  unread: boolean;
};

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

/** Replies that matter to the lead: an aggregator answering a DSP case, or engineering answering. */
export function notices(cases: QueueCase[], views: Record<string, string>, dependencyType = "DSP"): Notice[] {
  const since = Date.now() - WINDOW_DAYS * 86400000;
  const out: Notice[] = [];
  for (const c of withoutMirrors(cases, dependencyType)) {
    const l = c.latest;
    if (!l || new Date(l.at).getTime() < since) continue;
    let kind: NoticeKind | null = null;
    if (l.party === "engineering") kind = "engineering";
    else if (l.party === "third_party" && c.record_type === dependencyType && c.signal?.code !== "third_party_ack") kind = "aggregator";
    if (!kind) continue;
    const viewed = views[c.id];
    out.push({
      caseId: c.id, caseNumber: c.case_number, subject: c.subject, kind, who: l.who, source: l.source,
      at: l.at, text: l.text, url: l.url, unread: !viewed || new Date(l.at) > new Date(viewed),
    });
  }
  return out.sort((a, b) => Number(b.unread) - Number(a.unread) || b.at.localeCompare(a.at));
}

/** Notifications from the synced cases, what this person has viewed, and alerts for new ones. */
export function useNotifications(tenantId: string | undefined, cases: QueueCase[] | null,
                                 onClick: (caseId: string | null) => void) {
  const [views, setViews] = useState<Record<string, string> | null>(null);
  const [desktopEnabled, setDesktopEnabled] = useState(desktopOn);
  const [sound, setSound] = useState(() => stored(SOUND_KEY, "1") !== "0");
  const known = useRef<Set<string> | null>(null);
  const clicked = useRef(onClick);
  clicked.current = onClick;

  useEffect(() => {
    api.monitor.notificationViews(tenantId).then((r) => setViews(r.views)).catch(() => setViews({}));
  }, [tenantId]);
  useEffect(() => {
    window.addEventListener("pointerdown", unlockAudio, { once: true });
    return () => window.removeEventListener("pointerdown", unlockAudio);
  }, []);

  const list = useMemo(() => (cases && views ? notices(cases, views) : null), [cases, views]);
  const unread = list?.filter((n) => n.unread) ?? [];

  // Alert only for replies that arrive while the app is open, not for what was already there.
  useEffect(() => {
    if (!list) return;
    const keys = new Set(list.filter((n) => n.unread).map((n) => `${n.caseId}@${n.at}`));
    if (known.current) {
      const fresh = list.filter((n) => n.unread && !known.current!.has(`${n.caseId}@${n.at}`));
      if (fresh.length) {
        if (stored(SOUND_KEY, "1") !== "0") chime();
        if (desktopOn()) {
          const first = fresh[0];
          const who = first.kind === "engineering" ? "Engineering" : first.who ?? "Aggregator";
          desktop(fresh.length === 1 ? `${who} replied on ${first.caseNumber ?? "a case"}` : `${fresh.length} new replies`,
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

  return { list, unread: unread.length, markViewed, desktopEnabled, enableDesktop, sound, toggleSound, test };
}

const KIND: Record<NoticeKind, string> = { aggregator: "Aggregator responded", engineering: "Engineering replied" };

export function NotificationsView({ state, onOpen, thirdParty }: {
  state: ReturnType<typeof useNotifications>; onOpen: (id: string) => void; thirdParty: string;
}) {
  const [kind, setKind] = useState<"all" | NoticeKind>("all");
  const { list, unread, markViewed, desktopEnabled, enableDesktop, sound, toggleSound, test } = state;
  if (!list) return null;
  const shownList = list.filter((n) => kind === "all" || n.kind === kind);
  const count = (k: NoticeKind) => list.filter((n) => n.kind === k && n.unread).length;
  const unreadShown = shownList.filter((n) => n.unread).map((n) => n.caseId);

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>{unread ? `${unread} new notification${unread === 1 ? "" : "s"}` : "No new notifications"}</h2>
          <p className="monitor-muted">
            Replies from {thirdParty.toLowerCase()}s on DSP cases and from engineering in the last {WINDOW_DAYS} days.
            New ones stay highlighted until you mark them viewed. A later reply on the same case highlights it again.
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

      <Segmented aria-label="Notification type" value={kind} onChange={setKind}
                 options={[{ value: "all", label: `All (${unread})` },
                   { value: "aggregator", label: `${thirdParty} responded (${count("aggregator")})` },
                   { value: "engineering", label: `Engineering (${count("engineering")})` }]} />

      {shownList.length === 0 ? (
        <EmptyState title="Nothing yet" body="New replies appear here after each refresh." />
      ) : (
        <ul className="notice-list">
          {shownList.map((n) => (
            <li key={n.caseId} className={n.unread ? "is-unread" : ""}>
              <span className="notice-dot" aria-hidden />
              <div className="notice-body">
                <div className="notice-head">
                  <Tag tone={n.kind === "engineering" ? "accent" : "warn"}>{KIND[n.kind]}</Tag>
                  <button type="button" className="monitor-link" onClick={() => onOpen(n.caseId)}>{n.caseNumber ?? "Case"}</button>
                  <span className="notice-subject">{n.subject}</span>
                  {n.unread && <span className="notice-new">New</span>}
                </div>
                <div className="monitor-muted notice-meta">
                  {n.who} · {n.source} · {formatAge((Date.now() - new Date(n.at).getTime()) / 3600000)} ago ({formatTime(n.at)})
                  {n.url && <> · <a href={n.url} target="_blank" rel="noreferrer">Open reply</a></>}
                </div>
                {n.text && <p className="notice-text">{n.text.slice(0, 300)}</p>}
              </div>
              <div className="notice-actions">
                {n.unread
                  ? <Button size="sm" variant="secondary" onClick={() => markViewed([n.caseId])}>Mark viewed</Button>
                  : <span className="monitor-muted">Viewed</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
