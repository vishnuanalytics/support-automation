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
import { OnboardingWizard } from "./onboarding/OnboardingWizard";

type View =
  | "setup"
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

const NAV_GROUPS: { key: string; label: string; items: { view: View; label: string; ownerOnly?: boolean }[] }[] = [
  {
    key: "build",
    label: "Build",
    items: [
      { view: "editor", label: "Editor" },
      { view: "runs", label: "Runs" },
      { view: "activity", label: "Activity" },
      { view: "review", label: "Approvals" },
      { view: "trace", label: "Trace" },
    ],
  },
  {
    key: "knowledge",
    label: "Knowledge",
    items: [
      { view: "knowledge", label: "Knowledge" },
      { view: "rules", label: "Rules" },
      { view: "guide", label: "Guide" },
    ],
  },
  {
    key: "admin",
    label: "Admin",
    items: [
      { view: "team", label: "Team", ownerOnly: true },
      { view: "channels", label: "Channels", ownerOnly: true },
      { view: "connections", label: "Connections", ownerOnly: true },
      { view: "billing", label: "Billing", ownerOnly: true },
    ],
  },
];

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
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({
    build: true,
    knowledge: true,
    admin: false,
  });

  useEffect(() => {
    const owning = NAV_GROUPS.find((g) => g.items.some((i) => i.view === view));
    if (owning) setOpenGroups((prev) => (prev[owning.key] ? prev : { ...prev, [owning.key]: true }));
  }, [view]);

  // land a brand-new (or not-yet-dismissed) tenant on the setup wizard once,
  // the first time we know which tenant is active — never fights later nav.
  const [setupCheckedFor, setSetupCheckedFor] = useState<string | null>(null);
  useEffect(() => {
    if (!tenantId || setupCheckedFor === tenantId) return;
    setSetupCheckedFor(tenantId);
    if (!localStorage.getItem(`onboarding-dismissed:${tenantId}`)) setView("setup");
  }, [tenantId, setupCheckedFor]);

  function dismissOnboarding() {
    if (tenantId) localStorage.setItem(`onboarding-dismissed:${tenantId}`, "1");
    setView("editor");
  }

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
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
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
        <nav className="nav-list col">
          <button
            className={"nav-item" + (view === "setup" ? " active" : "")}
            style={{ fontWeight: 600 }}
            onClick={() => setView("setup")}
          >
            ⚙ Setup
          </button>
          {NAV_GROUPS.map((g) => {
            const items = g.items.filter((i) => !i.ownerOnly || isOwner);
            if (items.length === 0) return null;
            const open = openGroups[g.key];
            return (
              <div key={g.key} className="nav-group">
                <button
                  className="nav-group-header"
                  onClick={() => setOpenGroups((prev) => ({ ...prev, [g.key]: !prev[g.key] }))}
                  aria-expanded={open}
                >
                  <span className="nav-caret">{open ? "▾" : "▸"}</span> {g.label}
                </button>
                {open && (
                  <div className="nav-group-items col">
                    {items.map((i) => (
                      <button
                        key={i.view}
                        className={"nav-item" + (view === i.view ? " active" : "")}
                        onClick={() => setView(i.view)}
                      >
                        {i.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </nav>
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
        {view === "setup" ? (
          <OnboardingWizard
            key={tenantId}
            tenantId={tenantId}
            isOwner={isOwner}
            onNavigate={(v) => setView(v)}
            onFlowCreated={(id) => {
              setReloadKey((k) => k + 1);
              setFlowId(id);
            }}
            onDismiss={dismissOnboarding}
          />
        ) : view === "billing" ? (
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
