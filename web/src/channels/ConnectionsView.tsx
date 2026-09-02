import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { Connection } from "../types";

const AUTH_TYPES = ["none", "bearer", "header", "basic"] as const;

/** P6c — per-tenant HTTP connections for the `http_request` flow node. */
export function ConnectionsView() {
  const [rows, setRows] = useState<Connection[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
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
    api.connections.list().then(setRows).catch((e: ApiError) => setErr(e.message));
  };
  useEffect(load, []);

  const add = async () => {
    setBusy(true);
    setErr(null);
    try {
      const auth: Record<string, unknown> = { type: f.type };
      if (f.type === "bearer") auth.token = f.token;
      if (f.type === "header") { auth.header_name = f.header_name; auth.value = f.value; }
      if (f.type === "basic") { auth.username = f.username; auth.password = f.password; }
      await api.connections.create({ slug: f.slug.trim(), base_url: f.base_url.trim(), auth });
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
          </tr>
        </thead>
        <tbody>
          {rows?.map((c) => (
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
                <button
                  className="err"
                  onClick={() => api.connections.remove(c.slug).then(load).catch(() => {})}
                >
                  delete
                </button>
              </td>
            </tr>
          ))}
          {rows && rows.length === 0 && (
            <tr>
              <td colSpan={4} className="muted">
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
    </div>
  );
}
