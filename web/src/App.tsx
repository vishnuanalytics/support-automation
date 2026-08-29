import { useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "./supabase";
import { Login } from "./auth/Login";
import { FlowList } from "./flows/FlowList";
import { FlowEditor } from "./flows/FlowEditor";
import { RunsView } from "./runs/RunsView";
import { KnowledgeView } from "./kb/KnowledgeView";

export function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [flowId, setFlowId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [view, setView] = useState<"editor" | "runs" | "knowledge">("editor");

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session));
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, []);

  if (session === undefined) return <div style={{ padding: 20 }}>…</div>;
  if (session === null) return <Login />;

  return (
    <div className="shell">
      <div className="sidebar col">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="row" style={{ gap: 4 }}>
            <button className={view === "editor" ? "primary" : ""} onClick={() => setView("editor")}>
              Editor
            </button>
            <button className={view === "runs" ? "primary" : ""} onClick={() => setView("runs")}>
              Runs
            </button>
            <button className={view === "knowledge" ? "primary" : ""} onClick={() => setView("knowledge")}>
              Knowledge
            </button>
          </div>
          <button onClick={() => supabase.auth.signOut()} title={session.user.email ?? ""}>
            sign out
          </button>
        </div>
        {view === "editor" && (
          <FlowList
            key={reloadKey}
            activeId={flowId}
            onSelect={setFlowId}
            onCreated={(id) => {
              setReloadKey((k) => k + 1);
              setFlowId(id);
            }}
          />
        )}
        {view === "runs" && <div className="muted">observability — recent interpreter runs across your tenants</div>}
        {view === "knowledge" && (
          <div className="muted">
            internal SOPs &amp; runbooks per team — a <code>kb_lookup</code> node
            in a flow consults a collection at a checkpoint
          </div>
        )}
      </div>
      <div className="editor">
        {view === "knowledge" ? (
          <KnowledgeView />
        ) : view === "runs" ? (
          <RunsView />
        ) : flowId ? (
          <FlowEditor
            key={flowId}
            flowId={flowId}
            onSaved={() => setReloadKey((k) => k + 1)}
            onDeleted={() => {
              setFlowId(null);
              setReloadKey((k) => k + 1);
            }}
          />
        ) : (
          <div style={{ display: "grid", placeItems: "center" }} className="muted">
            select or create a flow
          </div>
        )}
      </div>
    </div>
  );
}
