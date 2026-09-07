import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { TemplateMeta } from "../types";

/**
 * Guided first-run setup: connect Salesforce, connect Slack, understand the
 * LLM model default, land in a first flow — the four things a brand-new
 * tenant previously had to discover on their own across separate tabs
 * (Connections, Rules, and the node Inspector).
 *
 * Salesforce / Slack / model are skippable. **Connecting at least one
 * knowledge source is required** — the bot has nothing to answer from
 * otherwise — so the "create your first flow" step stays locked until a
 * source is connected. `onDismiss` is called once the wizard is dismissed.
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

  const [orgKbId, setOrgKbId] = useState<string | null>(null);
  const [kbSourceCount, setKbSourceCount] = useState<number | null>(null);
  const [kbUrl, setKbUrl] = useState("");
  const [kbBusy, setKbBusy] = useState(false);
  const [kbMsg, setKbMsg] = useState<string | null>(null);

  const loadKb = () => {
    // scope to this workspace — otherwise a Setup crawl can upload into
    // another workspace's "Organization knowledge" (same name, cross-tenant)
    api.kb.listCollections(tenantId)
      .then((cs) => {
        const mine = cs.filter((c) => c.tenant_id === tenantId);
        setOrgKbId(mine.find((c) => c.org_kb)?.source_id ?? mine[0]?.source_id ?? null);
      })
      .catch(() => {});
    api.kb.listAllConnections(tenantId).then((r) => setKbSourceCount(r.length)).catch(() => setKbSourceCount(0));
  };

  useEffect(() => {
    api.salesforceOrgs.list(tenantId).then((r) => setSfCount(r.length)).catch(() => setSfCount(0));
    api.salesforceOrgs.oauthStatus().then((s) => setSfOauthConfigured(s.configured)).catch(() => {});
    api.slack.status().then((s) => {
      setSlackConfigured(s.configured);
      setSlackConnected(s.connected[tenantId] ?? false);
    }).catch(() => setSlackConnected(false));
    api.listFlows().then((fs) => setFlowCount(fs.filter((f) => f.tenant_id === tenantId).length)).catch(() => setFlowCount(0));
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
      setKbMsg(
        "Sample crawl started — the first ~20 pages will appear in Knowledge shortly. " +
        "Run the full site crawl from Knowledge → add source when you're ready.",
      );
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
    if (!templateId) return;
    setBusy(true);
    try {
      const cand = await api.templates.graph(templateId);
      const team = prompt("team (support / csm / offboarding / …)", "support");
      if (!team?.trim()) return;
      const { flow_id } = await api.createFlow({ team: team.trim(), name: cand.name || "New flow", tenant_id: tenantId });
      sessionStorage.setItem(`pendingCandidate:${flow_id}`, JSON.stringify(cand));
      onFlowCreated(flow_id);
      onDismiss();
    } catch (e) {
      alert((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const sfDone = (sfCount ?? 0) > 0;
  const slackDone = !slackConfigured || slackConnected === true; // if not configured server-side, nothing to do here
  const flowDone = (flowCount ?? 0) > 0;
  const kbDone = (kbSourceCount ?? 0) > 0;

  return (
    <div className="col" style={{ padding: 20, gap: 18, maxWidth: 640, overflow: "auto", height: "100%" }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <h2 style={{ margin: 0 }}>Get set up</h2>
        <button
          onClick={onDismiss}
          disabled={!kbDone}
          title={kbDone ? "" : "Connect a knowledge source below first — the bot has nothing to answer from otherwise."}
        >
          {kbDone ? "skip the rest" : "connect a source to continue"}
        </button>
      </div>
      <p className="muted" style={{ margin: 0 }}>
        Salesforce, Slack and the model are optional and reachable later.
        <strong> Connecting a knowledge source is required</strong> — it's what
        your bot answers from.
      </p>

      {/* Step 1 — Salesforce */}
      <div className="col" style={{ gap: 6, padding: 12, border: "1px solid var(--border)", borderRadius: 8 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <strong>{sfDone ? "✓ " : "1. "}Connect Salesforce</strong>
          {sfDone && <span className="muted" style={{ fontSize: 12 }}>{sfCount} org connected</span>}
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          Salesforce is the source of truth — inbound cases become real Salesforce
          Cases, and flows read/write Case fields, Queues, and Users from your org.
        </p>
        {!sfDone && (
          <div className="row" style={{ gap: 8 }}>
            {sfOauthConfigured ? (
              <button className="primary" onClick={connectSalesforce}>Connect Salesforce</button>
            ) : (
              <button onClick={() => onNavigate("connections")}>Set up with a Connected App →</button>
            )}
            {isOwner && <button onClick={() => onNavigate("connections")}>advanced (JWT / multiple orgs)</button>}
          </div>
        )}
        {sfMsg && <div className="muted" style={{ fontSize: 12 }}>{sfMsg}</div>}
      </div>

      {/* Step 2 — Slack */}
      <div className="col" style={{ gap: 6, padding: 12, border: "1px solid var(--border)", borderRadius: 8 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <strong>{slackDone ? "✓ " : "2. "}Connect Slack</strong>
          {slackConnected && <span className="muted" style={{ fontSize: 12 }}>connected</span>}
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          Optional — lets a flow ping a human for approval (e.g. a GitHub action a
          policy rule gates) or post to a channel instead of just Salesforce Chatter.
        </p>
        {!slackDone && (
          <button onClick={connectSlack}>Connect Slack</button>
        )}
        {!slackConfigured && (
          <div className="muted" style={{ fontSize: 12 }}>
            Not set up on this server yet — an owner can configure it under Rules.
          </div>
        )}
      </div>

      {/* Step 3 — model */}
      <div className="col" style={{ gap: 6, padding: 12, border: "1px solid var(--border)", borderRadius: 8 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <strong>✓ 3. Choose an AI model</strong>
          <span className="muted" style={{ fontSize: 12 }}>nothing to do — works out of the box</span>
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          Every flow node that calls an LLM (drafting a reply, classifying, judging)
          already runs on Groq's free tier by default — no key needed. Pick a
          specific model (Groq / Claude / OpenRouter) per-node from a real dropdown
          in the flow editor's Inspector panel. Want to use your own Claude or
          OpenRouter key instead of this deployment's own, for every flow here?
        </p>
        {isOwner && (
          <button onClick={() => onNavigate("connections")}>Add your own API key →</button>
        )}
      </div>

      {/* Step 4 — knowledge (required) */}
      <div
        className="col"
        style={{
          gap: 6, padding: 12, borderRadius: 8,
          border: `1px solid ${kbDone ? "var(--border)" : "var(--crit, #b4432a)"}`,
        }}
      >
        <div className="row" style={{ justifyContent: "space-between" }}>
          <strong>{kbDone ? "✓ " : "4. "}Connect a knowledge source <em>(required)</em></strong>
          {kbDone && (
            <span className="muted" style={{ fontSize: 12 }}>
              {kbSourceCount} source{kbSourceCount === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          Everything you connect feeds the org knowledge base and your flows read all of it.
          Fastest start: a quick <strong>sample crawl</strong> of a public docs / help-center
          site — the first ~20 pages, just to see it working. Run the full deep crawl of the
          whole site afterwards from <button
            style={{ padding: 0, border: "none", background: "none", color: "var(--accent)", cursor: "pointer", font: "inherit" }}
            onClick={() => onNavigate("knowledge")}
          >Knowledge → add source</button>.
        </p>
        {!kbDone && (
          <>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <input
                style={{ flex: 1, minWidth: 240 }}
                placeholder="https://docs.yourcompany.com"
                value={kbUrl}
                onChange={(e) => setKbUrl(e.target.value)}
              />
              <button
                className="primary"
                disabled={kbBusy || !kbUrl.trim() || !orgKbId}
                onClick={connectKbUrl}
              >
                {kbBusy ? "connecting…" : "Sample crawl (~20 pages)"}
              </button>
            </div>
            <button style={{ alignSelf: "flex-start" }} onClick={() => onNavigate("knowledge")}>
              Google Docs / Sheets, Linear, Nolt… →
            </button>
          </>
        )}
        {kbMsg && <span className="muted" style={{ fontSize: 12 }}>{kbMsg}</span>}
      </div>

      {/* Step 5 — first flow (locked until a knowledge source exists) */}
      <div className="col" style={{ gap: 6, padding: 12, border: "1px solid var(--border)", borderRadius: 8 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <strong>{flowDone ? "✓ " : "5. "}Create your first flow</strong>
          {flowDone && <span className="muted" style={{ fontSize: 12 }}>{flowCount} flow{flowCount === 1 ? "" : "s"}</span>}
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          {kbDone
            ? "Start from a template — a working flow you can edit rather than a blank canvas."
            : "Connect a knowledge source above first."}
        </p>
        {!flowDone && (
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <select
              value={templateId}
              onChange={(e) => setTemplateId(e.target.value)}
              disabled={!kbDone}
              style={{ width: "auto", minWidth: 220 }}
            >
              <option value="">choose a template…</option>
              {templates.map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
            <button className="primary" disabled={!kbDone || !templateId || busy} onClick={createFirstFlow}>
              {busy ? "creating…" : "Create flow"}
            </button>
            <button disabled={!kbDone} onClick={() => { onDismiss(); }}>
              start from a blank flow instead →
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
