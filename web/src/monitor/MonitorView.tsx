import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, ConfirmButton, Dialog, EmptyState, Input, Select, Skeleton, StatTile, Tag, type TagTone } from "../ui";
import { CaseView } from "./CaseView";
import { MonitorSettings } from "./MonitorSettings";
import { copyText, formatTime } from "./util";
import type { Digest, Queue, QueueCase, SlackStatus } from "./types";
import "./monitor.css";

type Tab = "mine" | "engineering" | "third_party" | "missing" | "overdue" | "all";
type Filters = { recordType: string; thirdParty: string; owner: string; level: string; search: string };

const NO_FILTERS: Filters = { recordType: "", thirdParty: "", owner: "", level: "", search: "" };
const FILTER_KEY = "monitor.filters";
const LEVEL_TONE: Record<string, TagTone> = {
  critical: "exception", high: "warn", medium: "warn-soft", low: "neutral",
};
const BALL_TEXT: Record<string, string> = {
  us: "Our turn", engineering: "Engineering", third_party: "Third party",
  ai: "AI agent", customer: "Customer to confirm", unknown: "Unknown",
};
const EVENT_TEXT: Record<string, string> = {
  new_case: "New case", new_reply: "New reply", newly_overdue: "Newly overdue",
  newly_missing_link: "Newly missing link",
};

function readFilters(): Filters {
  try {
    return { ...NO_FILTERS, ...JSON.parse(localStorage.getItem(FILTER_KEY) || "{}") };
  } catch {
    return NO_FILTERS;
  }
}

function saveFilters(f: Filters) {
  try {
    localStorage.setItem(FILTER_KEY, JSON.stringify(f));
  } catch {
    /* a remembered filter is only a convenience */
  }
}

function inTab(c: QueueCase, tab: Tab): boolean {
  if (tab === "mine") return c.ball === "us";
  if (tab === "engineering") return c.waiting_party === "engineering" && c.ball !== "us";
  if (tab === "third_party") return c.waiting_party === "third_party" && c.ball !== "us";
  if (tab === "missing") return c.missing_link;
  if (tab === "overdue") return c.overdue;
  return true;
}

function matches(c: QueueCase, f: Filters): boolean {
  const q = f.search.trim().toLowerCase();
  return (
    (!f.recordType || c.record_type === f.recordType) &&
    (!f.thirdParty || c.third_parties.includes(f.thirdParty)) &&
    (!f.owner || c.owner === f.owner) &&
    (!f.level || c.level === f.level) &&
    (!q || `${c.case_number} ${c.subject}`.toLowerCase().includes(q))
  );
}

function choices(all: string, values: (string | null)[]) {
  const unique = [...new Set(values.filter(Boolean) as string[])].sort();
  return [{ value: "", label: all }, ...unique.map((v) => ({ value: v, label: v }))];
}

function Sparkline({ points }: { points: Queue["trend"] }) {
  if (points.length < 2) return null;
  const width = 8;
  const gap = 2;
  const height = 40;
  const max = Math.max(1, ...points.map((p) => p.my_turn));
  return (
    <figure className="monitor-trend">
      <svg width={points.length * (width + gap)} height={height} role="img"
           aria-label={`My turn count across the last ${points.length} syncs, latest ${points[points.length - 1].my_turn}`}>
        {points.map((p, i) => {
          const h = Math.max(2, (p.my_turn / max) * height);
          return (
            <rect key={p.at} x={i * (width + gap)} y={height - h} width={width} height={h} rx={2}>
              <title>{`${formatTime(p.at)}: ${p.my_turn}`}</title>
            </rect>
          );
        })}
      </svg>
      <figcaption>My turn per sync</figcaption>
    </figure>
  );
}

