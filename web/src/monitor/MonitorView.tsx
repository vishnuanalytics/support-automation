import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, ConfirmButton, Dialog, EmptyState, Input, Select, Skeleton, Tag, type TagTone } from "../ui";
import { AlertsView, useAlerts } from "./AlertsView";
import { EscalationsView } from "./EscalationsView";
import { SlaReportView } from "./SlaReportView";
import { TechPendingView } from "./TechPendingView";
import { TodayView } from "./TodayView";
import { CaseView } from "./CaseView";
import { FollowUpsView } from "./FollowUpsView";
import { AgentsView } from "./AgentsView";
import { WeeklyView } from "./WeeklyView";
import { MonitorSettings } from "./MonitorSettings";
import { copyText, formatAge, formatTime } from "./util";
import type { Digest, Queue, QueueCase, SlackStatus, Stage } from "./types";
import "./monitor.css";

type Who = "everyone" | "mine" | "agents";
type Filters = {
  waiting: string; recordType: string; bu: string; thirdParty: string; module: string;
  subModule: string; subStatus: string; link: string; channel: string; lastFrom: string;
  owner: string; level: string; search: string;
};
type StageTab = Stage | "all";

const NO_FILTERS: Filters = {
  waiting: "", recordType: "", bu: "", thirdParty: "", module: "", subModule: "", subStatus: "",
  link: "", channel: "", lastFrom: "", owner: "", level: "", search: "",
};
const FILTER_LABEL: Record<keyof Filters, string> = {
  waiting: "Waiting on", recordType: "Record type", bu: "BU", thirdParty: "Aggregator",
  module: "Module", subModule: "Sub-module", subStatus: "Sub-status", link: "Link",
  channel: "Raise channel", lastFrom: "Last response from", owner: "Owner", level: "Level", search: "Search",
};
const FILTER_KEY = "monitor.filters";
const PANEL_KEY = "monitor.panel";
const LINK_TEXT: Record<string, string> = {
  github: "GitHub", slack: "Slack", jira: "Jira", freshchat: "Freshchat", email: "Email", other: "Link",
};
const LEVEL_TONE: Record<string, TagTone> = {
  critical: "exception", high: "warn", medium: "warn-soft", low: "neutral",
};
const EVENT_TEXT: Record<string, string> = {
  needs_action: "Needs action", ready_to_close: "Ready to close", new_case: "New case", new_reply: "New reply",
  newly_overdue: "Newly overdue", newly_stale: "Gone quiet",
  newly_missing_link: "Newly missing link",
};
// Why a case needs action: the groups shown as chips inside Act now.
const REASON_GROUP: Record<string, string> = {
  engineer_replied: "Engineer replied", team_replied: "Team replied", github_closed: "Issue closed",
  third_party_replied: "Aggregator replied", customer_replied: "Merchant replied",
  reply_bug_raised: "Raised as a bug", reply_solution: "Solution shared",
  reply_needs_info: "Needs more details", reply_update: "Aggregator update", reply_no_answer: "No real answer",
  customer_replied_email: "Merchant replied",
  ai_handoff: "AI handed over", dsp_resolved: "DSP resolved",
};

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...fallback, ...JSON.parse(raw) } : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* a remembered view is only a convenience */
  }
}

function matchesLast(c: QueueCase, value: string) {
  if (value === "none") return !c.latest;
  if (value === "real") return c.latest?.party === "third_party" && c.stage === "act";
  if (value === "ack") return c.signal?.code === "third_party_ack";
  return c.latest?.party === value;
}

