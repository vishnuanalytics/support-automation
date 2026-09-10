import { useEffect, useState, type ReactNode } from "react";
import { api, ApiError } from "../api";
import type { TemplateMeta } from "../types";
import { Toolbar, Button, Banner, Dialog, Field, Input, Select } from "../ui";

/**
 * Guided first-run setup, as a checklist inside the standard frame: exactly
 * one step is open at a time (the first incomplete one, unless you pick
 * another), progress derives from real state, and each step's action opens a
 * dialog or a real screen. Connecting a knowledge source is required — the
 * "create a flow" step stays locked until then. `onDismiss` fires on dismiss.
 */
export function OnboardingWizard({
  tenantId,
  isOwner,
  onNavigate,
  onFlowCreated,
  onDismiss,
}: {
  tenantId: string;
  isOwner: boolean;
  onNavigate: (view: "connections" | "rules" | "knowledge") => void;
  onFlowCreated: (id: string) => void;
  onDismiss: () => void;
}) {
  const [sfCount, setSfCount] = useState<number | null>(null);
  const [sfOauthConfigured, setSfOauthConfigured] = useState(false);
  const [sfMsg, setSfMsg] = useState<string | null>(null);

  const [slackConfigured, setSlackConfigured] = useState(false);
  const [slackConnected, setSlackConnected] = useState<boolean | null>(null);

  const [flowCount, setFlowCount] = useState<number | null>(null);
  const [templates, setTemplates] = useState<TemplateMeta[]>([]);
  const [templateId, setTemplateId] = useState("");
  const [busy, setBusy] = useState(false);
  const [flowDialog, setFlowDialog] = useState(false);
  const [flowTeam, setFlowTeam] = useState("support");
  const [flowErr, setFlowErr] = useState<string | null>(null);

  const [orgKbId, setOrgKbId] = useState<string | null>(null);
  const [kbSourceCount, setKbSourceCount] = useState<number | null>(null);
  const [kbUrl, setKbUrl] = useState("");
  const [kbBusy, setKbBusy] = useState(false);
  const [kbMsg, setKbMsg] = useState<string | null>(null);

  const [openStep, setOpenStep] = useState<number | null>(null);

  const loadKb = () => {
    api.kb
      .listCollections(tenantId)
      .then((cs) => setOrgKbId(cs.find((c) => c.org_kb)?.source_id ?? cs[0]?.source_id ?? null))
      .catch(() => {});
    api.kb
      .listAllConnections(tenantId)
      .then((r) => setKbSourceCount(r.length))
      .catch(() => setKbSourceCount(0));
  };

  useEffect(() => {
    api.salesforceOrgs.list(tenantId).then((r) => setSfCount(r.length)).catch(() => setSfCount(0));
    api.salesforceOrgs.oauthStatus().then((s) => setSfOauthConfigured(s.configured)).catch(() => {});
    api.slack
      .status()
      .then((s) => {
        setSlackConfigured(s.configured);
        setSlackConnected(s.connected[tenantId] ?? false);
      })
      .catch(() => setSlackConnected(false));
    api
      .listFlows()
      .then((fs) => setFlowCount(fs.filter((f) => f.tenant_id === tenantId).length))
      .catch(() => setFlowCount(0));
    api.templates.list().then(setTemplates).catch(() => {});
    loadKb();
  }, [tenantId]);

  const connectKbUrl = async () => {
    const url = kbUrl.trim();
    if (!orgKbId || !url) return;
    setKbBusy(true);
    setKbMsg(null);
    try {
      await api.kb.crawl(orgKbId, url);
      setKbMsg("Crawling in the background — pages appear in Knowledge as they embed.");
      setKbUrl("");
      loadKb();
    } catch (e) {
      setKbMsg((e as ApiError).message);
    } finally {
      setKbBusy(false);
    }
  };

  const connectSalesforce = async () => {
    setSfMsg(null);
    try {
      const { url } = await api.salesforceOrgs.oauthAuthorize({ org_label: "default", tenant_id: tenantId });
      window.open(url, "_blank", "width=520,height=680");
      setSfMsg("Finish in the Salesforce window, then refresh this page.");
    } catch (e) {
      setSfMsg(`✗ ${(e as ApiError).message}`);
    }
  };

  const connectSlack = async () => {
    const { url } = await api.slack.authorize(tenantId);
    const w = window.open(url, "slack-oauth", "width=520,height=720");
    const t = setInterval(() => {
      if (w?.closed) {
        clearInterval(t);
        api.slack.status().then((s) => setSlackConnected(s.connected[tenantId] ?? false)).catch(() => {});
      }
    }, 800);
  };

  const createFirstFlow = async () => {
    if (!templateId || !flowTeam.trim()) return;
    setBusy(true);
    setFlowErr(null);
    try {
      const cand = await api.templates.graph(templateId);
      const { flow_id } = await api.createFlow({
        team: flowTeam.trim(),
        name: cand.name || "New flow",
        tenant_id: tenantId,
      });
      sessionStorage.setItem(`pendingCandidate:${flow_id}`, JSON.stringify(cand));
      setFlowDialog(false);
      onFlowCreated(flow_id);
      onDismiss();
    } catch (e) {
      setFlowErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const sfDone = (sfCount ?? 0) > 0;
  const slackDone = !slackConfigured || slackConnected === true;
  const flowDone = (flowCount ?? 0) > 0;
  const kbDone = (kbSourceCount ?? 0) > 0;

  const steps: { key: string; title: string; done: boolean; body: ReactNode }[] = [
    {
      key: "sf",
      title: "Connect Salesforce",
      done: sfDone,
      body: (
        <>
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            Salesforce is the source of truth — inbound cases become real Salesforce Cases, and
            flows read/write Case fields, Queues and Users from your org.
          </p>
          {sfDone ? (
            <span className="muted" style={{ fontSize: 12 }}>{sfCount} org connected</span>
          ) : (
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              {sfOauthConfigured ? (
                <Button variant="primary" size="sm" onClick={connectSalesforce}>
                  Connect Salesforce
                </Button>
              ) : (
                <Button variant="secondary" size="sm" onClick={() => onNavigate("connections")}>
                  Set up with a Connected App →
                </Button>
              )}
              {isOwner && (
                <Button variant="ghost" size="sm" onClick={() => onNavigate("connections")}>
                  advanced (JWT / multiple orgs)
                </Button>
              )}
            </div>
          )}
          {sfMsg && <div className="muted" style={{ fontSize: 12 }}>{sfMsg}</div>}
        </>
      ),
    },
    {
      key: "slack",
      title: "Connect Slack",
      done: slackDone,
      body: (
        <>
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            Optional — lets a flow ping a human for approval or post to a channel instead of just
            Salesforce Chatter.
          </p>
          {!slackConfigured ? (
            <div className="muted" style={{ fontSize: 12 }}>
              Not set up on this server yet — an owner can configure it under Rules.
            </div>
          ) : slackConnected ? (
            <span className="muted" style={{ fontSize: 12 }}>connected</span>
          ) : (
            <Button variant="secondary" size="sm" onClick={connectSlack}>
              Connect Slack
            </Button>
          )}
        </>
      ),
    },
    {
      key: "model",
      title: "Choose an AI model",
      done: true,
      body: (
        <>
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            Every LLM node already runs on Groq's free tier by default — no key needed. Pick a
            specific model per-node from the flow editor's Inspector. Want to use your own Claude
            or OpenRouter key for every flow here?
          </p>
          {isOwner && (
            <Button variant="ghost" size="sm" onClick={() => onNavigate("connections")}>
              Add your own API key →
            </Button>
          )}
        </>
      ),
    },
    {
      key: "kb",
      title: "Connect a knowledge source (required)",
      done: kbDone,
      body: (
        <>
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            Everything you connect feeds the org knowledge base and your flows read all of it.
            Fastest start: crawl a public docs / help-center site (no login needed).
          </p>
          {kbDone ? (
            <span className="muted" style={{ fontSize: 12 }}>
              {kbSourceCount} source{kbSourceCount === 1 ? "" : "s"}
            </span>
          ) : (
            <>
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                <Input
                  style={{ flex: 1, minWidth: 240 }}
                  placeholder="https://docs.yourcompany.com"
                  value={kbUrl}
                  onChange={(e) => setKbUrl(e.target.value)}
                />
                <Button
                  variant="primary"
                  size="sm"
                  loading={kbBusy}
                  disabled={!kbUrl.trim() || !orgKbId}
                  onClick={connectKbUrl}
                >
                  Crawl &amp; connect
                </Button>
              </div>
              <Button variant="ghost" size="sm" style={{ alignSelf: "flex-start" }} onClick={() => onNavigate("knowledge")}>
                Google Docs / Sheets, Linear, Nolt… →
              </Button>
            </>
          )}
          {kbMsg && <span className="muted" style={{ fontSize: 12 }}>{kbMsg}</span>}
        </>
      ),
    },
    {
      key: "flow",
      title: "Create your first flow",
      done: flowDone,
      body: (
        <>
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            {kbDone
              ? "Start from a template — a working flow you can edit rather than a blank canvas."
              : "Connect a knowledge source above first."}
          </p>
          {flowDone ? (
            <span className="muted" style={{ fontSize: 12 }}>
              {flowCount} flow{flowCount === 1 ? "" : "s"}
            </span>
          ) : (
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <Select
                value={templateId}
                onChange={(e) => setTemplateId(e.target.value)}
                disabled={!kbDone}
                placeholder="choose a template…"
                options={templates.map((t) => ({ value: t.id, label: t.name }))}
              />
              <Button
                variant="primary"
                size="sm"
                disabled={!kbDone || !templateId}
                onClick={() => {
                  setFlowErr(null);
                  setFlowDialog(true);
                }}
              >
                Create flow
              </Button>
              <Button variant="ghost" size="sm" disabled={!kbDone} onClick={onDismiss}>
                start from a blank flow instead →
              </Button>
            </div>
          )}
        </>
      ),
    },
  ];

  const doneCount = steps.filter((s) => s.done).length;
  const firstIncomplete = steps.findIndex((s) => !s.done);
  const activeStep = openStep ?? (firstIncomplete === -1 ? steps.length - 1 : firstIncomplete);

  return (
    <div className="setup-shell">
      <div className="app-toolbar">
        <Toolbar title="Setup" meta={`${doneCount} of ${steps.length} done`}>
          <Button
            variant="ghost"
            size="sm"
            disabled={!kbDone}
            title={kbDone ? "" : "Connect a knowledge source first — the bot has nothing to answer from otherwise."}
            onClick={onDismiss}
          >
            {kbDone ? "Skip for now" : "Connect a source to continue"}
          </Button>
        </Toolbar>
      </div>

      <div className="setup-body">
        <div style={{ maxWidth: 760, display: "grid", gap: 16 }}>
          <div>
            <h2 style={{ margin: 0, font: "var(--type-view-title)" }}>Get set up</h2>
            <p className="muted" style={{ margin: "8px 0 0", maxWidth: "60ch" }}>
              Salesforce, Slack and the model are optional and reachable later.
              <strong> Connecting a knowledge source is required</strong> — it's what your bot
              answers from.
            </p>
            <div
              style={{
                height: 4,
                background: "var(--surface-raised)",
                borderRadius: "var(--radius-md)",
                marginTop: 16,
                maxWidth: 520,
              }}
            >
              <div
                style={{
                  height: "100%",
                  width: `${(doneCount / steps.length) * 100}%`,
                  background: "var(--accent)",
                  borderRadius: "var(--radius-md)",
                }}
              />
            </div>
          </div>

          <div style={{ display: "grid", gap: 10 }}>
            {steps.map((s, i) => {
              const open = i === activeStep;
              return (
                <div
                  key={s.key}
                  className={
                    "setup-step" +
                    (s.done ? " setup-step--done" : "") +
                    (open ? " setup-step--open" : "")
                  }
                >
                  <button
                    type="button"
                    className="setup-step__head"
                    aria-expanded={open}
                    onClick={() => setOpenStep(open ? -1 : i)}
                  >
                    <span className="setup-step__mark">{s.done ? "✓" : i + 1}</span>
                    <span className="setup-step__title">{s.title}</span>
                  </button>
                  {open && <div className="setup-step__body">{s.body}</div>}
                </div>
              );
            })}
          </div>

          <p className="muted" style={{ fontSize: 12, margin: 0 }}>
            Dismissing is remembered per workspace. Setup stays in the nav — it becomes the health
            checklist.
          </p>
        </div>
      </div>

      <Dialog
        open={flowDialog}
        onClose={() => setFlowDialog(false)}
        title="Create your first flow"
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setFlowDialog(false) },
          { label: busy ? "Creating…" : "Create flow", variant: "primary", onClick: createFirstFlow },
        ]}
      >
        <div style={{ display: "grid", gap: 12 }}>
          <Field label="Team" hint="support / csm / offboarding / …">
            <Input value={flowTeam} autoFocus onChange={(e) => setFlowTeam(e.target.value)} />
          </Field>
          {flowErr && <Banner tone="exception" title={flowErr} />}
        </div>
      </Dialog>
    </div>
  );
}
