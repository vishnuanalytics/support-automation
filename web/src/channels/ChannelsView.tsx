import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type {
  EmailChannel,
  EmailChannelSave,
  FreshchatChannel,
  FreshchatChannelSave,
  PostHogSave,
  PostHogStatus,
} from "../types";
import { Banner, ConfirmButton } from "../ui";

type Form = {
  provider: "imap" | "gmail";
  team: string;
  imap_host: string;
  imap_port: number;
  smtp_host: string;
  smtp_port: number;
  username: string;
  password: string;
  from_name: string;
  no_reply_addr: string;
  folder: string;
  auto_send_enabled: boolean;
  active: boolean;
};

const BLANK: Form = {
  provider: "imap", team: "support",
  imap_host: "", imap_port: 993, smtp_host: "", smtp_port: 587,
  username: "", password: "", from_name: "", no_reply_addr: "",
  folder: "INBOX", auto_send_enabled: false, active: false,
};

function fromChannel(ch: EmailChannel): Form {
  return {
    ...BLANK,
    provider: ch.provider ?? "imap",
    team: ch.team ?? "support",
    imap_host: ch.imap_host ?? "",
    imap_port: ch.imap_port ?? 993,
    smtp_host: ch.smtp_host ?? "",
    smtp_port: ch.smtp_port ?? 587,
    username: ch.username ?? "",
    from_name: ch.from_name ?? "",
    no_reply_addr: ch.no_reply_addr ?? "",
    folder: ch.folder ?? "INBOX",
    auto_send_enabled: ch.auto_send_enabled ?? false,
    active: ch.status === "active",
  };
}

export function ChannelsView({ tenantId }: { tenantId: string }) {
  return (
    <div style={{ display: "grid", gap: 16 }}>
      <EmailPanel tenantId={tenantId} />
      <FreshchatPanel tenantId={tenantId} />
      <PostHogPanel tenantId={tenantId} />
    </div>
  );
}

function PostHogPanel({ tenantId }: { tenantId: string }) {
  const [st, setSt] = useState<PostHogStatus | null>(null);
  const [host, setHost] = useState("https://us.posthog.com");
  const [projectId, setProjectId] = useState("");
  const [milestones, setMilestones] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function load() {
    api.posthog
      .status(tenantId)
      .then((s) => {
        setSt(s);
        if (s.configured) {
          setHost(s.host || "https://us.posthog.com");
          setProjectId(s.project_id || "");
          setMilestones((s.milestone_events || []).join(", "));
        }
      })
      .catch((e: ApiError) => setErr(e.message));
  }
  useEffect(load, [tenantId]);

  const payload = useMemo<PostHogSave>(
    () => ({
      tenant_id: tenantId,
      host: host.trim(),
      project_id: projectId.trim(),
      milestone_events: milestones
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      api_key: apiKey || undefined,
    }),
    [tenantId, host, projectId, milestones, apiKey],
  );

  async function run<T>(fn: () => Promise<T>, ok: string) {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      await fn();
      setMsg(ok);
      setApiKey("");
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setBusy(false);
  }

  const testConn = () =>
    run(async () => {
      const r = await api.posthog.test(payload);
      if (!r.ok) throw new ApiError(0, r.detail || "connection failed");
    }, "connection ok");

  return (
    <div className="int-card">
      <h4>Product analytics — PostHog</h4>
      <p className="muted" style={{ fontSize: 12 }}>
        Connect your PostHog project and the platform will pull a per-person
        activity rollup (last seen, 30-day event & active-day counts, a usage
        trend) plus counts for the milestone events you name below. It uses that
        to ground a draft and routing in what the user actually did — never to
        block a run. Nothing here copies your raw event stream; PostHog stays the
        source of truth. The API key is stored encrypted (Supabase Vault) and
        never shown again.
      </p>

      {st?.configured && (
        <p style={{ fontSize: 12 }}>
          Status: <strong>{st.status}</strong>
          {st.has_credentials ? " · key stored" : " · no key stored"}
          {st.contacts_synced != null && st.contacts_synced > 0 && (
            <>
              {" · "}
              {st.contacts_synced.toLocaleString()} contacts ·{" "}
              <strong>
                {st.coverage_pct != null ? `${st.coverage_pct}%` : "—"}
              </strong>{" "}
              matched to an account/contact
            </>
          )}
        </p>
      )}
      {st?.configured && (st.coverage_pct ?? 100) < 40 && (st.contacts_synced ?? 0) > 0 && (
        <div className="banner warn" style={{ fontSize: 12 }}>
          Only {st.coverage_pct}% of your PostHog users match a known Salesforce contact or
          account — account-level product signals will be sparse until your product calls
          <code> identify(email)</code>.
        </div>
      )}

      <label className="col" style={{ gap: 2, fontSize: 12 }}>
        Host
        <input value={host} onChange={(e) => setHost(e.target.value)}
          placeholder="https://us.posthog.com" />
      </label>
      <label className="col" style={{ gap: 2, fontSize: 12, marginTop: 8 }}>
        Project ID
        <input value={projectId} onChange={(e) => setProjectId(e.target.value)}
          placeholder="12345" />
      </label>
      <label className="col" style={{ gap: 2, fontSize: 12, marginTop: 8 }}>
        Milestone events (comma-separated — the ones worth surfacing on a case)
        <input value={milestones} onChange={(e) => setMilestones(e.target.value)}
          placeholder="onboarding_completed, api_key_invalid, export_started" />
      </label>
      <label className="col" style={{ gap: 2, fontSize: 12, marginTop: 8 }}>
        Personal API key {st?.has_credentials && <span className="muted">(leave blank to keep the stored one)</span>}
        <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)}
          placeholder="phx_..." />
      </label>

      {err && <Banner tone="exception" title={err} />}
      {msg && <Banner tone="accent" title={msg} />}

      <div className="row" style={{ gap: 6, marginTop: 12 }}>
        <button className="primary" disabled={busy}
          onClick={() => run(() => api.posthog.save(payload), "saved")}>
          {st?.configured ? "Save" : "Connect PostHog"}
        </button>
        <button disabled={busy || !projectId.trim()} onClick={testConn}>
          Test connection
        </button>
        {st?.configured && (
          <ConfirmButton
            label="Disconnect"
            variant="secondary"
            disabled={busy}
            title="Disconnect PostHog?"
            confirmLabel="Disconnect"
            onConfirm={() => run(() => api.posthog.remove(tenantId), "disconnected")}
          />
        )}
      </div>
    </div>
  );
}

