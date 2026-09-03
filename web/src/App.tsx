import { useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "./supabase";
import { api } from "./api";
import { Login } from "./auth/Login";
import { FlowList } from "./flows/FlowList";
import { FlowEditor } from "./flows/FlowEditor";
import { RunsView } from "./runs/RunsView";
import { ReviewView } from "./review/ReviewView";
import { TraceView } from "./trace/TraceView";
import { KnowledgeView } from "./kb/KnowledgeView";
import { RulesView } from "./rules/RulesView";
import { TeamView } from "./team/TeamView";
import { ChannelsView } from "./channels/ChannelsView";
import { ConnectionsView } from "./channels/ConnectionsView";
import { FlowGuideView } from "./guide/FlowGuideView";
import { BillingView } from "./billing/BillingView";
import { ActivityView } from "./activity/ActivityView";

type View =
  | "editor"
  | "runs"
  | "review"
  | "trace"
  | "knowledge"
  | "rules"
  | "guide"
  | "team"
  | "channels"
  | "connections"
  | "billing"
  | "activity";

type TenantMembership = { tenant_id: string; role: string; name?: string | null };

function tenantLabel(t: TenantMembership): string {
  return t.name || `workspace ${t.tenant_id.slice(0, 8)}`;
}

export function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [tenants, setTenants] = useState<TenantMembership[] | null>(null);
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [flowId, setFlowId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [view, setView] = useState<View>("editor");

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session));
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, []);

  const storageKey = session?.user.id ? `workspace:${session.user.id}` : null;

  function applyTenants(rows: TenantMembership[]) {
    setTenants(rows);
    if (rows.length === 0) {
      setTenantId(null);
      return;
    }
    if (rows.length === 1) {
      setTenantId(rows[0].tenant_id);
      return;
    }
    // 2+ workspaces — a stored choice wins if it's still one you're a member
    // of; otherwise show the picker (tenantId stays null).
    const stored = storageKey ? localStorage.getItem(storageKey) : null;
    setTenantId(stored && rows.some((r) => r.tenant_id === stored) ? stored : null);
  }

  const load = () => {
    setTenants(null);
    // claim any pending invites for this email first, then read memberships
    api.acceptInvitations().catch(() => {}).finally(() => {
      api.listTenants().then(applyTenants).catch(() => setTenants([]));
    });
  };

  useEffect(() => {
    if (session) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session]);

  function chooseTenant(id: string) {
    setTenantId(id);
    if (storageKey) localStorage.setItem(storageKey, id);
    setFlowId(null);
    setReloadKey((k) => k + 1);
  }

  const current = tenants?.find((t) => t.tenant_id === tenantId) ?? null;
  const role = current?.role ?? null;
  const canEdit = role === "owner" || role === "editor";
  const isOwner = role === "owner";

  async function createWorkspace() {
    const name = prompt("Workspace name (e.g. your company)")?.trim();
    if (!name) return;
    try {
      const created = await api.createTenant(name);
      if (storageKey) localStorage.setItem(storageKey, created.tenant_id);
      load();
    } catch (e) {
      alert(String(e));
    }
  }

  if (session === undefined) return <div style={{ padding: 20 }}>…</div>;
  if (session === null) return <Login />;
  if (tenants === null) return <div style={{ padding: 20 }}>…</div>;

  if (tenants.length === 0) {
    return (
      <div className="login col">
        <h1>Set up your workspace</h1>
        <p className="muted">
          You're signed in as <strong>{session.user.email}</strong>. Create a
          workspace to start building flows — or ask an owner to invite this
          email to an existing one.
        </p>
        <button className="primary" onClick={createWorkspace}>
          Create a workspace
        </button>
        <button onClick={() => supabase.auth.signOut()}>sign out</button>
      </div>
    );
  }

  if (!tenantId) {
    return (
      <div className="login col">
        <h1>Choose a workspace</h1>
        <p className="muted">
          You're signed in as <strong>{session.user.email}</strong>, a member of
          {" "}{tenants.length} workspaces. Pick one to continue — you can switch
          later from the header.
        </p>
        <div className="col" style={{ gap: 8 }}>
          {tenants.map((t) => (
            <button key={t.tenant_id} onClick={() => chooseTenant(t.tenant_id)}>
              {tenantLabel(t)} <span className="muted">— {t.role}</span>
            </button>
          ))}
        </div>
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
            <button className={view === "activity" ? "primary" : ""} onClick={() => setView("activity")}>
              Activity
            </button>
            <button className={view === "review" ? "primary" : ""} onClick={() => setView("review")}>
              Approvals
            </button>
            <button className={view === "trace" ? "primary" : ""} onClick={() => setView("trace")}>
              Trace
            </button>
            <button className={view === "knowledge" ? "primary" : ""} onClick={() => setView("knowledge")}>
              Knowledge
            </button>
            <button className={view === "rules" ? "primary" : ""} onClick={() => setView("rules")}>
              Rules
            </button>
            <button className={view === "guide" ? "primary" : ""} onClick={() => setView("guide")}>
              Guide
            </button>
            {isOwner && (
              <button className={view === "team" ? "primary" : ""} onClick={() => setView("team")}>
                Team
              </button>
            )}
            {isOwner && (
              <button className={view === "channels" ? "primary" : ""} onClick={() => setView("channels")}>
                Channels
              </button>
            )}
            {isOwner && (
              <button className={view === "connections" ? "primary" : ""} onClick={() => setView("connections")}>
                Connections
              </button>
            )}
            {isOwner && (
              <button className={view === "billing" ? "primary" : ""} onClick={() => setView("billing")}>
                Billing
              </button>
            )}
          </div>
          <div className="row" style={{ gap: 6 }}>
            {tenants.length > 1 && (
              <select
                value={tenantId}
                onChange={(e) => chooseTenant(e.target.value)}
                title="switch workspace"
              >
                {tenants.map((t) => (
                  <option key={t.tenant_id} value={t.tenant_id}>
                    {tenantLabel(t)}
                  </option>
                ))}
              </select>
            )}
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
            tenantId={tenantId}
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
        {view === "activity" && <div className="muted">who did what — flow publishes/rollbacks, approvals, membership, connections</div>}
        {view === "trace" && <div className="muted">one timeline per Case — jobs, runs, nodes and errors, in order</div>}
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
        {view === "guide" && (
          <div className="muted">
            how an inbound email becomes a handled Salesforce Case — the live
            flow, end to end
          </div>
        )}
        {view === "billing" && (
          <div className="muted">
            usage &amp; a notional cost estimate for this workspace, against its
            plan quota — no payment processing is wired up yet
          </div>
        )}
      </div>
      <div className={view === "editor" && flowId ? "editor" : "pane"}>
        {view === "billing" ? (
          <BillingView key={tenantId} tenantId={tenantId} />
        ) : view === "connections" ? (
          <ConnectionsView key={tenantId} tenantId={tenantId} />
        ) : view === "team" ? (
          <TeamView key={tenantId} tenantId={tenantId} />
        ) : view === "channels" ? (
          <ChannelsView key={tenantId} tenantId={tenantId} />
        ) : view === "guide" ? (
          <FlowGuideView />
        ) : view === "rules" ? (
          <RulesView key={tenantId} tenantId={tenantId} />
        ) : view === "knowledge" ? (
          <KnowledgeView key={tenantId} tenantId={tenantId} />
        ) : view === "runs" ? (
          <RunsView />
        ) : view === "activity" ? (
          <ActivityView key={tenantId} tenantId={tenantId} />
        ) : view === "review" ? (
          <ReviewView />
        ) : view === "trace" ? (
          <TraceView />
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
