import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Button, EmptyState, Skeleton, Tag, type TagTone } from "../ui";
import { formatTime } from "./util";
import type { Alert } from "./types";

const POLL_MS = 60_000;
const NOTIFY_KEY = "monitor.notify";
const KIND: Record<string, { label: string; tone: TagTone }> = {
  needs_action: { label: "Needs action", tone: "warn" },
  new_reply: { label: "New reply", tone: "neutral" },
  newly_overdue: { label: "Overdue", tone: "exception" },
  newly_stale: { label: "Gone quiet", tone: "neutral" },
  newly_missing_link: { label: "No link", tone: "warn" },
};

const SOUND_KEY = "monitor.sound";
let audio: AudioContext | null = null;

function wantsSound() {
  try {
    return localStorage.getItem(SOUND_KEY) !== "0";
  } catch {
    return true;
  }
}

/** Browsers only allow sound after a click, so this runs on the first one. */
function unlockAudio() {
  audio = audio ?? new AudioContext();
  void audio.resume();
}

function playChime() {
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

function wantsDesktop() {
  try {
    return localStorage.getItem(NOTIFY_KEY) === "1" && "Notification" in window
      && Notification.permission === "granted";
  } catch {
    return false;
  }
}

// Held on to, or the browser can drop the notification and its click handler.
const shown = new Set<Notification>();

function notify(title: string, body: string, onClick: () => void) {
  const notification = new Notification(title, { body });
  notification.onclick = () => {
    window.focus();
    notification.close();
    onClick();
  };
  notification.onclose = () => shown.delete(notification);
  shown.add(notification);
}

/** Polls for alerts, and shows a desktop notification for each batch of new ones.
 * Clicking one opens its case, or the Alerts page (null) for several. */
export function useAlerts(tenantId: string | undefined, onNotificationClick: (caseId: string | null) => void) {
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [unread, setUnread] = useState(0);
  const [desktop, setDesktop] = useState(wantsDesktop);
  const [sound, setSound] = useState(wantsSound);
  const newest = useRef<number | null>(null);
  const clicked = useRef(onNotificationClick);
  clicked.current = onNotificationClick;

  useEffect(() => {
    window.addEventListener("pointerdown", unlockAudio, { once: true });
    return () => window.removeEventListener("pointerdown", unlockAudio);
  }, []);

  const load = useCallback(() => {
    api.monitor.alerts(tenantId).then((r) => {
      setAlerts(r.alerts);
      setUnread(r.unread);
      const top = r.alerts[0]?.id ?? 0;
      if (newest.current !== null && top > newest.current) {
        const fresh = r.alerts.filter((a) => a.id > newest.current! && a.unread);
        if (fresh.length) {
          if (wantsSound()) playChime();
          if (wantsDesktop()) {
            const first = fresh[0];
            notify(`Case Monitor: ${fresh.length} new alert${fresh.length === 1 ? "" : "s"}`,
              `${first.case_number ?? ""} ${first.detail ?? first.subject ?? ""}`.trim(),
              () => clicked.current(fresh.length === 1 ? first.case_id : null));
          }
        }
      }
      newest.current = top;
    }).catch(() => {});
  }, [tenantId]);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const markSeen = async () => {
    await api.monitor.alertsSeen(tenantId);
    load();
  };

  const enableDesktop = async () => {
    if (!("Notification" in window)) return;
    const result = await Notification.requestPermission();
    try {
      localStorage.setItem(NOTIFY_KEY, result === "granted" ? "1" : "0");
    } catch {
      /* the choice just won't be remembered */
    }
    setDesktop(result === "granted");
  };

  const toggleSound = () => {
    const next = !sound;
    setSound(next);
    try {
      localStorage.setItem(SOUND_KEY, next ? "1" : "0");
    } catch {
      /* the choice just won't be remembered */
    }
  };

  const testAlert = () => {
    unlockAudio();
    if (sound) playChime();
    if (wantsDesktop()) notify("Case Monitor: test alert", "Alerts are working.", () => clicked.current(null));
  };

  return { alerts, unread, desktop, sound, refresh: load, markSeen, enableDesktop, toggleSound, testAlert };
}

export function AlertsView({ state, onOpen }: {
  state: ReturnType<typeof useAlerts>;
  onOpen: (id: string) => void;
}) {
  const { alerts, unread, desktop, sound, markSeen, enableDesktop, toggleSound, testAlert } = state;
  if (!alerts) return <Skeleton variant="row" lines={6} />;
  return (
    <section className="monitor-digest">
      <div className="monitor-fu-owner">
        <h2>{unread} new alert{unread === 1 ? "" : "s"}</h2>
        <div className="monitor-actions">
          {!desktop && <Button size="sm" onClick={enableDesktop}>Turn on desktop alerts</Button>}
          {desktop && <span className="monitor-muted">Desktop alerts on while this app is open</span>}
          <Button size="sm" onClick={toggleSound}>Sound: {sound ? "on" : "off"}</Button>
          <Button size="sm" onClick={testAlert}>Test alert</Button>
          <Button size="sm" disabled={!unread} onClick={markSeen}>Mark all read</Button>
        </div>
      </div>
      {alerts.length === 0 && (
        <EmptyState title="No alerts yet" body="Changes appear here after the next sync." />
      )}
      {alerts.map((a) => (
        <div key={a.id} className={`monitor-alert${a.unread ? " is-unread" : ""}`}>
          <Tag tone={KIND[a.kind]?.tone ?? "neutral"}>{KIND[a.kind]?.label ?? a.kind}</Tag>
          <button type="button" className="monitor-link" onClick={() => onOpen(a.case_id)}>
            {a.case_number || "Case"}
          </button>
          <div className="monitor-alert-text">
            {a.detail || a.subject}
            {a.detail && <div className="monitor-fu-subject">{a.subject}</div>}
          </div>
          <span className="monitor-muted">{formatTime(a.created_at)}</span>
        </div>
      ))}
    </section>
  );
}