export function MonitorView({ tenantId, isOwner }: { tenantId?: string | null; isOwner: boolean }) {
  const tid = tenantId || undefined;
  const [queue, setQueue] = useState<Queue | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("mine");
  const [filters, setFilters] = useState<Filters>(readFilters);
  const [showHidden, setShowHidden] = useState(false);
  const [selected, setSelected] = useState(0);
  const [openId, setOpenId] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("case"),
  );
  const [mode, setMode] = useState<"queue" | "digest" | "settings">("queue");
  const [digest, setDigest] = useState<Digest | null>(null);
  const [slack, setSlack] = useState<SlackStatus | null>(null);
  const [help, setHelp] = useState(false);
  const pendingG = useRef(false);

  const load = useCallback(() => {
    api.monitor.queue(tid).then(setQueue).catch((e: ApiError) => setErr(e.message));
    api.monitor.slackStatus(tid).then(setSlack).catch(() => {});
  }, [tid]);
  useEffect(load, [load]);

  // A running sync finishes in the worker, so poll until it does.
  useEffect(() => {
    if (!queue?.running) return;
    const timer = window.setTimeout(load, 5000);
    return () => window.clearTimeout(timer);
  }, [queue, load]);

  useEffect(() => {
    if (mode === "digest") api.monitor.digest(tid).then(setDigest).catch((e: ApiError) => setErr(e.message));
  }, [mode, tid]);

  const open = useCallback((id: string | null) => {
    setOpenId(id);
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("case", id);
    else url.searchParams.delete("case");
    window.history.replaceState(null, "", url);
  }, []);

  const updateFilters = (patch: Partial<Filters>) => {
    const next = { ...filters, ...patch };
    setFilters(next);
    saveFilters(next);
    setSelected(0);
  };

  const all = useMemo(() => queue?.cases ?? [], [queue]);
  const visible = useMemo(
    () => all.filter((c) => (showHidden || !c.hidden) && matches(c, filters)),
    [all, showHidden, filters],
  );
  const rows = useMemo(() => visible.filter((c) => inTab(c, tab)), [visible, tab]);
  const count = (t: Tab) => visible.filter((c) => inTab(c, t)).length;
  const thirdParty = queue?.labels.third_party ?? "third party";
  const tabs: { key: Tab; label: string }[] = [
    { key: "mine", label: "My turn" },
    { key: "engineering", label: "Waiting on Engineering" },
    { key: "third_party", label: `Waiting on ${thirdParty}` },
    { key: "missing", label: "Missing link" },
    { key: "overdue", label: "Overdue" },
    { key: "all", label: "All" },
  ];

  const copySuggested = useCallback(async (id: string) => {
    try {
      const detail = await api.monitor.caseDetail(id, tid);
      await copyText(detail.drafts[0].text);
      setNote(`Copied "${detail.drafts[0].title}"`);
    } catch {
      setErr("Could not copy the draft");
    }
  }, [tid]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (pendingG.current) {
        pendingG.current = false;
        if (e.key === "q") {
          open(null);
          setMode("queue");
          return;
        }
      }
      if (e.key === "g") pendingG.current = true;
      else if (e.key === "?") setHelp(true);
      else if (openId || mode !== "queue") return;
      else if (e.key === "j") setSelected((s) => Math.min(s + 1, rows.length - 1));
      else if (e.key === "k") setSelected((s) => Math.max(s - 1, 0));
      else if (e.key === "Enter" && rows[selected]) open(rows[selected].id);
      else if (e.key === "c" && rows[selected]) void copySuggested(rows[selected].id);
      else if (e.key === "o" && rows[selected]?.url) window.open(rows[selected].url!, "_blank", "noopener");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rows, selected, openId, mode, open, copySuggested]);

  const sync = async () => {
    setErr(null);
    try {
      await api.monitor.sync(tid);
      setNote("Sync queued");
      window.setTimeout(load, 2000);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };

  const loadDemo = async () => {
    try {
      await api.monitor.loadDemo(tid);
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };

  const connectSlack = async () => {
    try {
      const { url } = await api.monitor.slackAuthorize(tid);
      window.open(url, "_blank", "noopener");
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };

  const shortcuts = (
    <Dialog open={help} onClose={() => setHelp(false)} title="Keyboard shortcuts"
            actions={[{ label: "Close", variant: "primary", onClick: () => setHelp(false) }]}>
      <dl className="monitor-keys">
        {[["j / k", "Move through the queue"], ["Enter", "Open the case"], ["c", "Copy the suggested draft"],
          ["o", "Open in the case system"], ["g then q", "Back to the queue"], ["?", "This list"]].map(([k, d]) => (
          <div key={k}><dt><kbd>{k}</kbd></dt><dd>{d}</dd></div>
        ))}
      </dl>
    </Dialog>
  );

  if (openId) {
    return (
      <div className="monitor">
        <CaseView caseId={openId} tenantId={tid} slackConnected={!!slack?.connected}
                  onBack={() => { open(null); load(); }} onConnectSlack={connectSlack} />
        {shortcuts}
      </div>
    );
  }

  const failed = queue?.last_sync?.status === "failed";
  const errors = queue?.last_sync?.errors ?? [];
  const hasDemo = all.some((c) => c.source === "demo");

  return (
    <div className="monitor">
      <header className="monitor-head">
        <div>
          <h1>Case Monitor</h1>
          <p>
            {queue?.running ? "Sync running…" : queue?.last_sync
              ? `Last sync ${formatTime(queue.last_sync.started_at)}`
              : "Not synced yet"}
          </p>
        </div>
        <div className="monitor-actions">
          <Button size="sm" variant={mode === "queue" ? "secondary" : "ghost"} onClick={() => setMode("queue")}>Queue</Button>
          <Button size="sm" variant={mode === "digest" ? "secondary" : "ghost"} onClick={() => setMode("digest")}>Digest</Button>
          {isOwner && (
            <Button size="sm" variant={mode === "settings" ? "secondary" : "ghost"} onClick={() => setMode("settings")}>Settings</Button>
          )}
          {slack?.configured && (
            <Button size="sm" variant="ghost" onClick={connectSlack}>
              {slack.connected ? "Reconnect Slack" : "Connect Slack"}
            </Button>
          )}
          <Button size="sm" variant="ghost" aria-label="Keyboard shortcuts" title="Keyboard shortcuts" onClick={() => setHelp(true)}>?</Button>
          <Button size="sm" variant="primary" loading={queue?.running} onClick={sync}>Refresh now</Button>
        </div>
      </header>

      {err && <Banner tone="exception" title={err} />}
      {note && <p className="monitor-note" role="status">{note}</p>}
      {failed && <Banner tone="exception" title="Last sync failed" detail={errors[0]?.error} />}
      {!failed && errors.length > 0 && (
        <details className="monitor-errors">
          <summary>{errors.length} item{errors.length === 1 ? "" : "s"} could not be read in the last sync</summary>
          <ul>
            {errors.map((e, i) => (
              <li key={i}>
                {e.case_number && <strong>Case {e.case_number}: </strong>}
                {e.error}
                {e.url && <> (<a href={e.url} target="_blank" rel="noreferrer">link</a>)</>}
              </li>
            ))}
          </ul>
        </details>
      )}

      {mode === "settings" ? (
        <MonitorSettings tenantId={tid} />
      ) : mode === "digest" ? (
        <section className="monitor-digest">
          <h2>What changed in the last sync</h2>
          <p className="monitor-muted">{digest?.since ? formatTime(digest.since) : "No completed sync yet"}</p>
          {digest && digest.events.length === 0 && <p>Nothing new.</p>}
          {Object.keys(EVENT_TEXT).map((kind) => {
            const events = digest?.events.filter((e) => e.kind === kind) ?? [];
            if (!events.length) return null;
            return (
              <div key={kind}>
                <h3>{EVENT_TEXT[kind]} ({events.length})</h3>
                <ul>
                  {events.map((e) => (
                    <li key={e.id}>
                      <button type="button" className="monitor-link" onClick={() => open(e.case_id)}>
                        {e.case_number || e.case_id}
                      </button>{" "}{e.subject}
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
          <Button size="sm" onClick={() => window.print()}>Print</Button>
        </section>
      ) : !queue ? (
        <Skeleton variant="row" lines={6} />
      ) : all.length === 0 ? (
        <EmptyState title="No cases yet"
                    body="Connect your case system in Settings and run a sync, or try it with demo data."
                    action={<Button variant="primary" onClick={loadDemo}>Load demo data</Button>} />
      ) : (
        <>
          <div className="monitor-tiles">
            <StatTile value={queue.tiles.total} label="Total" />
            <StatTile value={queue.tiles.my_turn} label="My turn now" />
            <StatTile value={queue.tiles.mismatches} label="Mismatches" tone={queue.tiles.mismatches ? "warn" : "neutral"} />
            <StatTile value={queue.tiles.missing_link} label="Missing link" tone={queue.tiles.missing_link ? "warn" : "neutral"} />
            <StatTile value={queue.tiles.overdue} label="Overdue" tone={queue.tiles.overdue ? "exception" : "neutral"} />
            <StatTile value={queue.tiles.critical} label="Critical" tone={queue.tiles.critical ? "exception" : "neutral"} />
            <Sparkline points={queue.trend} />
          </div>

          <div className="monitor-tabs" role="tablist" aria-label="Case queues">
            {tabs.map((t) => (
              <button key={t.key} type="button" role="tab" aria-selected={tab === t.key}
                      className={tab === t.key ? "is-active" : ""}
                      onClick={() => { setTab(t.key); setSelected(0); }}>
                {t.label} <span>{count(t.key)}</span>
              </button>
            ))}
          </div>

          <div className="monitor-filters">
            <Input aria-label="Search cases" placeholder="Search" value={filters.search}
                   onChange={(e) => updateFilters({ search: e.target.value })} />
            <Select aria-label="Record type" value={filters.recordType} options={choices("All record types", all.map((c) => c.record_type))}
                    onChange={(e) => updateFilters({ recordType: e.target.value })} />
            <Select aria-label="Third party" value={filters.thirdParty} options={choices(`All: ${thirdParty}`, all.flatMap((c) => c.third_parties))}
                    onChange={(e) => updateFilters({ thirdParty: e.target.value })} />
            <Select aria-label="Owner" value={filters.owner} options={choices("All owners", all.map((c) => c.owner))}
                    onChange={(e) => updateFilters({ owner: e.target.value })} />
            <Select aria-label="Level" value={filters.level} options={choices("All levels", ["critical", "high", "medium", "low"])}
                    onChange={(e) => updateFilters({ level: e.target.value })} />
            <label className="monitor-check">
              <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} />
              Show reviewed
            </label>
          </div>

          <table className="monitor-table">
            <thead>
              <tr><th>Level</th><th>Case</th><th>Whose turn</th><th>Next action</th><th>Owner</th></tr>
            </thead>
            <tbody>
              {rows.map((c, i) => (
                <tr key={c.id} className={i === selected ? "is-selected" : ""} onClick={() => open(c.id)}>
                  <td><Tag tone={LEVEL_TONE[c.level]}>{c.level} {c.score}</Tag></td>
                  <td>
                    <button type="button" className="monitor-link" onClick={(e) => { e.stopPropagation(); open(c.id); }}>
                      {c.case_number || "AI case"}
                    </button>
                    <div>{c.subject}</div>
                    {c.duplicate_of && <small>Same issue as other cases: {c.duplicate_of}</small>}
                  </td>
                  <td>
                    {c.ball === "third_party" ? thirdParty : BALL_TEXT[c.ball]}
                    {c.mismatch && <div><Tag tone="warn">Mismatch</Tag></div>}
                    {c.hidden && <div><Tag>Reviewed</Tag></div>}
                  </td>
                  <td>{c.next_action}</td>
                  <td>{c.owner}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <EmptyState title="Nothing here" body="Try another tab or clear your filters." />}
          {hasDemo && (
            <div className="monitor-demo">
              <span>Showing demo data.</span>
              <ConfirmButton size="sm" variant="ghost" label="Clear demo data" title="Remove the demo cases?"
                             body="Real synced cases are not affected." confirmLabel="Remove demo data"
                             onConfirm={async () => { await api.monitor.clearDemo(tid); load(); }} />
            </div>
          )}
        </>
      )}
      {shortcuts}
    </div>
  );
}