function matches(c: QueueCase, f: Filters): boolean {
  const q = f.search.trim().toLowerCase();
  return (
    (!f.waiting || c.waiting_raw === f.waiting) &&
    (!f.recordType || c.record_type === f.recordType) &&
    (!f.bu || c.bu === f.bu) &&
    (!f.thirdParty || c.third_parties.includes(f.thirdParty)) &&
    (!f.module || c.module === f.module) &&
    (!f.subModule || c.sub_module === f.subModule) &&
    (!f.subStatus || c.sub_status === f.subStatus) &&
    (!f.link || (f.link === "none" ? c.link_kinds.length === 0 : c.link_kinds.includes(f.link))) &&
    (!f.channel || c.partner_channel === f.channel) &&
    (!f.lastFrom || matchesLast(c, f.lastFrom)) &&
    (!f.owner || c.owner === f.owner) &&
    (!f.level || c.level === f.level) &&
    (!q || `${c.case_number} ${c.subject} ${c.owner}`.toLowerCase().includes(q))
  );
}

const MODES = ["today", "agents", "weekly", "sla", "queue", "followups", "escalations", "engineering", "alerts", "digest", "settings"] as const;
type Mode = (typeof MODES)[number];

const MORE = [
  { value: "queue", label: "All cases" }, { value: "sla", label: "Weekly SLA" },
  { value: "followups", label: "Follow-ups" }, { value: "escalations", label: "Escalations" },
  { value: "engineering", label: "Engineering" }, { value: "alerts", label: "Alerts" },
  { value: "digest", label: "Digest" }, { value: "settings", label: "Settings" },
];

function choices(all: string, values: (string | null)[]) {
  const unique = [...new Set(values.filter(Boolean) as string[])].sort();
  return [{ value: "", label: all }, ...unique.map((v) => ({ value: v, label: v }))];
}

function countBy(values: (string | null)[]) {
  const counts = new Map<string, number>();
  for (const v of values) if (v) counts.set(v, (counts.get(v) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1]);
}

