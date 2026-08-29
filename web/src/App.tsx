import { useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "./supabase";
import { api } from "./api";
import { Login } from "./auth/Login";
import { FlowList } from "./flows/FlowList";
import { FlowEditor } from "./flows/FlowEditor";
import { RunsView } from "./runs/RunsView";
import { KnowledgeView } from "./kb/KnowledgeView";
import { RulesView } from "./rules/RulesView";
import { TeamView } from "./team/TeamView";

type View = "editor" | "runs" | "knowledge" | "rules" | "team";

export function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [role, setRole] = useState<string | null>(null);
  const [memberships, setMemberships] = useState<number | null>(null);
  const [flowId, setFlowId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [view, setView] = useState<View>("editor");

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session));
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, []);

  useEffect(() => {
    if (!session) return;
    const rank: Record<string, number> = { owner: 3, editor: 2, viewer: 1 };
    // claim any pending invites for this email first, then read memberships
    api.acceptInvitations().catch(() => {}).finally(() => {
      api
        .listTenants()
        .then((rows) => {
          setMemberships(rows.length);
          const best = rows
            .map((r) => r.role)
            .sort((a, b) => (rank[b] ?? 0) - (rank[a] ?? 0))[0];
          setRole(best ?? null);
        })
        .catch(() => {
          setMemberships(0);
          setRole(null);
        });
    });
  }, [session]);

  const canEdit = role === "owner" || role === "editor";
  const isOwner = role === "owner";

  if (session === undefined) return <div style={{ padding: 20 }}>…</div>;
  if (session === null) return <Login />;

  if (memberships === 0) {
    return (
      <div className="login col">
        <h1>No workspace yet</h1>
        <p className="muted">
          You're signed in as <strong>{session.user.email}</strong> but not a
          member of any workspace. Ask an owner to invite this email address.
        </p>
        <button onClick={() => supabase.auth.signOut()}>sign out</button>
      </div>
    );
  }

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
            <button className={view === "rules" ? "primary" : ""} onClick={() => setView("rules")}>
              Rules
            </button>
            {isOwner && (
              <button className={view === "team" ? "primary" : ""} onClick={() => setView("team")}>
                Team
              </button>
            )}
          </div>
          <div className="row" style={{ gap: 6 }}>
            {role && !canEdit && (
              <span className="pill" title="your access is view-only">view-only</span>
            )}
            <button onClick={() => supabase.auth.signOut()} title={session.user.email ?? ""}>
              sign out
            </button>
          </div>
        </div>
        {view === "editor" && (
          <FlowList
            key={reloadKey}
            activeId={flowId}
            canEdit={canEdit}
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
        {view === "rules" && (
          <div className="muted">
            structured <code>when → then</code> rules a <code>policy_gate</code> node
            evaluates; <code>task</code> outcomes route through Slack approval
          </div>
        )}
      </div>
      <div className={view === "editor" && flowId ? "editor" : "pane"}>
        {view === "team" ? (
          <TeamView />
        ) : view === "rules" ? (
          <RulesView />
        ) : view === "knowledge" ? (
          <KnowledgeView />
        ) : view === "runs" ? (
          <RunsView />
        ) : flowId ? (
          <FlowEditor
            key={flowId}
            flowId={flowId}
            canEdit={canEdit}
            onSaved={() => setReloadKey((k) => k + 1)}
            onDeleted={() => {
              setFlowId(null);
              setReloadKey((k) => k + 1);
            }}
          />
        ) : (
          <div className="pane-empty muted">select or create a flow</div>
        )}
      </div>
    </div>
  );
}
