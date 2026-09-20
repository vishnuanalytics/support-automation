import { useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import type { LucideIcon } from "lucide-react";
import {
  Workflow, History, Activity as ActivityIcon, CheckCircle2, Search,
  BookOpen, HelpCircle, ListChecks, ClipboardList, Compass, Users, Plug,
  CreditCard, Settings, LogOut, KeyRound,
} from "lucide-react";
import { supabase } from "./supabase";
import { api } from "./api";
import type { Invitation } from "./types";
import { PreAuth } from "./auth/PreAuth";
import { SetNewPassword } from "./auth/SetNewPassword";
import { FlowList } from "./flows/FlowList";
import { FlowEditor } from "./flows/FlowEditor";
import { RunsView } from "./runs/RunsView";
import { ReviewView } from "./review/ReviewView";
import { TraceView } from "./trace/TraceView";
import { KnowledgeView } from "./kb/KnowledgeView";
import { RulesView } from "./rules/RulesView";
import { AskView } from "./ask/AskView";
import { IntakeView } from "./intake/IntakeView";
import { IntegrationsView } from "./channels/IntegrationsView";
import { AdminView } from "./admin/AdminView";
import { FlowGuideView } from "./guide/FlowGuideView";
import { OnboardingWizard } from "./onboarding/OnboardingWizard";
import { AppShell } from "./ui/AppShell";
import { Sidebar } from "./ui/Sidebar";
import { Button, Dialog, Field, Input, Banner, ThemeToggle } from "./ui";

type View =
  | "setup"
  | "editor"
  | "runs"
  | "review"
  | "trace"
  | "knowledge"
  | "ask"
  | "rules"
  | "intake"
  | "guide"
  | "team"
  | "connections"
  | "billing"
  | "activity";

// Keep in sync with the View union above -- used to validate the `view`
// query param on load (a stale/hand-edited one falls back to "editor"
// instead of rendering a blank pane).
const VIEWS: View[] = [
  "setup", "editor", "runs", "review", "trace", "knowledge", "ask",
  "rules", "intake", "guide", "team", "connections", "billing", "activity",
];

// The app has no router (deliberately, per auth/PreAuth.tsx -- the
// Supabase OAuth/magic-link redirect uses the URL *hash*, so this reads/
// writes the query string instead to never collide with it). Without
// this, `view` was plain React state with no persistence at all: a
// refresh on any page other than the default landed back on Editor,
// since the state just re-initialized to "editor" on every fresh mount.
function viewFromUrl(): View {
  const v = new URLSearchParams(window.location.search).get("view");
  return VIEWS.includes(v as View) ? (v as View) : "editor";
}

type TenantMembership = { tenant_id: string; role: string; name?: string | null };

const NAV_GROUPS: { key: string; label: string; items: { view: View; label: string; icon: LucideIcon; ownerOnly?: boolean }[] }[] = [
  {
    key: "build",
    label: "Build",
    items: [
      { view: "editor", label: "Editor", icon: Workflow },
      { view: "runs", label: "Runs", icon: History },
      { view: "activity", label: "Activity", icon: ActivityIcon },
      { view: "review", label: "Approvals", icon: CheckCircle2 },
      { view: "trace", label: "Trace", icon: Search },
    ],
  },
  {
    key: "knowledge",
    label: "Knowledge",
    items: [
      { view: "knowledge", label: "Knowledge", icon: BookOpen },
      { view: "ask", label: "Ask", icon: HelpCircle },
      { view: "rules", label: "Rules", icon: ListChecks },
      { view: "intake", label: "Intake", icon: ClipboardList },
      { view: "guide", label: "Guide", icon: Compass },
    ],
  },
  {
    key: "admin",
    label: "Admin",
    items: [
      { view: "team", label: "Team", icon: Users, ownerOnly: true },
      { view: "connections", label: "Connections", icon: Plug, ownerOnly: true },
      { view: "billing", label: "Billing", icon: CreditCard, ownerOnly: true },
    ],
  },
];

function tenantLabel(t: TenantMembership): string {
  return t.name || `workspace ${t.tenant_id.slice(0, 8)}`;
}

export function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [recovering, setRecovering] = useState(false);
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);
  const [tenants, setTenants] = useState<TenantMembership[] | null>(null);
  const [pendingInvites, setPendingInvites] = useState<Invitation[] | null>(null);
  const [invitesDismissed, setInvitesDismissed] = useState(false);
  const [acceptingInvites, setAcceptingInvites] = useState(false);
  const [inviteAcceptErr, setInviteAcceptErr] = useState<string | null>(null);
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [flowId, setFlowId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [view, setView] = useState<View>(() => viewFromUrl());
  const [newWorkspaceOpen, setNewWorkspaceOpen] = useState(false);
  const [newWorkspaceName, setNewWorkspaceName] = useState("");
  const [workspaceErr, setWorkspaceErr] = useState<string | null>(null);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({
    build: true,
    knowledge: true,
    admin: false,
  });
  const [navCollapsed, setNavCollapsed] = useState(
    () => typeof window !== "undefined" && window.localStorage.getItem("sidebar-collapsed") === "1",
  );
  useEffect(() => {
    window.localStorage.setItem("sidebar-collapsed", navCollapsed ? "1" : "0");
  }, [navCollapsed]);
  // The flow list nested under "Editor" — clicking it while already on the
  // editor view toggles it shut instead of just sitting open forever;
  // clicking it from anywhere else always opens it (it'd be confusing for
  // a stale "closed" from a previous visit to swallow the first click).
  const [editorListOpen, setEditorListOpen] = useState(true);

  useEffect(() => {
    const owning = NAV_GROUPS.find((g) => g.items.some((i) => i.view === view));
    if (owning) setOpenGroups((prev) => (prev[owning.key] ? prev : { ...prev, [owning.key]: true }));
  }, [view]);

  // Mirror the current view into the URL (replaceState, not pushState --
  // clicking around the nav shouldn't spam the browser's back button) so
  // a refresh lands back where you were, not on the default Editor view.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (view === "editor") params.delete("view");
    else params.set("view", view);
    const qs = params.toString();
    const url = window.location.pathname + (qs ? `?${qs}` : "") + window.location.hash;
    window.history.replaceState(null, "", url);
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
    // auth-js re-emits SIGNED_IN on every tab focus (visibilitychange →
    // _recoverAndRefresh) with a fresh session object. Only take the new one
    // when the identity or token actually changed — otherwise a plain tab
    // switch churns `session`, which cascades into a full `load()` + shell
    // unmount below.
    const { data: sub } = supabase.auth.onAuthStateChange((e, s) => {
      // A password-reset link lands back here already signed in (Supabase
      // issues the session as part of processing the recovery token) --
      // without this, that session would just fall straight through to the
      // normal app with no chance to actually set the new password.
      if (e === "PASSWORD_RECOVERY") setRecovering(true);
      setSession((prev) =>
        prev?.access_token === s?.access_token && prev?.user.id === s?.user.id ? prev : s,
      );
    });
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
    // Reset synchronously, same as tenants above -- the render gates on
    // `tenants === null` while it's in flight, but pendingInvites has no
    // such gate of its own. Without this, a same-tab user switch (sign out,
    // a different user signs in) can flash the PREVIOUS user's pending
    // invite (their workspace name + role) if listTenants() happens to
    // resolve before this fetch does -- a real stale-data leak between
    // two different signed-in identities in the same tab, not just UI churn.
    setPendingInvites(null);
    setInvitesDismissed(false);
    setInviteAcceptErr(null);
    // Invites used to be claimed silently here on every sign-in -- an
    // invitee never saw it happen, which read as "it just shows accepted"
    // with no visible acceptance step. Now: read memberships, and
    // separately surface any pending invite for this email so accepting is
    // an explicit click (api.acceptInvitations()), not a side effect of
    // signing in.
    api.listTenants().then(applyTenants).catch(() => setTenants([]));
    const email = session?.user.email?.toLowerCase();
    api.team.invitations()
      .then((rows) => setPendingInvites(
        rows.filter((i) => i.status === "pending" && i.email.toLowerCase() === email)))
      .catch(() => setPendingInvites([]));
  };

  async function acceptInvites() {
    setAcceptingInvites(true);
    setInviteAcceptErr(null);
    try {
      await api.acceptInvitations();
      load();
    } catch (e) {
      setInviteAcceptErr(String(e));
    } finally {
      setAcceptingInvites(false);
    }
  }

  useEffect(() => {
    if (session) load();
    // key on the stable user id, not the session object — a genuine
    // TOKEN_REFRESHED (new access_token, same user) must not blow away
    // `tenants` and unmount the shell.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.user.id]);

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
    const name = newWorkspaceName.trim();
    if (!name) return;
    setWorkspaceErr(null);
    try {
      const created = await api.createTenant(name);
      if (storageKey) localStorage.setItem(storageKey, created.tenant_id);
      setNewWorkspaceOpen(false);
      setNewWorkspaceName("");
      load();
    } catch (e) {
      setWorkspaceErr(String(e));
    }
  }

  const newWorkspaceDialog = (
    <Dialog
      open={newWorkspaceOpen}
      onClose={() => setNewWorkspaceOpen(false)}
      title="New workspace"
      actions={[
        { label: "Cancel", variant: "ghost", onClick: () => setNewWorkspaceOpen(false) },
        { label: "Create", variant: "primary", onClick: createWorkspace },
      ]}
    >
      <div style={{ display: "grid", gap: 12 }}>
        <Field label="Name" hint="e.g. your company">
          <Input value={newWorkspaceName} autoFocus onChange={(e) => setNewWorkspaceName(e.target.value)} />
        </Field>
        {workspaceErr && <Banner tone="exception" title={workspaceErr} />}
      </div>
    </Dialog>
  );

  if (session === undefined) return <div style={{ padding: 20 }}>…</div>;
  if (recovering) {
    return (
      <div className="picker">
        <h1 style={{ font: "var(--type-view-title)", margin: 0 }}>Choose a new password</h1>
        <p className="muted" style={{ margin: 0 }}>
          Signed in as <strong>{session?.user.email}</strong>.
        </p>
        <SetNewPassword submitLabel="Save and continue" onDone={() => setRecovering(false)} />
      </div>
    );
  }
  if (session === null) return <PreAuth />;
  if (tenants === null) return <div style={{ padding: 20 }}>…</div>;

  if (pendingInvites && pendingInvites.length > 0 && !invitesDismissed) {
    return (
      <div className="picker">
        <h1 style={{ font: "var(--type-view-title)", margin: 0 }}>You've been invited</h1>
        <p className="muted" style={{ margin: 0 }}>
          Signed in as <strong>{session.user.email}</strong>.
        </p>
        <div className="picker__list">
          {pendingInvites.map((i) => (
            <div key={i.invite_id} className="picker__item">
              <span style={{ flex: 1, fontWeight: 600 }}>{i.tenant_name || "a workspace"}</span>
              <span className="muted" style={{ fontSize: 11 }}>as {i.role}</span>
            </div>
          ))}
        </div>
        {inviteAcceptErr && <Banner tone="exception" title={inviteAcceptErr} />}
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          <Button variant="primary" disabled={acceptingInvites} onClick={acceptInvites}>
            {acceptingInvites ? "Accepting…" : pendingInvites.length > 1 ? "Accept all" : "Accept invite"}
          </Button>
          <Button variant="ghost" onClick={() => setInvitesDismissed(true)}>Not now</Button>
          <Button variant="ghost" onClick={() => supabase.auth.signOut()}>Sign out</Button>
        </div>
      </div>
    );
  }

  if (tenants.length === 0) {
    return (
      <div className="picker">
        <h1 style={{ font: "var(--type-view-title)", margin: 0 }}>Set up your workspace</h1>
        <p className="muted" style={{ margin: 0 }}>
          You're signed in as <strong>{session.user.email}</strong>. Create a workspace to start
          building flows — or ask an owner to invite this email to an existing one.
        </p>
        <Button variant="primary" onClick={() => { setNewWorkspaceName(""); setNewWorkspaceOpen(true); }}>
          ＋ Create a workspace
        </Button>
        <Button variant="ghost" onClick={() => supabase.auth.signOut()}>Sign out</Button>
        {newWorkspaceDialog}
      </div>
    );
  }

  if (!tenantId) {
    return (
      <div className="picker">
        <span className="muted" style={{ font: "var(--type-kicker)", textTransform: "uppercase", letterSpacing: ".16em" }}>
          Signed in as {session.user.email}
        </span>
        <h1 style={{ font: "var(--type-view-title)", margin: 0 }}>Choose a workspace</h1>
        <div className="picker__list">
          {tenants.map((t) => (
            <button key={t.tenant_id} className="picker__item" onClick={() => chooseTenant(t.tenant_id)}>
              <span style={{ flex: 1, fontWeight: 600 }}>{tenantLabel(t)}</span>
              <span className="muted" style={{ fontSize: 11 }}>{t.role}</span>
            </button>
          ))}
        </div>
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          <Button variant="secondary" size="sm" onClick={() => { setNewWorkspaceName(""); setNewWorkspaceOpen(true); }}>
            ＋ New workspace
          </Button>
          <Button variant="ghost" size="sm" onClick={() => supabase.auth.signOut()}>Sign out</Button>
        </div>
        {newWorkspaceDialog}
      </div>
    );
  }

  const sidebar = (
    <Sidebar
      collapsed={navCollapsed}
      onToggleCollapsed={() => setNavCollapsed((v) => !v)}
      head={
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
          {tenants.length > 1 && (
            <select
              className="sidebar-collapsible workspace-select"
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
            <span className="pill sidebar-collapsible" title="your access is view-only">view-only</span>
          )}
        </div>
      }
      foot={
        <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
          <span
            className="muted sidebar-collapsible"
            style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 12 }}
            title={session.user.email ?? ""}
          >
            {session.user.email}
          </span>
          <ThemeToggle />
          <Button
            variant="icon"
            size="sm"
            title="Set password"
            aria-label="Set password"
            onClick={() => setPasswordDialogOpen(true)}
          >
            <KeyRound size={16} />
          </Button>
          <Button
            variant="icon"
            size="sm"
            title="Sign out"
            aria-label="Sign out"
            onClick={() => supabase.auth.signOut()}
          >
            <LogOut size={16} />
          </Button>
        </div>
      }
    >
      <div className="col">
        <nav className="nav-list col">
          <button
            className={"nav-item" + (view === "setup" ? " active" : "")}
            style={{ fontWeight: 600 }}
            title="Setup"
            onClick={() => setView("setup")}
          >
            <Settings size={17} />
            <span className="sidebar-collapsible">Setup</span>
          </button>
          {NAV_GROUPS.map((g) => {
            const items = g.items.filter((i) => !i.ownerOnly || isOwner);
            if (items.length === 0) return null;
            const open = openGroups[g.key] || navCollapsed;
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
                      <div key={i.view} className="col" style={{ gap: 0 }}>
                        <button
                          className={"nav-item" + (view === i.view ? " active" : "")}
                          title={i.label}
                          onClick={() => {
                            if (i.view === "editor" && view === "editor") {
                              setEditorListOpen((v) => !v);
                            } else {
                              setView(i.view);
                              if (i.view === "editor") setEditorListOpen(true);
                            }
                          }}
                        >
                          <i.icon size={17} />
                          <span className="sidebar-collapsible">{i.label}</span>
                          {i.view === "editor" && (
                            <span className="nav-item__caret sidebar-collapsible" aria-hidden>
                              {editorListOpen ? "▾" : "▸"}
                            </span>
                          )}
                        </button>
                        {/* the flow picker opens right under Editor, not after
                            every other nav group — otherwise a long BUILD/
                            KNOWLEDGE/ADMIN list buries it below the fold every
                            time (reported: "saved editors showing bottom of
                            the page"). Clicking Editor again while already
                            there toggles it shut instead of it just sitting
                            open forever. */}
                        {i.view === "editor" && view === "editor" && editorListOpen && !navCollapsed && (
                          <div className="nav-flowlist sidebar-collapsible">
                            <FlowList
                              key={`${tenantId}:${reloadKey}`}
                              tenantId={tenantId}
                              activeId={flowId}
                              canEdit={canEdit}
                              onSelect={setFlowId}
                              onCreated={(id) => {
                                setReloadKey((k) => k + 1);
                                setFlowId(id);
                              }}
                            />
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </nav>
        <div className="sidebar-collapsible col">
        {view === "runs" && <div className="muted">observability — recent interpreter runs across your tenants</div>}
        {view === "activity" && <div className="muted">who did what — flow publishes/rollbacks, approvals, membership, connections</div>}
        {view === "trace" && <div className="muted">one timeline per Case — jobs, runs, nodes and errors, in order</div>}
        {view === "knowledge" && (
          <div className="muted">
            internal SOPs &amp; runbooks per team — a <code>kb_lookup</code> node
            in a flow consults a collection at a checkpoint
          </div>
        )}
        {view === "ask" && (
          <div className="muted">
            plain-English questions over this workspace's cases — answered from the
            case graph (read-only, workspace-scoped) or the closest resolved cases
          </div>
        )}
        {view === "rules" && (
          <div className="muted">
            structured <code>when → then</code> rules a <code>policy_gate</code> node
            evaluates; <code>task</code> outcomes route through Slack approval
          </div>
        )}
        {view === "intake" && (
          <div className="muted">
            per-issue checklists — the exact questions the <code>clarify</code> node
            asks when a customer's report is missing detail
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
            usage against your plan's quota, and the plans you can subscribe or
            upgrade to — real Stripe/Razorpay checkout, no card ever touches us
          </div>
        )}
        </div>
      </div>
    </Sidebar>
  );

  return (
    <>
    <AppShell sidebar={sidebar}>
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
        ) : view === "billing" || view === "team" || view === "activity" ? (
          <AdminView key={tenantId} tenantId={tenantId} tab={view} />
        ) : view === "connections" ? (
          <IntegrationsView key={tenantId} tenantId={tenantId} />
        ) : view === "guide" ? (
          <FlowGuideView />
        ) : view === "ask" ? (
          <AskView key={tenantId} tenantId={tenantId} />
        ) : view === "rules" ? (
          <RulesView key={tenantId} tenantId={tenantId} />
        ) : view === "intake" ? (
          <IntakeView key={tenantId} tenantId={tenantId} />
        ) : view === "knowledge" ? (
          <KnowledgeView key={tenantId} tenantId={tenantId} />
        ) : view === "runs" ? (
          <RunsView />
        ) : view === "review" ? (
          <ReviewView key={tenantId} tenantId={tenantId} />
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
    </AppShell>
    <Dialog
      open={passwordDialogOpen}
      onClose={() => setPasswordDialogOpen(false)}
      title="Set a password"
    >
      <p className="muted" style={{ marginTop: 0 }}>
        Useful if you've only ever signed in via a magic link or Google — this lets you sign in
        with a password too, without needing another email.
      </p>
      <SetNewPassword onDone={() => setPasswordDialogOpen(false)} />
    </Dialog>
    </>
  );
}
