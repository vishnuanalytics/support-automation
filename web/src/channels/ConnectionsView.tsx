import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type {
  Connection, ConnectionAction, SalesforceOrg, SalesforceOrgSchema,
  ZendeskConnection, ZendeskConnectionSave,
} from "../types";

const AUTH_TYPES = ["none", "bearer", "header", "basic"] as const;

/** P6c — per-tenant HTTP connections for the `http_request` flow node.
 * FR-47 — a connection can also carry named, reusable *actions*, turning it
 * into a connector next to the salesforce/slack builtins (GET /api/connectors),
 * usable from any flow's `connector_action` node with zero Python changes. */
export function ConnectionsView({ tenantId }: { tenantId: string }) {
  const [rows, setRows] = useState<Connection[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [f, setF] = useState({
    slug: "",
    base_url: "",
    type: "none" as (typeof AUTH_TYPES)[number],
    token: "",
    header_name: "",
    value: "",
    username: "",
    password: "",
  });

  const load = () => {
    api.connections.list(tenantId).then(setRows).catch((e: ApiError) => setErr(e.message));
  };
  useEffect(load, [tenantId]);

  const add = async () => {
    setBusy(true);
    setErr(null);
    try {
      const auth: Record<string, unknown> = { type: f.type };
      if (f.type === "bearer") auth.token = f.token;
      if (f.type === "header") { auth.header_name = f.header_name; auth.value = f.value; }
      if (f.type === "basic") { auth.username = f.username; auth.password = f.password; }
      await api.connections.create({ slug: f.slug.trim(), base_url: f.base_url.trim(), auth, tenant_id: tenantId });
      setF({ ...f, slug: "", base_url: "", token: "", value: "", password: "" });
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pane col" style={{ gap: 16, maxWidth: 760 }}>
      <h2 style={{ margin: 0 }}>Connections</h2>
      <p style={{ margin: 0, color: "var(--muted, #667)" }}>
        A named base URL + credentials an <code>http_request</code> flow node can call.
        The secret is stored server-side and never shown again.
      </p>

      {err && <div className="banner err">{err}</div>}

      <table className="runs-table">
        <thead>
          <tr>
            <th>slug</th>
            <th>base URL</th>
            <th>auth</th>
            <th />
            <th />
          </tr>
        </thead>
        <tbody>
          {rows?.map((c) => (
            <>
              <tr key={c.slug}>
                <td>
                  <code>{c.slug}</code>
                </td>
                <td>{c.base_url}</td>
                <td>
                  {c.auth.type || "none"}
                  {c.has_secret && " 🔒"}
                </td>
                <td>
                  <button onClick={() => setExpanded(expanded === c.slug ? null : c.slug)}>
                    {expanded === c.slug ? "hide actions" : "manage actions"}
                  </button>
                </td>
                <td>
                  <button
                    className="err"
                    onClick={() => api.connections.remove(c.slug, tenantId).then(load).catch(() => {})}
                  >
                    delete
                  </button>
                </td>
              </tr>
              {expanded === c.slug && (
                <tr key={`${c.slug}-actions`}>
                  <td colSpan={5}>
                    <ConnectionActionsPanel slug={c.slug} tenantId={tenantId} />
                  </td>
                </tr>
              )}
            </>
          ))}
          {rows && rows.length === 0 && (
            <tr>
              <td colSpan={5} className="muted">
                none yet
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="col" style={{ gap: 8, borderTop: "1px solid var(--hair,#ddd)", paddingTop: 12 }}>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <input
            placeholder="slug (e.g. vendor-api)"
            value={f.slug}
            onChange={(e) => setF({ ...f, slug: e.target.value })}
          />
          <input
            placeholder="https://api.vendor.com"
            value={f.base_url}
            style={{ minWidth: 240 }}
            onChange={(e) => setF({ ...f, base_url: e.target.value })}
          />
          <select value={f.type} onChange={(e) => setF({ ...f, type: e.target.value as never })}>
            {AUTH_TYPES.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          {f.type === "bearer" && (
            <input
              placeholder="token"
              value={f.token}
              onChange={(e) => setF({ ...f, token: e.target.value })}
            />
          )}
          {f.type === "header" && (
            <>
              <input
                placeholder="header name"
                value={f.header_name}
                onChange={(e) => setF({ ...f, header_name: e.target.value })}
              />
              <input
                placeholder="header value"
                value={f.value}
                onChange={(e) => setF({ ...f, value: e.target.value })}
              />
            </>
          )}
          {f.type === "basic" && (
            <>
              <input
                placeholder="username"
                value={f.username}
                onChange={(e) => setF({ ...f, username: e.target.value })}
              />
              <input
                placeholder="password"
                type="password"
                value={f.password}
                onChange={(e) => setF({ ...f, password: e.target.value })}
              />
            </>
          )}
          <button
            className="primary"
            disabled={busy || !f.slug.trim() || !f.base_url.trim()}
            onClick={add}
          >
            Add connection
          </button>
        </div>
      </div>

      <CaseConnectorPicker tenantId={tenantId} />
      <SalesforceOrgsPanel tenantId={tenantId} />
      <ZendeskPanel tenantId={tenantId} />
      <AiModelsPanel tenantId={tenantId} />
    </div>
  );
}

/** Multi-provider connectors step 1 (FR-51) — which connected system this
 * tenant's case-touching nodes (sf_case/sf_writeback/ask_human/handover/
 * identify/clarify/notify_human) write to by default. Hardcoded to the two
 * REAL implementations that exist (`salesforce`, `zendesk`) rather than a
 * free-text box — a typo'd connector slug here would silently escalate
 * every case (connectors.resolve_case_connector falls back to Salesforce
 * only when the row is *missing*, not when it names something that
 * doesn't implement the CASE_ACTIONS contract). A per-node `connector`
 * override (in that node's own JSON config) still beats this default. */
function CaseConnectorPicker({ tenantId }: { tenantId: string }) {
  const [value, setValue] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    api.caseConnector.get(tenantId).then((r) => setValue(r.case_connector)).catch(() => setValue("salesforce"));
  }, [tenantId]);

  const save = async (next: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await api.caseConnector.set(next, tenantId);
      setValue(next);
      setMsg("saved");
    } catch (e) {
      setMsg(`✗ ${(e as ApiError).message}`);
    } finally {
      setBusy(false);
    }
  };

  if (value === null) return null;
  return (
    <div className="col" style={{ gap: 8, borderTop: "1px solid var(--hair,#ddd)", paddingTop: 16 }}>
      <h3 style={{ margin: 0 }}>Case system</h3>
      <p style={{ margin: 0, color: "var(--muted, #667)" }}>
        Which connected system your case-touching flow nodes (routing, notes, assignment,
        replies) write to by default. Connect it below before switching to it.
      </p>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <select value={value} disabled={busy} onChange={(e) => save(e.target.value)}>
          <option value="salesforce">Salesforce</option>
          <option value="zendesk">Zendesk</option>
        </select>
        {msg && <span className="muted" style={{ fontSize: 12 }}>{msg}</span>}
      </div>
    </div>
  );
}

/** Multi-provider connectors step 2 — Zendesk as a second real case system
 * (see interpreter/zendesk.py for the mapping notes / honest gaps vs.
 * Salesforce's data model). One connection per tenant (unlike Salesforce,
 * Zendesk isn't multi-org here), same shape as the email/Freshchat panels. */
function ZendeskPanel({ tenantId }: { tenantId: string }) {
  const [ch, setCh] = useState<ZendeskConnection | null>(null);
  const [f, setF] = useState({ subdomain: "", email: "", api_token: "" });
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => {
    api.zendesk
      .status(tenantId)
      .then((s) => {
        setCh(s);
        if (s.configured) setF((p) => ({ ...p, subdomain: s.subdomain ?? "", email: s.email ?? "" }));
      })
      .catch((e: ApiError) => setErr(e.message));
  };
  useEffect(load, [tenantId]);

  const payload = useMemo<ZendeskConnectionSave>(() => ({
    tenant_id: tenantId, subdomain: f.subdomain.trim(), email: f.email.trim(),
    api_token: f.api_token || undefined,
  }), [f, tenantId]);

  async function run<T>(fn: () => Promise<T>, ok: string) {
    setBusy(true); setErr(null); setMsg(null);
    try {
      await fn();
      setMsg(ok);
      load();
      setF((p) => ({ ...p, api_token: "" }));
    } catch (e) {
      setErr((e as ApiError).message);
    }
    setBusy(false);
  }

  const testConn = () =>
    run(async () => {
      const r = await api.zendesk.test(payload);
      if (!r.ok) throw new ApiError(0, r.error || "connection failed");
    }, "connection ok");

  return (
    <div className="col" style={{ gap: 8, borderTop: "1px solid var(--hair,#ddd)", paddingTop: 16 }}>
      <h3 style={{ margin: 0 }}>Zendesk</h3>
      <p style={{ margin: 0, color: "var(--muted, #667)" }}>
        Connect a Zendesk account to use it as this tenant's case system (pick it above once
        connected). Ticket comments/status/assignment map onto Zendesk's own model — see the
        Zendesk connector's own notes for where that mapping is intentionally partial.
      </p>
      {ch?.configured && (
        <div className={`banner ${ch.status === "error" ? "err" : "ok"}`}>status: <strong>{ch.status}</strong></div>
      )}
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <input placeholder="subdomain (yourcompany)" value={f.subdomain}
          onChange={(e) => setF({ ...f, subdomain: e.target.value })} />
        <input placeholder="agent email (bot@yourcompany.com)" value={f.email}
          style={{ minWidth: 220 }} onChange={(e) => setF({ ...f, email: e.target.value })} />
        <input type="password" placeholder={ch?.configured ? "API token (leave blank to keep)" : "API token"}
          value={f.api_token} onChange={(e) => setF({ ...f, api_token: e.target.value })} />
      </div>
      <span className="muted" style={{ fontSize: 12 }}>
        Zendesk admin console → Apps and integrations → APIs → API tokens.
      </span>

      {err && <div className="banner err">{err}</div>}
      {msg && <div className="banner ok">{msg}</div>}

      <div className="row" style={{ gap: 6 }}>
        <button onClick={testConn} disabled={busy || !f.subdomain || !f.email}>Test connection</button>
        <button className="primary" disabled={busy || !f.subdomain || !f.email}
          onClick={() => run(() => api.zendesk.save(payload), "saved")}>
          Save
        </button>
        {ch?.configured && (
          <button className="err" disabled={busy}
            onClick={() => confirm("Disconnect Zendesk?") && run(() => api.zendesk.remove(tenantId), "disconnected")}>
            Disconnect
          </button>
        )}
      </div>
    </div>
  );
}

/** FR-47 — named, reusable actions on one connection (e.g. "create_ticket"
 * on a "zendesk" connection): method + path + params it declares (drives the
 * flow editor's generic connector_action form) + an optional body template.
 * Both `path` and `body_template` support "{{ param_key }}" substitution over
 * the action's own params at run time (interpreter/connections.py::_fill). */
function ConnectionActionsPanel({ slug, tenantId }: { slug: string; tenantId: string }) {
  const [rows, setRows] = useState<ConnectionAction[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [f, setF] = useState({ name: "", method: "GET", path: "", paramsJson: "[]", bodyJson: "" });
  const [jsonErr, setJsonErr] = useState<string | null>(null);

  const load = () => {
    api.connections.actions.list(slug, tenantId).then(setRows).catch((e: ApiError) => setErr(e.message));
  };
  useEffect(load, [slug, tenantId]);

  const add = async () => {
    setJsonErr(null);
    let params: ConnectionAction["params"];
    let body_template: unknown;
    try {
      params = JSON.parse(f.paramsJson || "[]");
      body_template = f.bodyJson.trim() ? JSON.parse(f.bodyJson) : undefined;
    } catch (e) {
      setJsonErr((e as Error).message);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.connections.actions.save(
        slug, { name: f.name.trim(), method: f.method, path: f.path.trim(), params, body_template }, tenantId);
      setF({ name: "", method: "GET", path: "", paramsJson: "[]", bodyJson: "" });
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="col" style={{ gap: 8, padding: "8px 0" }}>
      <p className="muted" style={{ margin: 0, fontSize: 12 }}>
        Actions this connection exposes to any flow's <code>connector_action</code> node —
        e.g. a Zendesk connection's <code>create_ticket</code>.{" "}
        <code>path</code>/<code>body_template</code> support{" "}
        <code>{"{{ param_key }}"}</code> from the action's own params.
      </p>
      {err && <div className="banner err">{err}</div>}
      <table className="runs-table">
        <thead>
          <tr>
            <th>name</th>
            <th>method</th>
            <th>path</th>
            <th>params</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows?.map((a) => (
            <tr key={a.name}>
              <td><code>{a.name}</code></td>
              <td>{a.method}</td>
              <td><code>{a.path}</code></td>
              <td className="muted">{a.params.map((p) => p.key).join(", ") || "—"}</td>
              <td>
                <button className="err"
                        onClick={() => api.connections.actions.remove(slug, a.name, tenantId).then(load).catch(() => {})}>
                  delete
                </button>
              </td>
            </tr>
          ))}
          {rows && rows.length === 0 && (
            <tr><td colSpan={5} className="muted">no actions saved yet</td></tr>
          )}
        </tbody>
      </table>

      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <input placeholder="name (e.g. create_ticket)" value={f.name}
               onChange={(e) => setF({ ...f, name: e.target.value })} />
        <select value={f.method} onChange={(e) => setF({ ...f, method: e.target.value })}>
          {["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => <option key={m}>{m}</option>)}
        </select>
        <input placeholder="/tickets/{{ id }}.json" value={f.path} style={{ minWidth: 220 }}
               onChange={(e) => setF({ ...f, path: e.target.value })} />
      </div>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <textarea rows={3} style={{ flex: 1, fontFamily: "monospace", fontSize: 12 }}
                  placeholder='params: [{"key":"id","label":"Id","type":"template","required":true}]'
                  value={f.paramsJson} onChange={(e) => setF({ ...f, paramsJson: e.target.value })} />
        <textarea rows={3} style={{ flex: 1, fontFamily: "monospace", fontSize: 12 }}
                  placeholder='body_template (optional): {"ticket":{"subject":"{{ subject }}"}}'
                  value={f.bodyJson} onChange={(e) => setF({ ...f, bodyJson: e.target.value })} />
      </div>
      {jsonErr && <div className="err" style={{ fontSize: 11 }}>{jsonErr}</div>}
      <div>
        <button className="primary" disabled={busy || !f.name.trim() || !f.path.trim()} onClick={add}>
          Save action
        </button>
      </div>
    </div>
  );
}

const LLM_PROVIDERS = [
  { key: "groq", label: "Groq", hint: "the default — free tier, no key needed to start" },
  { key: "anthropic", label: "Anthropic (Claude)", hint: "paid — set a model to claude-* in a node to use it" },
  { key: "openrouter", label: "OpenRouter", hint: "free-tier fallback models, plus paid ones with your own key" },
] as const;

/** BYOK (2026-09-04) — a tenant can paste their own API key per LLM
 * provider instead of sharing this deployment's own keys. Never required:
 * every provider without a tenant key falls back to the platform's own
 * (Groq works out of the box with neither). */
function AiModelsPanel({ tenantId }: { tenantId: string }) {
  const [status, setStatus] = useState<import("../types").LlmKeyStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const load = () => {
    api.llmKeys.status(tenantId).then(setStatus).catch((e: ApiError) => setErr(e.message));
  };
  useEffect(load, [tenantId]);

  const save = async (provider: string) => {
    const key = (drafts[provider] || "").trim();
    if (!key) return;
    setBusy(provider);
    setErr(null);
    try {
      await api.llmKeys.save({ provider, api_key: key, tenant_id: tenantId });
      setDrafts({ ...drafts, [provider]: "" });
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  const remove = async (provider: string) => {
    setBusy(provider);
    try {
      await api.llmKeys.remove(provider, tenantId);
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="col" style={{ gap: 12, borderTop: "1px solid var(--hair,#ddd)", paddingTop: 16 }}>
      <h3 style={{ margin: 0 }}>AI models</h3>
      <p style={{ margin: 0, color: "var(--muted, #667)" }}>
        Every flow node that calls an LLM (drafting a reply, classifying, judging)
        uses this deployment's own key by default — Groq's free tier needs nothing
        set up. Paste your own key for a provider to use it (and its usage) instead,
        for every flow in this workspace. A node still picks which <em>model</em> to
        use — set that per-node in the editor.
      </p>
      {err && <div className="banner err">{err}</div>}
      {status && (
        <table className="runs-table">
          <thead>
            <tr>
              <th>provider</th>
              <th>status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {LLM_PROVIDERS.map((p) => {
              const mine = status.tenant[p.key];
              const platform = status.platform[p.key];
              return (
                <tr key={p.key}>
                  <td>
                    <b>{p.label}</b>
                    <div className="muted" style={{ fontSize: 11 }}>{p.hint}</div>
                  </td>
                  <td>
                    {mine ? (
                      <span className="ok">✓ your own key set</span>
                    ) : platform ? (
                      <span className="muted">using this deployment's key</span>
                    ) : (
                      <span className="muted">no key anywhere — falls back to another provider</span>
                    )}
                  </td>
                  <td>
                    <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                      {mine ? (
                        <button className="err" disabled={busy === p.key} onClick={() => remove(p.key)}>
                          {busy === p.key ? "removing…" : "remove"}
                        </button>
                      ) : (
                        <>
                          <input
                            type="password"
                            placeholder={`${p.label} API key`}
                            value={drafts[p.key] || ""}
                            style={{ width: 200 }}
                            onChange={(e) => setDrafts({ ...drafts, [p.key]: e.target.value })}
                          />
                          <button
                            disabled={busy === p.key || !(drafts[p.key] || "").trim()}
                            onClick={() => save(p.key)}
                          >
                            {busy === p.key ? "saving…" : "save"}
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** Self-serve Salesforce connection (2026-09-03) — a tenant can hold
 * several named orgs (e.g. "prod" + "sandbox"); each is JWT-bearer creds
 * saved server-side and never shown again. "Fetch from org" pulls the
 * org's real Case fields/picklist-values and Queues, so a future
 * field-mapping / dropdown UI has real data instead of hardcoded names —
 * not built yet, this panel is the connect + introspect foundation. */
function SalesforceOrgsPanel({ tenantId }: { tenantId: string }) {
  const [rows, setRows] = useState<SalesforceOrg[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testMsg, setTestMsg] = useState<string | null>(null);
  const [oauthMsg, setOauthMsg] = useState<string | null>(null);
  const [oauthConfigured, setOauthConfigured] = useState(false);
  const [schemaByOrg, setSchemaByOrg] = useState<Record<string, SalesforceOrgSchema | "loading" | "error">>({});
  const [f, setF] = useState({
    org_label: "default", SF_USERNAME: "", SF_CONSUMER_KEY: "",
    SF_PRIVATE_KEY: "", SF_DOMAIN: "",
  });

  const load = () => {
    api.salesforceOrgs.list(tenantId).then(setRows).catch((e: ApiError) => setErr(e.message));
  };
  useEffect(load, [tenantId]);
  useEffect(() => {
    api.salesforceOrgs.oauthStatus().then((s) => setOauthConfigured(s.configured)).catch(() => {});
  }, []);

  const connectOAuth = async () => {
    setOauthMsg(null);
    try {
      const { url } = await api.salesforceOrgs.oauthAuthorize({
        org_label: f.org_label.trim() || "default", domain: f.SF_DOMAIN.trim(), tenant_id: tenantId,
      });
      window.open(url, "_blank", "width=520,height=680");
      setOauthMsg("Finish in the Salesforce window, then refresh.");
    } catch (e) {
      setOauthMsg(`✗ ${(e as ApiError).message}`);
    }
  };

  const creds = () => {
    const c: Record<string, string> = { SF_USERNAME: f.SF_USERNAME.trim() };
    if (f.SF_CONSUMER_KEY.trim()) c.SF_CONSUMER_KEY = f.SF_CONSUMER_KEY.trim();
    if (f.SF_PRIVATE_KEY.trim()) c.SF_PRIVATE_KEY = f.SF_PRIVATE_KEY.trim();
    if (f.SF_DOMAIN.trim()) c.SF_DOMAIN = f.SF_DOMAIN.trim();
    return c;
  };

  const test = async () => {
    setBusy(true);
    setTestMsg(null);
    try {
      const r = await api.salesforceOrgs.test({ org_label: f.org_label.trim(), creds: creds(), tenant_id: tenantId });
      setTestMsg(r.ok ? "✓ connected" : `✗ ${r.error}`);
    } catch (e) {
      setTestMsg(`✗ ${(e as ApiError).message}`);
    } finally {
      setBusy(false);
    }
  };

  const connect = async () => {
    setBusy(true);
    setErr(null);
    try {
      await api.salesforceOrgs.connect({ org_label: f.org_label.trim(), creds: creds(), tenant_id: tenantId });
      setF({ ...f, SF_CONSUMER_KEY: "", SF_PRIVATE_KEY: "" });
      setTestMsg(null);
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const fetchSchema = async (orgLabel: string) => {
    setSchemaByOrg((s) => ({ ...s, [orgLabel]: "loading" }));
    try {
      const schema = await api.salesforceOrgs.schema(orgLabel, tenantId);
      setSchemaByOrg((s) => ({ ...s, [orgLabel]: schema }));
    } catch {
      setSchemaByOrg((s) => ({ ...s, [orgLabel]: "error" }));
    }
  };

  return (
    <div className="col" style={{ gap: 12, borderTop: "1px solid var(--hair,#ddd)", paddingTop: 16 }}>
      <h3 style={{ margin: 0 }}>Salesforce</h3>
      <p style={{ margin: 0, color: "var(--muted, #667)" }}>
        Connect one or more Salesforce orgs (e.g. a production org + a sandbox) using a Connected
        App's JWT bearer credentials. "Fetch from org" pulls your real Case fields, picklist values,
        and Queues — nothing about your org's setup is assumed or hardcoded.
      </p>

      <table className="runs-table">
        <thead>
          <tr>
            <th>org</th>
            <th>username</th>
            <th>domain</th>
            <th />
            <th />
          </tr>
        </thead>
        <tbody>
          {rows?.map((o) => {
            const schema = schemaByOrg[o.org_label];
            return (
              <tr key={o.org_label}>
                <td><code>{o.org_label}</code></td>
                <td>{o.SF_USERNAME || (o.SF_OAUTH_INSTANCE_URL ? "(via OAuth)" : "")}</td>
                <td>{o.SF_OAUTH_INSTANCE_URL || o.SF_DOMAIN || "login"}</td>
                <td>
                  <button disabled={schema === "loading"} onClick={() => fetchSchema(o.org_label)}>
                    {schema === "loading" ? "fetching…" : "fetch from org"}
                  </button>
                  {schema && schema !== "loading" && schema !== "error" && (
                    <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                      {schema.case_fields.length} Case fields · {schema.queues.length} queues
                      {schema.errors.length > 0 && ` (${schema.errors.join("; ")})`}
                    </span>
                  )}
                  {schema === "error" && <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>failed</span>}
                </td>
                <td>
                  <button
                    className="err"
                    onClick={() => api.salesforceOrgs.remove(o.org_label, tenantId).then(load).catch(() => {})}
                  >
                    disconnect
                  </button>
                </td>
              </tr>
            );
          })}
          {rows && rows.length === 0 && (
            <tr><td colSpan={5} className="muted">no Salesforce org connected yet</td></tr>
          )}
        </tbody>
      </table>

      {err && <div className="banner err">{err}</div>}

      <div className="col" style={{ gap: 8 }}>
        <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <input
            placeholder="org label (e.g. prod, sandbox)"
            value={f.org_label}
            onChange={(e) => setF({ ...f, org_label: e.target.value })}
          />
          <input
            placeholder="domain (blank = login, or 'test' for a sandbox)"
            value={f.SF_DOMAIN}
            style={{ minWidth: 220 }}
            onChange={(e) => setF({ ...f, SF_DOMAIN: e.target.value })}
          />
          {oauthConfigured ? (
            <button className="primary" onClick={connectOAuth}>
              Connect Salesforce
            </button>
          ) : (
            <span className="muted" style={{ fontSize: 12 }}>
              (one-click OAuth isn't set up on this server yet — use the JWT form below)
            </span>
          )}
          {oauthMsg && <span style={{ fontSize: 13 }}>{oauthMsg}</span>}
        </div>
      </div>

      <details>
        <summary className="muted" style={{ cursor: "pointer", fontSize: 13 }}>
          Advanced: connect with a Connected App's JWT bearer credentials directly
        </summary>
        <div className="col" style={{ gap: 8, marginTop: 8 }}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <input
              placeholder="integration user (e.g. bot@acme.com)"
              value={f.SF_USERNAME}
              style={{ minWidth: 220 }}
              onChange={(e) => setF({ ...f, SF_USERNAME: e.target.value })}
            />
            <input
              placeholder="Connected App consumer key"
              value={f.SF_CONSUMER_KEY}
              style={{ minWidth: 280 }}
              onChange={(e) => setF({ ...f, SF_CONSUMER_KEY: e.target.value })}
            />
          </div>
          <textarea
            placeholder="-----BEGIN PRIVATE KEY-----&#10;the Connected App's uploaded certificate's private key&#10;-----END PRIVATE KEY-----"
            value={f.SF_PRIVATE_KEY}
            rows={5}
            style={{ fontFamily: "monospace", fontSize: 12 }}
            onChange={(e) => setF({ ...f, SF_PRIVATE_KEY: e.target.value })}
          />
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <button disabled={busy || !f.SF_USERNAME.trim()} onClick={test}>Test connection</button>
            <button
              className="primary"
              disabled={busy || !f.org_label.trim() || !f.SF_USERNAME.trim()}
              onClick={connect}
            >
              Connect
            </button>
            {testMsg && <span style={{ fontSize: 13 }}>{testMsg}</span>}
          </div>
        </div>
      </details>
    </div>
  );
}