function EmailPanel({ tenantId }: { tenantId: string }) {
  const [ch, setCh] = useState<EmailChannel | null>(null);
  const [f, setF] = useState<Form>(BLANK);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function load() {
    api.email
      .status(tenantId)
      .then((s) => {
        setCh(s);
        if (s.configured) setF(fromChannel(s));
      })
      .catch((e: ApiError) => setErr(e.message));
  }
  useEffect(load, [tenantId]);

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((p) => ({ ...p, [k]: v }));

  const payload = useMemo<EmailChannelSave>(() => {
    const b: EmailChannelSave = {
      provider: f.provider, tenant_id: tenantId, team: f.team.trim() || "support",
      from_name: f.from_name.trim() || undefined,
      no_reply_addr: f.no_reply_addr.trim() || undefined,
      auto_send_enabled: f.auto_send_enabled, active: f.active,
    };
    if (f.provider === "imap") {
      Object.assign(b, {
        imap_host: f.imap_host.trim(), imap_port: f.imap_port,
        smtp_host: f.smtp_host.trim(), smtp_port: f.smtp_port,
        username: f.username.trim(), folder: f.folder.trim() || "INBOX",
        password: f.password || undefined,
      });
    }
    return b;
  }, [f, tenantId]);

  async function run<T>(fn: () => Promise<T>, ok: string) {
    setBusy(true); setErr(null); setMsg(null);
    try {
      await fn();
      setMsg(ok);
      load();
      setF((p) => ({ ...p, password: "" }));
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setBusy(false);
  }

  async function connectGmail() {
    setErr(null);
    try {
      const { url } = await api.email.googleAuthorize(tenantId);
      window.open(url, "_blank", "width=520,height=640");
      setMsg("Finish in the Google window, then refresh.");
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }

  const testConn = () =>
    run(async () => {
      const r = await api.email.test(payload);
      if (!r.ok) throw new ApiError(0, r.error || "connection failed");
    }, "connection ok");

  return (
    <div className="int-card">
      <h4>Email channel</h4>
      <p className="muted" style={{ fontSize: 12 }}>
        Point a support mailbox here and the platform will run each incoming
        message through this team's <strong>published</strong> flow. A reply is
        sent back only when the flow's confidence gate passes <em>and</em>{" "}
        auto-send is on below — otherwise the message is flagged for a human.
        The password / Google token is stored encrypted (Supabase Vault) and
        never shown again.
      </p>

      {ch && ch.configured && (
        <div className={`banner ${ch.status === "error" ? "err" : "ok"}`} style={{ marginBottom: 10 }}>
          status: <strong>{ch.status}</strong>
          {ch.last_poll_at && ` · last poll ${new Date(ch.last_poll_at).toLocaleString()}`}
          {ch.last_error && ` · ${ch.last_error}`}
        </div>
      )}

      <div className="field">
        <label>provider</label>
        <div className="row" style={{ gap: 12 }}>
          <label className="row" style={{ gap: 4 }}>
            <input type="radio" style={{ width: "auto" }} checked={f.provider === "imap"}
              onChange={() => set("provider", "imap")} /> IMAP / SMTP
          </label>
          <label className="row" style={{ gap: 4 }}>
            <input type="radio" style={{ width: "auto" }} checked={f.provider === "gmail"}
              onChange={() => set("provider", "gmail")}
              disabled={!ch?.gmail_available} /> Gmail
            {!ch?.gmail_available && <span className="muted">(not configured on server)</span>}
          </label>
        </div>
      </div>

      <div className="row" style={{ gap: 6 }}>
        <div className="field" style={{ flex: 1 }}>
          <label>team (whose published flow runs)</label>
          <input value={f.team} onChange={(e) => set("team", e.target.value)} placeholder="support" />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label>from name</label>
          <input value={f.from_name} onChange={(e) => set("from_name", e.target.value)}
            placeholder="Acme Support" />
        </div>
      </div>

      {f.provider === "gmail" ? (
        <div className="field">
          <button onClick={connectGmail} disabled={!ch?.gmail_available}>
            {ch?.configured && ch.provider === "gmail" ? "Reconnect Gmail" : "Connect Gmail"}
          </button>
          {ch?.provider === "gmail" && ch.username && (
            <div className="muted" style={{ fontSize: 12 }}>connected as {ch.username}</div>
          )}
        </div>
      ) : (
        <>
          <div className="row" style={{ gap: 6 }}>
            <div className="field" style={{ flex: 2 }}>
              <label>IMAP host</label>
              <input value={f.imap_host} onChange={(e) => set("imap_host", e.target.value)}
                placeholder="imap.gmail.com" />
            </div>
            <div className="field" style={{ width: 90 }}>
              <label>port</label>
              <input type="number" value={f.imap_port}
                onChange={(e) => set("imap_port", parseInt(e.target.value, 10) || 993)} />
            </div>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <div className="field" style={{ flex: 2 }}>
              <label>SMTP host</label>
              <input value={f.smtp_host} onChange={(e) => set("smtp_host", e.target.value)}
                placeholder="smtp.gmail.com" />
            </div>
            <div className="field" style={{ width: 90 }}>
              <label>port</label>
              <input type="number" value={f.smtp_port}
                onChange={(e) => set("smtp_port", parseInt(e.target.value, 10) || 587)} />
            </div>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <div className="field" style={{ flex: 1 }}>
              <label>mailbox address / login</label>
              <input value={f.username} onChange={(e) => set("username", e.target.value)}
                placeholder="support@acme.com" />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label>app password {ch?.configured && <span className="muted">(leave blank to keep)</span>}</label>
              <input type="password" value={f.password}
                onChange={(e) => set("password", e.target.value)} placeholder="••••••••••••" />
            </div>
          </div>
          <div className="field">
            <label>folder to poll</label>
            <input value={f.folder} onChange={(e) => set("folder", e.target.value)} placeholder="INBOX" />
          </div>
        </>
      )}

      <div className="field">
        <label>reply-from address <span className="muted">(optional; default = the mailbox)</span></label>
        <input value={f.no_reply_addr} onChange={(e) => set("no_reply_addr", e.target.value)}
          placeholder="no-reply@acme.com" />
      </div>

      <label className="row" style={{ gap: 6, margin: "6px 0" }}>
        <input type="checkbox" style={{ width: "auto" }} checked={f.auto_send_enabled}
          onChange={(e) => set("auto_send_enabled", e.target.checked)} />
        <strong>auto-send replies</strong> — off = every reply waits for a human
      </label>
      <label className="row" style={{ gap: 6, marginBottom: 10 }}>
        <input type="checkbox" style={{ width: "auto" }} checked={f.active}
          onChange={(e) => set("active", e.target.checked)} />
        poll this mailbox (active)
      </label>

      {err && <Banner tone="exception" title={err} />}
      {msg && <Banner tone="accent" title={msg} />}

      <div className="row" style={{ gap: 6, marginTop: 10 }}>
        {f.provider === "imap" && (
          <button onClick={testConn} disabled={busy || !f.imap_host || !f.username}>
            Test connection
          </button>
        )}
        <button className="primary" disabled={busy} onClick={() => run(() => api.email.save(payload), "saved")}>
          Save
        </button>
        {ch?.configured && (
          <ConfirmButton
            label="Disconnect"
            disabled={busy}
            title="Disconnect this mailbox?"
            confirmLabel="Disconnect"
            onConfirm={() => run(() => api.email.remove(tenantId), "disconnected")}
          />
        )}
      </div>
    </div>
  );
}

// ── Freshchat: the first pluggable chat/call channel (multi-provider
// connectors step 3) ────────────────────────────────────────────────────
type FreshchatForm = {
  domain: string;
  team: string;
  api_token: string;
  webhook_public_key: string;
  auto_send_enabled: boolean;
  oauth_domain: string;
  client_id: string;
  client_secret: string;
};

const FRESHCHAT_BLANK: FreshchatForm = {
  domain: "", team: "support", api_token: "", webhook_public_key: "", auto_send_enabled: false,
  oauth_domain: "", client_id: "", client_secret: "",
};

function fromFreshchatChannel(ch: FreshchatChannel): FreshchatForm {
  return {
    ...FRESHCHAT_BLANK,
    domain: ch.domain ?? "",
    team: ch.team ?? "support",
    auto_send_enabled: ch.auto_send_enabled ?? false,
  };
}

function FreshchatPanel({ tenantId }: { tenantId: string }) {
  const [ch, setCh] = useState<FreshchatChannel | null>(null);
  const [f, setF] = useState<FreshchatForm>(FRESHCHAT_BLANK);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function load() {
    api.freshchat
      .status(tenantId)
      .then((s) => {
        setCh(s);
        if (s.configured) setF(fromFreshchatChannel(s));
      })
      .catch((e: ApiError) => setErr(e.message));
    api.freshchat.webhookUrl(tenantId).then((r) => setWebhookUrl(r.url)).catch(() => {});
  }
  useEffect(load, [tenantId]);

  const set = <K extends keyof FreshchatForm>(k: K, v: FreshchatForm[K]) =>
    setF((p) => ({ ...p, [k]: v }));

  const payload = useMemo<FreshchatChannelSave>(() => ({
    tenant_id: tenantId, domain: f.domain.trim(), team: f.team.trim() || "support",
    auto_send_enabled: f.auto_send_enabled,
    api_token: f.api_token || undefined,
    webhook_public_key: f.webhook_public_key || undefined,
    oauth_domain: f.oauth_domain.trim() || undefined,
    client_id: f.client_id || undefined,
    client_secret: f.client_secret || undefined,
  }), [f, tenantId]);

  async function run<T>(fn: () => Promise<T>, ok: string) {
    setBusy(true); setErr(null); setMsg(null);
    try {
      await fn();
      setMsg(ok);
      load();
      setF((p) => ({ ...p, api_token: "", webhook_public_key: "", client_id: "", client_secret: "" }));
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setBusy(false);
  }

  const testConn = () =>
    run(async () => {
      const r = await api.freshchat.test(payload);
      if (!r.ok) throw new ApiError(0, r.error || "connection failed");
    }, "connection ok");

  async function connectOAuth() {
    setBusy(true); setErr(null); setMsg(null);
    try {
      if (f.client_id || f.client_secret || !ch?.oauth_client_configured) {
        await api.freshchat.save(payload);   // persist a freshly-typed client before authorizing
      }
      const { url } = await api.freshchat.oauthAuthorize(tenantId);
      const w = window.open(url, "freshchat-oauth", "width=520,height=720");
      const t = setInterval(() => {
        if (w?.closed) {
          clearInterval(t);
          load();
        }
      }, 800);
      setF((p) => ({ ...p, client_id: "", client_secret: "" }));
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setBusy(false);
  }

  return (
    <div className="int-card">
      <h4>Freshchat channel</h4>
      <p className="muted" style={{ fontSize: 12 }}>
        Connect a Freshchat account and the platform will run each incoming
        chat message through this team's <strong>published</strong> flow — the
        same pipeline email uses (triage, draft, confidence gate), just a
        different door in. A reply is sent back into the conversation only
        when the flow's confidence gate passes <em>and</em> auto-send is on
        below — otherwise it's flagged for a human. The API token / webhook
        key are stored encrypted (Supabase Vault) and never shown again.
      </p>

      {ch && ch.configured && (
        <div className={`banner ${ch.status === "error" ? "err" : "ok"}`} style={{ marginBottom: 10 }}>
          status: <strong>{ch.status}</strong>
          {ch.signature_verification === false &&
            " · no webhook key saved yet — inbound messages will be rejected"}
        </div>
      )}

      <div className="row" style={{ gap: 6 }}>
        <div className="field" style={{ flex: 2 }}>
          <label>Freshchat domain</label>
          <input value={f.domain} onChange={(e) => set("domain", e.target.value)}
            placeholder="yourcompany.freshchat.com" />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label>team (whose published flow runs)</label>
          <input value={f.team} onChange={(e) => set("team", e.target.value)} placeholder="support" />
        </div>
      </div>

      <div className="field">
        <label>API token {ch?.configured && <span className="muted">(leave blank to keep)</span>}</label>
        <input type="password" value={f.api_token}
          onChange={(e) => set("api_token", e.target.value)} placeholder="API token" />
        <span className="muted" style={{ fontSize: 12 }}>
          Freshchat admin console → Settings → API tokens (Admin API scope). Skip this if your
          account only has a Custom/External App — use OAuth below instead.
        </span>
      </div>

      <div className="col" style={{ gap: 8, borderTop: "1px solid var(--hair,#ddd)", paddingTop: 12, marginTop: 4 }}>
        <strong style={{ fontSize: 13 }}>Or connect via OAuth</strong>
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          For an account whose only credential is a Custom/External App (client_id + client_secret,
          not a per-agent API token). Save the client below, then authorize in a popup.
        </p>
        <div className="row" style={{ gap: 6 }}>
          <div className="field" style={{ flex: 2 }}>
            <label>OAuth domain (if different from above)</label>
            <input value={f.oauth_domain} onChange={(e) => set("oauth_domain", e.target.value)}
              placeholder="yourcompany.myfreshworks.com" />
          </div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <div className="field" style={{ flex: 1 }}>
            <label>Client ID {ch?.oauth_client_configured && <span className="muted">(leave blank to keep)</span>}</label>
            <input value={f.client_id} onChange={(e) => set("client_id", e.target.value)}
              placeholder="fw_ext_..." />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>Client secret {ch?.oauth_client_configured && <span className="muted">(leave blank to keep)</span>}</label>
            <input type="password" value={f.client_secret}
              onChange={(e) => set("client_secret", e.target.value)} placeholder="Client secret" />
          </div>
        </div>
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <button disabled={busy || (!f.client_id && !ch?.oauth_client_configured)}
            onClick={connectOAuth}>
            Connect via OAuth
          </button>
          {ch?.oauth && <span className="muted" style={{ fontSize: 12 }}>authorized ✓</span>}
          {ch?.oauth_client_configured && !ch?.oauth && (
            <span className="muted" style={{ fontSize: 12 }}>client saved, not yet authorized</span>
          )}
        </div>
      </div>

      <div className="field">
        <label>Webhook public key {ch?.configured && <span className="muted">(leave blank to keep)</span>}</label>
        <textarea rows={3} value={f.webhook_public_key}
          onChange={(e) => set("webhook_public_key", e.target.value)}
          placeholder="-----BEGIN PUBLIC KEY-----" />
        <span className="muted" style={{ fontSize: 12 }}>
          Shown next to the webhook URL below when you set it up in Freshchat —
          used to verify every inbound webhook really came from Freshchat.
        </span>
      </div>

      {webhookUrl && (
        <div className="field">
          <label>webhook URL to paste into Freshchat</label>
          <input readOnly value={webhookUrl} onFocus={(e) => e.target.select()} />
        </div>
      )}

      <label className="row" style={{ gap: 6, margin: "6px 0 10px" }}>
        <input type="checkbox" style={{ width: "auto" }} checked={f.auto_send_enabled}
          onChange={(e) => set("auto_send_enabled", e.target.checked)} />
        <strong>auto-send replies</strong> — off = every reply waits for a human
      </label>

      {err && <Banner tone="exception" title={err} />}
      {msg && <Banner tone="accent" title={msg} />}

      <div className="row" style={{ gap: 6, marginTop: 10 }}>
        <button onClick={testConn} disabled={busy || !f.domain}>
          Test connection
        </button>
        <button className="primary" disabled={busy || !f.domain}
          onClick={() => run(() => api.freshchat.save(payload), "saved")}>
          Save
        </button>
        {ch?.configured && (
          <ConfirmButton
            label="Disconnect"
            disabled={busy}
            title="Disconnect Freshchat?"
            confirmLabel="Disconnect"
            onConfirm={() => run(() => api.freshchat.remove(tenantId), "disconnected")}
          />
        )}
      </div>
    </div>
  );
}