function Breakdown({ cases, active, onPick }: {
  cases: QueueCase[];
  active: Filters;
  onPick: (patch: Partial<Filters>) => void;
}) {
  const dep = cases.filter((c) => c.waiting_raw);
  const waiting = countBy(dep.map((c) => c.waiting_raw)).map(([v]) => v);
  const types = countBy(dep.map((c) => c.record_type)).map(([v]) => v);
  const count = (w: string, t: string) =>
    dep.filter((c) => c.waiting_raw === w && c.record_type === t).length;
  const chips = (title: string, key: keyof Filters, values: (string | null)[]) => (
    <div className="monitor-chips">
      <h2>{title}</h2>
      {countBy(values).slice(0, 8).map(([value, n]) => (
        <button key={value} type="button" className={active[key] === value ? "is-active" : ""}
                aria-pressed={active[key] === value}
                onClick={() => onPick({ [key]: active[key] === value ? "" : value })}>
          {value} <span>{n}</span>
        </button>
      ))}
    </div>
  );
  return (
    <div className="monitor-summary-grid">
      <table className="monitor-matrix">
        <thead>
          <tr><th>Waiting on</th>{types.map((t) => <th key={t}>{t}</th>)}<th>Total</th></tr>
        </thead>
        <tbody>
          {waiting.map((w) => (
            <tr key={w}>
              <th>{w}</th>
              {types.map((t) => (
                <td key={t}>
                  <button type="button" className="monitor-link" disabled={!count(w, t)}
                          aria-label={`${w}, ${t}: ${count(w, t)} cases`}
                          onClick={() => onPick({ waiting: w, recordType: t })}>
                    {count(w, t)}
                  </button>
                </td>
              ))}
              <td>
                <button type="button" className="monitor-link"
                        onClick={() => onPick({ waiting: w, recordType: "" })}>
                  {dep.filter((c) => c.waiting_raw === w).length}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {chips("BU", "bu", dep.map((c) => c.bu))}
      {chips("Aggregator", "thirdParty", dep.flatMap((c) => c.third_parties))}
      {chips("Sub-module", "subModule", dep.map((c) => c.sub_module))}
    </div>
  );
}

function partyText(party: string | undefined, thirdParty: string, customer: string) {
  if (party === "us") return "Agent";
  if (party === "engineering") return "Engineering";
  return party === "customer" ? customer : thirdParty;
}

function hoursSince(at: string) {
  return (Date.now() - new Date(at).getTime()) / 3600000;
}

function FocusCards({ names, cases, customer, active, onPick }: {
  names: string[];
  cases: QueueCase[];
  customer: string;
  active: Filters;
  onPick: (patch: Partial<Filters>) => void;
}) {
  // Support cases mirror their DSP, so count raised cases once.
  return (
    <section className="monitor-focus" aria-label="Focus aggregators">
      {names.map((name) => {
        const own = cases.filter((c) => c.third_parties.includes(name) && c.record_type === "DSP");
        const parts = ([
          ["real", "Real update", own.filter((c) => c.latest?.party === "third_party" && c.stage === "act").length],
          ["customer", `${customer} replied`, own.filter((c) => c.latest?.party === "customer").length],
          ["ack", "Ack only", own.filter((c) => c.signal?.code === "third_party_ack").length],
          ["us", "Agent replied", own.filter((c) => c.latest?.party === "us").length],
          ["none", "No email yet", own.filter((c) => !c.latest).length],
        ] as [string, string, number][]).filter(([, , n]) => n > 0);
        const on = active.thirdParty === name;
        return (
          <article key={name} className={on ? "is-active" : ""}>
            <button type="button" className="monitor-focus-title"
                    onClick={() => onPick({ thirdParty: on ? "" : name, recordType: on ? "" : "DSP", lastFrom: "" })}>
              {name} <span>{own.length} DSP</span>
            </button>
            {parts.map(([key, label, n]) => (
              <button key={key} type="button" className={on && active.lastFrom === key ? "is-active" : ""}
                      onClick={() => onPick({ thirdParty: name, recordType: "DSP", lastFrom: key })}>
                {label} <strong>{n}</strong>
              </button>
            ))}
          </article>
        );
      })}
    </section>
  );
}

function Row({ c, selected, onOpen, thirdParty, customer }: {
  c: QueueCase; selected: boolean; onOpen: () => void; thirdParty: string; customer: string;
}) {
  const meta = [c.record_type, c.waiting_raw && `Waiting on ${c.waiting_raw}`,
                c.sub_status && c.sub_status !== "None" ? c.sub_status : null, c.bu,
                c.third_parties.join(", "), [c.module, c.sub_module].filter(Boolean).join(" / ")]
    .filter(Boolean);
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  return (
    <tr className={selected ? "is-selected" : ""} onClick={onOpen}>
      <td>
        <button type="button" className="monitor-link" onClick={(e) => { e.stopPropagation(); onOpen(); }}>
          {c.case_number || "AI case"}
        </button>
        <div className="monitor-subject">{c.subject}</div>
        <div className="monitor-meta">{meta.join(" · ")}</div>
        <div className="monitor-flags">
          <Tag tone={LEVEL_TONE[c.level]}>{c.level}</Tag>
          {c.latest && ["act", "close", "check"].includes(c.stage) && (
            <Tag tone="warn">Pending {formatAge(hoursSince(c.latest.at))}</Tag>
          )}
          {c.overdue && <Tag tone="exception">Overdue {formatAge(c.overdue_hours)}</Tag>}
          {c.duplicate_of && <Tag>Same issue as another case</Tag>}
          {c.hidden && <Tag>Reviewed</Tag>}
        </div>
      </td>
      <td>
        {c.latest ? (
          <>
            <div className="monitor-latest-head">
              <Tag tone={c.latest.party === "us" ? "neutral" : "warn-soft"}>
                {partyText(c.latest.party, thirdParty, customer)}
              </Tag>{" "}
              {c.latest.who} · {c.latest.source} · {formatAge(hoursSince(c.latest.at))} ago
            </div>
            <div className="monitor-latest">{c.latest.text}</div>
          </>
        ) : (
          <span className="monitor-muted">No response found</span>
        )}
      </td>
      <td>
        {c.signal && <div className="monitor-why">{c.signal.text}</div>}
        <div className="monitor-next-cell">{c.next_action}</div>
      </td>
      <td>{c.owner}{c.mine && <div><Tag>Mine</Tag></div>}</td>
      <td>
        <div className="monitor-open">
          {c.url && <a href={c.url} target="_blank" rel="noreferrer" onClick={stop}>Salesforce</a>}
          {c.related.filter((r) => r.url).map((r) => (
            <a key={r.id} href={r.url!} target="_blank" rel="noreferrer" onClick={stop}>
              {r.record_type || "Case"} {r.case_number}
            </a>
          ))}
          {c.links.map((l) => (
            <a key={l.url} href={l.url} target="_blank" rel="noreferrer" onClick={stop}>
              {LINK_TEXT[l.kind] ?? "Link"}
            </a>
          ))}
        </div>
      </td>
    </tr>
  );
}

export function MonitorView({ tenantId, isOwner }: { tenantId?: string | null; isOwner: boolean }) {
  const tid = tenantId || undefined;
  const [queue, setQueue] = useState<Queue | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [tab, setTab] = useState<StageTab>("act");
  const [who, setWho] = useState<Who>("everyone");
  const [overdueOnly, setOverdueOnly] = useState(false);
  const [reason, setReason] = useState("");
  const [filters, setFilters] = useState<Filters>(() => readJson(FILTER_KEY, NO_FILTERS));
  const [panelOpen, setPanelOpen] = useState(() => readJson(PANEL_KEY, { open: false }).open);
  const [showHidden, setShowHidden] = useState(false);
  const [selected, setSelected] = useState(0);
  const [openId, setOpenId] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("case"),
  );
  const [mode, setModeState] = useState<Mode>(() => {
    const tab = new URLSearchParams(window.location.search).get("tab");
    return MODES.includes(tab as Mode) ? (tab as Mode) : "today";
  });
  const setMode = useCallback((next: Mode) => {
    setModeState(next);
    const url = new URL(window.location.href);
    if (next === "queue") url.searchParams.delete("tab");
    else url.searchParams.set("tab", next);
    window.history.replaceState(null, "", url);
  }, []);
  const [digest, setDigest] = useState<Digest | null>(null);
  const [slack, setSlack] = useState<SlackStatus | null>(null);
  const [help, setHelp] = useState(false);
  const pendingG = useRef(false);
  const alertState = useAlerts(tid, (caseId) => {
    open(caseId);
    if (!caseId) setMode("alerts");
  });

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

  const updateFilters = (patch: Partial<Filters>) => {
    const next = { ...filters, ...patch };
    setFilters(next);
    writeJson(FILTER_KEY, next);
    setSelected(0);
  };

  const togglePanel = () => {
    setPanelOpen(!panelOpen);
    writeJson(PANEL_KEY, { open: !panelOpen });
  };

  const clearAll = () => {
    updateFilters(NO_FILTERS);
    setWho("everyone");
    setOverdueOnly(false);
    setReason("");
  };

  const open = useCallback((id: string | null) => {
    setOpenId(id);
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("case", id);
    else url.searchParams.delete("case");
    window.history.replaceState(null, "", url);
  }, []);

  const all = useMemo(() => queue?.cases ?? [], [queue]);
  const thirdParty = queue?.labels.third_party ?? "Third party";
  const customer = queue?.labels.customer ?? "Customer";

  const reviewedCount = useMemo(() => all.filter((c) => c.hidden).length, [all]);
  const listed = useMemo(() => all.filter((c) => showHidden || !c.hidden), [all, showHidden]);

  // Everything except the stage tab: the tab counts must reflect these.
  const visible = useMemo(() => listed.filter((c) =>
    matches(c, filters) &&
    (who === "everyone" || (who === "mine" ? c.mine : !c.mine)) &&
    (!overdueOnly || c.overdue)), [listed, filters, who, overdueOnly]);

  const stages: { key: StageTab; label: string; hint: string }[] = [
    { key: "act", label: "Act now", hint: "A real update or reply needs us" },
    { key: "close", label: "Ready to close", hint: "Outside work is done: update the merchant and close" },
    { key: "raise", label: "Raise it", hint: "Not raised, or no link recorded" },
    { key: "engineering", label: "Waiting on Engineering", hint: "We spoke last" },
    { key: "third_party", label: `Waiting on ${thirdParty}`, hint: "We spoke last, or ack only" },
    { key: "customer", label: `Waiting on ${customer}`, hint: "Solution given, to confirm" },
    { key: "check", label: "Check by hand", hint: "Can't be read here" },
    ...(all.some((c) => c.stage === "ai")
      ? [{ key: "ai" as StageTab, label: "AI agent", hint: "An AI agent has it" }] : []),
    { key: "all", label: "All", hint: "Every open case" },
  ];
  const inStage = (c: QueueCase, s: StageTab) => s === "all" || c.stage === s;
  const count = (s: StageTab) => visible.filter((c) => inStage(c, s)).length;

  const stageRows = useMemo(() => visible.filter((c) => inStage(c, tab)), [visible, tab]);
  const reasons = tab === "act"
    ? countBy(stageRows.map((c) => REASON_GROUP[c.signal?.code ?? ""] ?? null)) : [];
  const rows = useMemo(() => stageRows.filter((c) =>
    !reason || REASON_GROUP[c.signal?.code ?? ""] === reason), [stageRows, reason]);

  const chooseTab = (s: StageTab) => {
    setTab(s);
    setReason("");
    setSelected(0);
  };

  // Back to the list at once, so working through it goes straight to the next case.
  const onReviewed = (id: string) => {
    setQueue((q) => q && { ...q, cases: q.cases.map((c) => (c.id === id ? { ...c, hidden: true } : c)) });
    setNote("Marked reviewed");
    open(null);
    load();
    alertState.refresh();
  };

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
  }, [rows, selected, openId, mode, setMode, open, copySuggested]);

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
                  onBack={() => { open(null); load(); }} onReviewed={() => onReviewed(openId)}
                  onConnectSlack={connectSlack} />
        {shortcuts}
      </div>
    );
  }

  const failed = queue?.last_sync?.status === "failed";
  const errors = queue?.last_sync?.errors ?? [];
  const hasDemo = all.some((c) => c.source === "demo");
  const activeFilters = (Object.keys(filters) as (keyof Filters)[]).filter((k) => filters[k] && k !== "search");

  return (
    <div className="monitor">
      <header className="monitor-head">
        <div>
          <h1>Case Monitor</h1>
          <p>
            {all.length - reviewedCount} open{reviewedCount ? ` · ${reviewedCount} reviewed` : ""} · {queue?.running ? "Sync running…" : queue?.last_sync
              ? `Synced ${formatTime(queue.last_sync.started_at)}` : "Not synced yet"}
            {queue?.slack_via_claude && (
              <> · Slack read {queue.slack_read_at ? formatTime(queue.slack_read_at) : "not yet"}
                <span title='Say "refresh Slack" in a Claude session to update Slack threads.'> (say "refresh Slack" in Claude to update)</span>
              </>
            )}
          </p>
        </div>
        <div className="monitor-actions">
          <Input aria-label="Find a case" placeholder="Find a case" value={filters.search}
                 onChange={(e) => { updateFilters({ search: e.target.value }); chooseTab("all"); setMode("queue"); }} />
          <Button size="sm" variant={mode === "today" ? "secondary" : "ghost"} onClick={() => setMode("today")}>Needs Me</Button>
          <Button size="sm" variant={mode === "agents" ? "secondary" : "ghost"} onClick={() => setMode("agents")}>Agents</Button>
          <Button size="sm" variant={mode === "weekly" ? "secondary" : "ghost"} onClick={() => setMode("weekly")}>Wednesday Report</Button>
          <Select aria-label="More views" value={MORE.some((m) => m.value === mode) ? mode : ""}
                  onChange={(e) => e.target.value && setMode(e.target.value as Mode)}
                  options={[{ value: "", label: alertState.unread ? `Older pages (${alertState.unread})` : "Older pages" },
                    ...MORE.filter((m) => isOwner || m.value !== "settings").map((m) => (
                      m.value === "alerts" && alertState.unread
                        ? { ...m, label: `Alerts (${alertState.unread})` } : m))]} />
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

      {mode === "today" ? (
        <TodayView tenantId={tid} onOpen={open} thirdParty={thirdParty} customer={customer} />
      ) : mode === "agents" ? (
        <AgentsView tenantId={tid} onOpen={open} />
      ) : mode === "weekly" ? (
        <WeeklyView tenantId={tid} onOpen={open} thirdParty={thirdParty} customer={customer} />
      ) : mode === "sla" ? (
        <SlaReportView tenantId={tid} onOpen={open} />
      ) : mode === "settings" ? (
        <MonitorSettings tenantId={tid} />
      ) : mode === "followups" ? (
        <FollowUpsView tenantId={tid} onOpen={open} />
      ) : mode === "escalations" ? (
        <EscalationsView tenantId={tid} onOpen={open} />
      ) : mode === "engineering" ? (
        <TechPendingView tenantId={tid} onOpen={open} />
      ) : mode === "alerts" ? (
        <AlertsView state={alertState} onOpen={open} />
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
          <div className="monitor-stages" role="tablist" aria-label="Who acts next">
            {stages.map((s) => (
              <button key={s.key} type="button" role="tab" aria-selected={tab === s.key}
                      className={tab === s.key ? "is-active" : ""} onClick={() => chooseTab(s.key)}>
                <strong>{count(s.key)}</strong>
                <span>{s.label}</span>
                <small>{s.hint}</small>
              </button>
            ))}
          </div>

          {queue.focus_partners.length > 0 && (
            <FocusCards names={queue.focus_partners} cases={listed} customer={customer} active={filters}
                        onPick={(patch) => { updateFilters(patch); chooseTab("all"); }} />
          )}

          <div className="monitor-bar">
            <Input aria-label="Search cases" placeholder="Search case, subject or owner" value={filters.search}
                   onChange={(e) => updateFilters({ search: e.target.value })} />
            {queue.has_lead && (
              <div className="monitor-switch" role="group" aria-label="Whose cases">
                {(["everyone", "mine", "agents"] as Who[]).map((w) => (
                  <button key={w} type="button" aria-pressed={who === w} className={who === w ? "is-active" : ""}
                          onClick={() => { setWho(w); setSelected(0); }}>
                    {w === "everyone" ? "Everyone" : w === "mine" ? "Mine" : "Agents'"}
                  </button>
                ))}
              </div>
            )}
            <div className="monitor-switch">
              <button type="button" aria-pressed={overdueOnly} className={overdueOnly ? "is-active" : ""}
                      onClick={() => { setOverdueOnly(!overdueOnly); setSelected(0); }}>
                Overdue only
              </button>
            </div>
            <Button size="sm" variant="secondary" aria-expanded={panelOpen} onClick={togglePanel}>
              Filters{activeFilters.length ? ` (${activeFilters.length})` : ""}
            </Button>
            <label className="monitor-check">
              <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} />
              Show reviewed
            </label>
          </div>

          {activeFilters.length > 0 && (
            <div className="monitor-active">
              {activeFilters.map((k) => (
                <button key={k} type="button" aria-label={`Remove ${FILTER_LABEL[k]} filter`}
                        onClick={() => updateFilters({ [k]: "" })}>
                  {FILTER_LABEL[k]}: {filters[k]} ×
                </button>
              ))}
              <Button size="sm" variant="ghost" onClick={clearAll}>Clear all</Button>
            </div>
          )}

          {panelOpen && (
            <section className="monitor-panel" aria-label="Filters and breakdown">
              <div className="monitor-filters">
                <Select aria-label="Waiting on" value={filters.waiting} options={choices("Waiting on: all", all.map((c) => c.waiting_raw))}
                        onChange={(e) => updateFilters({ waiting: e.target.value })} />
                <Select aria-label="Record type" value={filters.recordType} options={choices("Record type: all", all.map((c) => c.record_type))}
                        onChange={(e) => updateFilters({ recordType: e.target.value })} />
                <Select aria-label="BU" value={filters.bu} options={choices("BU: all", all.map((c) => c.bu))}
                        onChange={(e) => updateFilters({ bu: e.target.value })} />
                <Select aria-label={thirdParty} value={filters.thirdParty} options={choices(`${thirdParty}: all`, all.flatMap((c) => c.third_parties))}
                        onChange={(e) => updateFilters({ thirdParty: e.target.value })} />
                <Select aria-label="Module" value={filters.module} options={choices("Module: all", all.map((c) => c.module))}
                        onChange={(e) => updateFilters({ module: e.target.value, subModule: "" })} />
                <Select aria-label="Sub-module" value={filters.subModule}
                        options={choices("Sub-module: all", all.filter((c) => !filters.module || c.module === filters.module).map((c) => c.sub_module))}
                        onChange={(e) => updateFilters({ subModule: e.target.value })} />
                <Select aria-label="Sub-status" value={filters.subStatus} options={choices("Sub-status: all", all.map((c) => c.sub_status))}
                        onChange={(e) => updateFilters({ subStatus: e.target.value })} />
                <Select aria-label="Link" value={filters.link}
                        options={[{ value: "", label: "Link: all" }, { value: "none", label: "No link" },
                                  ...Object.entries(LINK_TEXT).map(([value, label]) => ({ value, label }))]}
                        onChange={(e) => updateFilters({ link: e.target.value })} />
                <Select aria-label="Raise channel" value={filters.channel} options={choices("Raise channel: all", all.map((c) => c.partner_channel))}
                        onChange={(e) => updateFilters({ channel: e.target.value })} />
                <Select aria-label="Last response from" value={filters.lastFrom}
                        options={[{ value: "", label: "Last response: all" }, { value: "us", label: "Agent" },
                                  { value: "real", label: `${thirdParty}: real update` }, { value: "ack", label: `${thirdParty}: ack only` },
                                  { value: "customer", label: customer },
                                  { value: "engineering", label: "Engineering" }, { value: "none", label: "None found" }]}
                        onChange={(e) => updateFilters({ lastFrom: e.target.value })} />
                <Select aria-label="Owner" value={filters.owner} options={choices("Owner: all", all.map((c) => c.owner))}
                        onChange={(e) => updateFilters({ owner: e.target.value })} />
                <Select aria-label="Level" value={filters.level} options={choices("Level: all", ["critical", "high", "medium", "low"])}
                        onChange={(e) => updateFilters({ level: e.target.value })} />
              </div>
              <Breakdown cases={listed} active={filters} onPick={updateFilters} />
            </section>
          )}

          {reasons.length > 0 && (
            <div className="monitor-chips monitor-reasons" role="group" aria-label="Why action is needed">
              <button type="button" className={!reason ? "is-active" : ""} onClick={() => { setReason(""); setSelected(0); }}>
                All <span>{stageRows.length}</span>
              </button>
              {reasons.map(([value, n]) => (
                <button key={value} type="button" aria-pressed={reason === value}
                        className={reason === value ? "is-active" : ""}
                        onClick={() => { setReason(reason === value ? "" : value); setSelected(0); }}>
                  {value} <span>{n}</span>
                </button>
              ))}
            </div>
          )}

          <table className="monitor-table">
            <thead>
              <tr><th>Case</th><th>Last response</th><th>Next step</th><th>Owner</th><th>Open</th></tr>
            </thead>
            <tbody>
              {rows.map((c, i) => (
                <Row key={c.id} c={c} selected={i === selected} onOpen={() => open(c.id)}
                     thirdParty={thirdParty} customer={customer} />
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
