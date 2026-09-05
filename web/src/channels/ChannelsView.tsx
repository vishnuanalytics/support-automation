import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { EmailChannel, EmailChannelSave, FreshchatChannel, FreshchatChannelSave } from "../types";

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
    <div style={{ overflow: "auto", height: "100%" }}>
      <EmailPanel tenantId={tenantId} />
      <div style={{ borderTop: "1px solid var(--border)", margin: "8px 0" }} />
      <FreshchatPanel tenantId={tenantId} />
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
    <div className="pane" style={{ overflow: "auto", padding: 16, maxWidth: 640 }}>
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

      {err && <div className="banner err">{err}</div>}
      {msg && <div className="banner ok">{msg}</div>}

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
          <button className="err" disabled={busy}
            onClick={() => confirm("Disconnect this mailbox?") &&
              run(() => api.email.remove(tenantId), "disconnected")}>
            Disconnect
          </button>
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
};

const FRESHCHAT_BLANK: FreshchatForm = {
  domain: "", team: "support", api_token: "", webhook_public_key: "", auto_send_enabled: false,
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
  }), [f, tenantId]);

  async function run<T>(fn: () => Promise<T>, ok: string) {
    setBusy(true); setErr(null); setMsg(null);
    try {
      await fn();
      setMsg(ok);
      load();
      setF((p) => ({ ...p, api_token: "", webhook_public_key: "" }));
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

  return (
    <div className="pane" style={{ overflow: "auto", padding: 16, maxWidth: 640 }}>
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
          onChange={(e) => set("api_token", e.target.value)} placeholder="••••••••••••" />
        <span className="muted" style={{ fontSize: 12 }}>
          Freshchat admin console → Settings → API tokens (Admin API scope).
        </span>
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

      {err && <div className="banner err">{err}</div>}
      {msg && <div className="banner ok">{msg}</div>}

      <div className="row" style={{ gap: 6, marginTop: 10 }}>
        <button onClick={testConn} disabled={busy || !f.domain}>
          Test connection
        </button>
        <button className="primary" disabled={busy || !f.domain}
          onClick={() => run(() => api.freshchat.save(payload), "saved")}>
          Save
        </button>
        {ch?.configured && (
          <button className="err" disabled={busy}
            onClick={() => confirm("Disconnect Freshchat?") &&
              run(() => api.freshchat.remove(tenantId), "disconnected")}>
            Disconnect
          </button>
        )}
      </div>
    </div>
  );
}
