import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Banner, Button, ConfirmButton, EmptyState, Input, Skeleton } from "../ui";
import { CaseView } from "./CaseView";
import { CasesView } from "./CasesView";
import { MonitorSettings } from "./MonitorSettings";
import { NotificationsView, useNotifications } from "./Notifications";
import { TodoView } from "./TodoView";
import { useEscalationMarks } from "./marks";
import { formatTime } from "./util";
import type { Queue, SlackStatus } from "./types";
import "./monitor.css";

type Page = "todo" | "notifications" | "cases" | "settings";
const PAGES: Page[] = ["todo", "notifications", "cases", "settings"];
// New replies arrive with each sync, so look for them every couple of minutes.
const POLL_MS = 120_000;

function readParam(name: string) {
  return new URLSearchParams(window.location.search).get(name);
}

function writeParam(name: string, value: string | null) {
  const url = new URL(window.location.href);
  if (value) url.searchParams.set(name, value);
  else url.searchParams.delete(name);
  window.history.replaceState(null, "", url);
}

export function MonitorView({ tenantId, isOwner }: { tenantId?: string | null; isOwner: boolean }) {
  const tid = tenantId || undefined;
  const [queue, setQueue] = useState<Queue | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [slack, setSlack] = useState<SlackStatus | null>(null);
  const [find, setFind] = useState("");
  const [openId, setOpenId] = useState<string | null>(() => readParam("case"));
  const [page, setPageState] = useState<Page>(() => {
    const tab = readParam("tab") as Page;
    return PAGES.includes(tab) && (tab !== "settings" || isOwner) ? tab : "todo";
  });
  const marks = useEscalationMarks(tid);

  const setPage = (p: Page) => {
    setPageState(p);
    writeParam("tab", p === "todo" ? null : p);
  };
  const open = useCallback((id: string | null) => {
    setOpenId(id);
    writeParam("case", id);
  }, []);

  const load = useCallback(() => {
    api.monitor.queue(tid).then(setQueue).catch((e: ApiError) => setErr(e.message));
    api.monitor.slackStatus(tid).then(setSlack).catch(() => {});
  }, [tid]);
  useEffect(load, [load]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      api.monitor.queue(tid).then(setQueue).catch(() => {});
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [tid]);

  // A running sync finishes in the worker, so poll until it does.
  useEffect(() => {
    if (!queue?.running) return;
    const timer = window.setTimeout(load, 5000);
    return () => window.clearTimeout(timer);
  }, [queue, load]);

  const cases = useMemo(() => queue?.cases ?? [], [queue]);
  const notes = useNotifications(tid, queue ? cases : null, (caseId) => {
    if (caseId) open(caseId);
    else setPage("notifications");
  });
  const labels = { thirdParty: queue?.labels.third_party ?? "Aggregator", customer: queue?.labels.customer ?? "Merchant" };

  const sync = async () => {
    setErr(null);
    try {
      await api.monitor.sync(tid);
      setNote("Refresh started. This takes about a minute.");
      window.setTimeout(load, 2000);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };

  const goTo = () => {
    const q = find.trim().replace(/^0+/, "");
    const hit = cases.find((c) => (c.case_number ?? "").replace(/^0+/, "") === q);
    if (hit) {
      open(hit.id);
      setFind("");
      setErr(null);
    } else setErr(`Case ${find.trim()} isn't in the open cases from the last refresh`);
  };

  const connectSlack = async () => {
    try {
      const { url } = await api.monitor.slackAuthorize(tid);
      window.open(url, "_blank", "noopener");
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };

  if (openId) {
    return (
      <div className="monitor">
        <CaseView caseId={openId} tenantId={tid} slackConnected={!!slack?.connected}
                  onBack={() => { open(null); load(); }}
                  onReviewed={() => { setNote("Marked reviewed"); open(null); load(); }}
                  onConnectSlack={connectSlack} />
      </div>
    );
  }

  const last = queue?.last_sync;
  const failed = last?.status === "failed";
  const unread = last?.errors ?? [];
  const hasDemo = cases.some((c) => c.source === "demo");
  const openCount = cases.filter((c) => !c.hidden).length;

  return (
    <div className="monitor">
      <header className="monitor-head">
        <div>
          <h1>Case Monitor</h1>
          <p>
            {openCount} open · {queue?.running ? "Refreshing…" : last ? `Refreshed ${formatTime(last.started_at)}` : "Not refreshed yet"}
          </p>
        </div>
        <div className="monitor-actions">
          <Input aria-label="Open a case by number" placeholder="Open case number" value={find}
                 onChange={(e) => setFind(e.target.value)} onKeyDown={(e) => e.key === "Enter" && find.trim() && goTo()} />
          {isOwner && (
            <Button size="sm" variant={page === "settings" ? "secondary" : "ghost"} onClick={() => setPage(page === "settings" ? "todo" : "settings")}>
              Settings
            </Button>
          )}
          <Button size="sm" variant="primary" loading={queue?.running} onClick={sync}>Refresh now</Button>
        </div>
      </header>

      <nav className="monitor-pages" aria-label="Sections">
        {([["todo", "To-do", "What needs you today"],
           ["notifications", "Notifications", "Aggregator and engineering replies"],
           ["cases", "All cases", "Search and filter every open case"]] as const).map(([p, label, hint]) => (
          <button key={p} type="button" aria-current={page === p ? "page" : undefined}
                  className={page === p ? "is-active" : ""} onClick={() => setPage(p)}>
            <span>{label}
              {p === "cases" && queue && <em>{openCount}</em>}
              {p === "notifications" && notes.unread > 0 && <em className="is-new" aria-label={`${notes.unread} new`}>{notes.unread}</em>}
            </span>
            <small>{p === "notifications" && notes.pending ? `${notes.pending} pending with us` : hint}</small>
          </button>
        ))}
      </nav>

      {err && <Banner tone="exception" title={err} />}
      {note && <p className="monitor-note" role="status">{note}</p>}
      {failed && (
        <Banner tone="warn" title="The last refresh didn't finish"
                detail={`${unread[0]?.error ?? "Unknown error"}. You're seeing data from the refresh before it. Press Refresh now to try again.`} />
      )}
      {!failed && unread.length > 0 && (
        <details className="monitor-errors">
          <summary>{unread.length} linked thread{unread.length === 1 ? "" : "s"} couldn't be read in the last refresh</summary>
          <ul>
            {unread.map((e, i) => (
              <li key={i}>
                {e.case_number && <strong>Case {e.case_number}: </strong>}{e.error}
                {e.url && <> (<a href={e.url} target="_blank" rel="noreferrer">link</a>)</>}
              </li>
            ))}
          </ul>
        </details>
      )}

      {page === "settings" ? (
        <MonitorSettings tenantId={tid} />
      ) : !queue ? (
        <Skeleton variant="row" lines={6} />
      ) : cases.length === 0 ? (
        <EmptyState title="No cases yet" body="Connect your case system in Settings and press Refresh now." />
      ) : page === "notifications" ? (
        <NotificationsView state={notes} onOpen={open} thirdParty={labels.thirdParty} />
      ) : page === "todo" ? (
        <TodoView cases={cases} tenantId={tid} onOpen={open} labels={labels} marks={marks} />
      ) : (
        <CasesView cases={cases} onOpen={open} labels={labels} />
      )}

      {hasDemo && (
        <div className="monitor-demo">
          <span>Showing demo data.</span>
          <ConfirmButton size="sm" variant="ghost" label="Clear demo data" title="Remove the demo cases?"
                         body="Real synced cases are not affected." confirmLabel="Remove demo data"
                         onConfirm={async () => { await api.monitor.clearDemo(tid); load(); }} />
        </div>
      )}
    </div>
  );
}
