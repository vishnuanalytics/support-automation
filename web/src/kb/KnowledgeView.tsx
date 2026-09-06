import { Fragment, useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type {
  KbCollection,
  KbConnection,
  KbConnector,
  KbDocDefaults,
  KbDocWriteback,
  KbEntry,
  KbEntryRow,
} from "../types";

/**
 * Self-serve knowledge base (Phase 14). Every tenant has one **org-level
 * collection** ("Organization knowledge") — the default target for every
 * connected source (docs/KB_SOURCE_CONNECTORS.md: crawl roots, Google
 * Docs/Sheets, Linear, Nolt) and what a chat flow's `retrieve` node reads
 * when it names no `kb_sources`. Extra per-team collections are optional on
 * top of it (most orgs just use the one).
 */
export function KnowledgeView({ tenantId }: { tenantId: string }) {
  const [cols, setCols] = useState<KbCollection[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const c = await api.kb.listCollections();
      setCols(c);
      // default to the org KB (server sorts it first)
      setSel((s) => s ?? c.find((x) => x.org_kb)?.source_id ?? c[0]?.source_id ?? null);
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function newCollection() {
    const name = prompt(
      "Name for an extra collection (optional — most orgs just use the org knowledge base):",
    )?.trim();
    if (!name) return;
    try {
      const { source_id } = await api.kb.createCollection({ name, tenant_id: tenantId });
      await refresh();
      setSel(source_id);
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  const orgKb = cols.filter((c) => c.org_kb);
  const teamCols = cols.filter((c) => !c.org_kb);

  const railBtn = (c: KbCollection) => (
    <button
      key={c.source_id}
      className={c.source_id === sel ? "primary" : ""}
      style={{ justifyContent: "space-between", display: "flex", gap: 6 }}
      onClick={() => setSel(c.source_id)}
    >
      <span>{c.name}</span>
      <span className="row" style={{ gap: 4 }}>
        {!!c.provisional_count && (
          <span
            title={`${c.provisional_count} entr${c.provisional_count === 1 ? "y" : "ies"} held pending review`}
            style={{ fontSize: 11, padding: "0 5px", borderRadius: 8, background: "#8a5a00", color: "#fff" }}
          >
            {c.provisional_count} held
          </span>
        )}
        <span className="muted">{c.entry_count}</span>
      </span>
    </button>
  );

  return (
    <div style={{ display: "flex", height: "100%", minHeight: 0 }}>
      <div
        className="col"
        style={{ width: 220, borderRight: "1px solid var(--border)", padding: 10, gap: 8, overflow: "auto" }}
      >
        <strong>Knowledge</strong>
        {err && <div className="err" style={{ fontSize: 12 }}>{err}</div>}
        <div className="col" style={{ gap: 2 }}>{orgKb.map(railBtn)}</div>

        <div className="row" style={{ justifyContent: "space-between", marginTop: 6 }}>
          <span className="muted" style={{ fontSize: 11 }}>Additional collections</span>
          <button
            style={{ fontSize: 11, padding: "0 6px" }}
            title="Optional — a separate collection a flow can scope to. Most orgs don't need one."
            onClick={newCollection}
          >
            ＋
          </button>
        </div>
        <div className="col" style={{ gap: 2 }}>
          {teamCols.map(railBtn)}
          {teamCols.length === 0 && (
            <div className="muted" style={{ fontSize: 11 }}>none — everything feeds the org KB</div>
          )}
        </div>
      </div>
      <div style={{ flex: 1, minWidth: 0, overflow: "auto" }}>
        {sel && cols.find((c) => c.source_id === sel) ? (
          <Collection
            key={sel}
            col={cols.find((c) => c.source_id === sel)!}
            onChange={refresh}
          />
        ) : (
          <div className="muted" style={{ display: "grid", placeItems: "center", height: "100%" }}>
            select a collection
          </div>
        )}
      </div>
    </div>
  );
}

function Collection({ col, onChange }: { col: KbCollection; onChange: () => void }) {
  const [entries, setEntries] = useState<KbEntryRow[]>([]);
  const [openId, setOpenId] = useState<string | "new" | null>(null);
  const [showRetired, setShowRetired] = useState(false);

  const [gApi, setGApi] = useState<{ configured: boolean; connected: boolean }>({
    configured: false,
    connected: false,
  });

  const load = useCallback(async () => {
    setEntries(await api.kb.listEntries(col.source_id));
  }, [col.source_id]);

  const loadGoogle = useCallback(async () => {
    try {
      const s = await api.google.status();
      setGApi({ configured: s.configured, connected: !!s.connected[col.tenant_id] });
    } catch {
      setGApi({ configured: false, connected: false });
    }
  }, [col.tenant_id]);

  useEffect(() => {
    void load();
    void loadGoogle();
  }, [load, loadGoogle]);

  async function removeCollection() {
    if (!confirm(`archive collection "${col.name}" and all its entries?`)) return;
    await api.kb.deleteCollection(col.source_id);
    onChange();
  }

  async function connectGoogle() {
    const { url } = await api.google.authorize(col.tenant_id);
    const w = window.open(url, "google-oauth", "width=520,height=640");
    const timer = setInterval(() => {
      if (w?.closed) {
        clearInterval(timer);
        void loadGoogle();
      }
    }, 800);
  }

  async function resync(entryId: string) {
    try {
      await api.kb.resyncGdoc(entryId);
      alert("Re-syncing this source in the background — updated entries will appear shortly.");
      void load();
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  async function uploadFile(file: File) {
    const b64 = await new Promise<string>((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(String(r.result));
      r.onerror = () => rej(r.error);
      r.readAsDataURL(file);
    });
    try {
      await api.kb.upload(col.source_id, { filename: file.name, content_b64: b64 });
      void load();
      onChange();
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  async function exportCollection() {
    try {
      const bundle = await api.kb.export(col.source_id);
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${col.name.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-backup.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  async function importBackup(file: File) {
    let bundle: { entries?: { title: string; body_md: string }[] };
    try {
      bundle = JSON.parse(await file.text());
    } catch {
      alert("not valid JSON");
      return;
    }
    const entries = bundle.entries;
    if (!Array.isArray(entries) || entries.length === 0) {
      alert("no entries found in this file — expected an export-shaped JSON ({ entries: [...] })");
      return;
    }
    try {
      const res = await api.kb.import(col.source_id, entries);
      let msg = `importing ${res.accepted} entries in the background — they'll appear here as they're embedded.`;
      if (res.warnings.length > 0) msg += `\n\nskipped:\n` + res.warnings.join("\n");
      alert(msg);
      void load();
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  const retiredCount = entries.filter((e) => e.status === "superseded").length;
  const visible = entries.filter((e) => showRetired || e.status !== "superseded");
  const heldCount = entries.filter((e) => e.status === "provisional").length;

  return (
    <div className="col" style={{ padding: 16, gap: 12, overflow: "auto" }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="col" style={{ gap: 2 }}>
          <h3 style={{ margin: 0 }}>
            {col.name}
            {col.org_kb && (
              <span
                title="The default knowledge base — every connected source feeds it and your chat flows read all of it"
                style={{
                  fontSize: 11, marginLeft: 8, padding: "1px 6px", borderRadius: 8,
                  background: "#2b6a2b", color: "#fff", verticalAlign: "middle",
                }}
              >
                org default
              </span>
            )}
          </h3>
          {col.description && <span className="muted">{col.description}</span>}
          {heldCount > 0 && (
            <span className="muted" style={{ fontSize: 12 }}>
              {heldCount} entr{heldCount === 1 ? "y is" : "ies are"} <strong>held: disputed</strong> —
              a review-drafted change, retrievable but flagged until it clears review.
            </span>
          )}
        </div>
        <div className="row">
          <button onClick={() => setOpenId("new")}>＋ entry</button>
          <label className="button" style={{ cursor: "pointer" }}>
            ⬆ upload file
            <input
              type="file"
              accept=".pdf,.docx,.md,.markdown,.txt,.csv,.json"
              style={{ display: "none" }}
              onChange={(e) => {
                const f = e.target.files?.[0];
                e.currentTarget.value = "";
                if (f) void uploadFile(f);
              }}
            />
          </label>
          <button onClick={exportCollection} title="download a JSON backup of this collection">
            ⬇ export
          </button>
          <label className="button" style={{ cursor: "pointer" }} title="restore entries from a backup">
            ⬆ import backup
            <input
              type="file"
              accept=".json"
              style={{ display: "none" }}
              onChange={(e) => {
                const f = e.target.files?.[0];
                e.currentTarget.value = "";
                if (f) void importBackup(f);
              }}
            />
          </label>
          <button className="err" onClick={removeCollection}>archive collection</button>
        </div>
      </div>

      <ConnectedSources
        col={col}
        google={gApi}
        onConnectGoogle={connectGoogle}
        onChange={() => {
          void load();
          onChange();
        }}
      />

      {openId === "new" && (
        <EntryEditor
          collectionId={col.source_id}
          onDone={() => {
            setOpenId(null);
            void load();
            onChange();
          }}
          onCancel={() => setOpenId(null)}
        />
      )}

      {retiredCount > 0 && (
        <label className="row muted" style={{ gap: 6, fontSize: 12 }}>
          <input
            type="checkbox"
            checked={showRetired}
            onChange={(e) => setShowRetired(e.target.checked)}
          />
          show {retiredCount} retired (superseded) entr{retiredCount === 1 ? "y" : "ies"}
        </label>
      )}

      <table className="runs-table">
        <thead>
          <tr>
            <th>title</th>
            <th>chunks</th>
            <th>updated</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {visible.map((e) => (
            <Fragment key={e.entry_id}>
              <tr style={e.status === "superseded" ? { opacity: 0.55 } : undefined}>
                <td>
                  {e.origin === "gdoc" && <span title={e.gdoc_url ?? "Google Doc"}>🔗 </span>}
                  {e.title}
                  <KbStatusBadge entry={e} />
                  {e.sync_error && (
                    <span className="err" style={{ fontSize: 11 }}> · sync error</span>
                  )}
                </td>
                <td className="muted">{e.chunk_count}</td>
                <td className="muted">
                  {e.origin === "gdoc" && e.synced_at
                    ? `synced ${new Date(e.synced_at).toLocaleString()}`
                    : new Date(e.updated_at).toLocaleString()}
                </td>
                <td className="row" style={{ gap: 4 }}>
                  {e.connection_id && (
                    <button onClick={() => resync(e.entry_id)} title="re-sync this source">
                      re-sync
                    </button>
                  )}
                  <button
                    onClick={() => setOpenId(openId === e.entry_id ? null : e.entry_id)}
                  >
                    {openId === e.entry_id ? "close" : e.origin === "gdoc" ? "view" : "edit"}
                  </button>
                </td>
              </tr>
              {openId === e.entry_id && (
                <tr>
                  <td colSpan={4}>
                    <EntryEditor
                      collectionId={col.source_id}
                      entryId={e.entry_id}
                      onDone={() => {
                        setOpenId(null);
                        void load();
                        onChange();
                      }}
                      onCancel={() => setOpenId(null)}
                    />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
          {visible.length === 0 && (
            <tr>
              <td colSpan={4} className="muted">
                no entries — add a runbook / workflow / config note, or connect a source above
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// show_if evaluation, shared by the add + edit connector forms
function kbFieldVisible(
  f: KbConnector["config_fields"][number],
  values: Record<string, string>,
): boolean {
  const s = f.show_if;
  if (!s) return true;
  const v = values[s.key] ?? "";
  if (s.eq !== undefined) return v === s.eq;
  if (s.ne !== undefined) return v !== s.ne;
  if (s.in !== undefined) return s.in.includes(v);
  return true;
}

// one config-field <input>/<select> — shared by the add + edit forms
function KbConfigField({
  f,
  value,
  onChange,
}: {
  f: KbConnector["config_fields"][number];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="field">
      <label>
        {f.label}
        {f.required ? " *" : ""}
      </label>
      {f.type === "select" ? (
        <select value={value || f.options?.[0] || ""} onChange={(e) => onChange(e.target.value)}>
          {(f.options ?? []).map((o) => (
            <option key={o} value={o}>{f.option_labels?.[o] ?? o}</option>
          ))}
        </select>
      ) : (
        <input
          type={f.secret ? "password" : f.type === "number" ? "number" : "text"}
          autoComplete={f.secret ? "off" : undefined}
          placeholder={f.secret ? "leave blank to keep the saved key" : f.placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {f.help && <span className="muted" style={{ fontSize: 11 }}>{f.help}</span>}
    </div>
  );
}

// ── Connected sources (docs/KB_SOURCE_CONNECTORS.md) ──────────────────────
function ConnectedSources({
  col,
  google,
  onConnectGoogle,
  onChange,
}: {
  col: KbCollection;
  google: { configured: boolean; connected: boolean };
  onConnectGoogle: () => void;
  onChange: () => void;
}) {
  const [conns, setConns] = useState<KbConnection[]>([]);
  const [catalogue, setCatalogue] = useState<KbConnector[]>([]);
  const [writebacks, setWritebacks] = useState<KbDocWriteback[]>([]);
  const [docDefaults, setDocDefaults] = useState<KbDocDefaults>({});
  const [showWb, setShowWb] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [editDefaults, setEditDefaults] = useState(false);
  const [adding, setAdding] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [cs, cat, wb, dd] = await Promise.all([
        api.kb.listConnections(col.source_id),
        api.kb.listConnectors(col.tenant_id),
        api.kb.listDocWritebacks(col.source_id).catch(() => [] as KbDocWriteback[]),
        api.kb.getDocDefaults(col.tenant_id).catch(() => null),
      ]);
      setConns(cs);
      setCatalogue(cat);
      setWritebacks(wb);
      if (dd) setDocDefaults(dd.effective);
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }, [col.source_id, col.tenant_id]);

  useEffect(() => {
    void load();
  }, [load]);

  const docMode = (c: KbConnection): "suggest" | "write_back" | null => {
    if (c.connector !== "gdocs") return null;
    const cfg = c.config as { on_correction?: string; access?: string };
    const a = cfg.on_correction ?? cfg.access;
    return a === "suggest" || a === "write_back" ? a : null;
  };
  const notIndexed = (c: KbConnection) =>
    c.connector === "gdocs" && (c.config as { index?: boolean }).index === false;

  async function sync(cid: string) {
    try {
      await api.kb.syncConnection(cid);
      alert("Re-syncing in the background.");
      await load();
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  async function toggle(c: KbConnection) {
    try {
      await api.kb.setConnectionStatus(
        c.connection_id,
        c.status === "paused" ? "active" : "paused",
      );
      await load();
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  async function remove(c: KbConnection) {
    if (!confirm(`disconnect "${c.label}" and archive the entries it produced?`)) return;
    try {
      await api.kb.deleteConnection(c.connection_id);
      await load();
      onChange();
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  const needsGoogle = catalogue.some(
    (c) => c.auth === "oauth2" && !c.available && google.configured && !google.connected,
  );

  return (
    <div
      className="col"
      style={{ gap: 8, border: "1px solid var(--border)", borderRadius: 6, padding: 12 }}
    >
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong style={{ fontSize: 13 }}>Connected sources</strong>
        <div className="row" style={{ gap: 6 }}>
          <button
            onClick={() => setEditDefaults((v) => !v)}
            title="Org-wide default for a new Google Doc's read / correction behaviour"
          >
            {editDefaults ? "close" : "Google Docs defaults"}
          </button>
          {needsGoogle && (
            <button onClick={onConnectGoogle} title="OAuth so Google Docs / Sheets can sync">
              Connect Google
            </button>
          )}
          <button className="primary" onClick={() => setAdding((v) => !v)}>
            {adding ? "close" : "＋ add source"}
          </button>
        </div>
      </div>

      {err && <div className="err" style={{ fontSize: 12 }}>{err}</div>}

      {(() => {
        const failing = conns.filter((c) => c.status === "error");
        if (failing.length === 0) return null;
        return (
          <div
            className="err"
            style={{
              fontSize: 12, display: "flex", gap: 8, alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <span>
              ⚠ {failing.length} source{failing.length === 1 ? "" : "s"} failing to sync
              {failing[0].last_result?.error ? ` — ${failing[0].last_result.error}` : ""}
            </span>
            <button onClick={() => failing.forEach((c) => void sync(c.connection_id))}>
              retry all
            </button>
          </div>
        );
      })()}

      {editDefaults && (
        <DocDefaultsForm
          tenantId={col.tenant_id}
          current={docDefaults}
          onDone={(eff) => {
            setDocDefaults(eff);
            setEditDefaults(false);
          }}
        />
      )}

      {adding && (
        <AddSourceForm
          collectionId={col.source_id}
          tenantId={col.tenant_id}
          catalogue={catalogue}
          gdocDefaults={docDefaults}
          onDone={() => {
            setAdding(false);
            void load();
            onChange();
          }}
        />
      )}

      {conns.length === 0 && !adding && (
        <div className="muted" style={{ fontSize: 12 }}>
          nothing connected — “＋ add source” to crawl a docs site or sync a Google Sheet / Doc.
          The chat flow’s retrieval reads every connected source together.
        </div>
      )}

      {conns.length > 0 && (
        <table className="runs-table">
          <thead>
            <tr>
              <th>source</th>
              <th>type</th>
              <th>status</th>
              <th>entries</th>
              <th>last synced</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {conns.map((c) => (
              <Fragment key={c.connection_id}>
              <tr>
                <td style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {c.label}
                  {docMode(c) && (
                    <span
                      title={
                        docMode(c) === "suggest"
                          ? "An approved KB correction opens a GitHub issue with the diff — a human applies it to the doc"
                          : "An approved KB correction is written into the doc, then a GitHub issue opens for verification"
                      }
                      style={{
                        fontSize: 11, marginLeft: 6, padding: "1px 6px", borderRadius: 8,
                        background: docMode(c) === "suggest" ? "#33608a" : "#5a3a8a",
                        color: "#fff", whiteSpace: "nowrap",
                      }}
                    >
                      {docMode(c) === "suggest" ? "suggests edits" : "write-back"}
                    </span>
                  )}
                  {notIndexed(c) && (
                    <span
                      title="Connected but not read into the knowledge base — the bot won't use it to answer"
                      style={{
                        fontSize: 11, marginLeft: 6, padding: "1px 6px", borderRadius: 8,
                        background: "#555", color: "#fff", whiteSpace: "nowrap",
                      }}
                    >
                      not indexed
                    </span>
                  )}
                </td>
                <td className="muted">{c.connector}</td>
                <td>
                  <ConnStatus c={c} />
                </td>
                <td className="muted">{c.entry_count}</td>
                <td className="muted">
                  {c.last_synced_at ? new Date(c.last_synced_at).toLocaleString() : "—"}
                </td>
                <td className="row" style={{ gap: 4 }}>
                  <button onClick={() => sync(c.connection_id)}>re-sync</button>
                  <button
                    onClick={() => setEditId((id) => (id === c.connection_id ? null : c.connection_id))}
                  >
                    {editId === c.connection_id ? "close" : "edit"}
                  </button>
                  <button onClick={() => toggle(c)}>
                    {c.status === "paused" ? "resume" : "pause"}
                  </button>
                  <button className="err" onClick={() => remove(c)}>remove</button>
                </td>
              </tr>
              {editId === c.connection_id && (
                <tr>
                  <td colSpan={6}>
                    <ConnectionEditForm
                      conn={c}
                      spec={catalogue.find((s) => s.slug === c.connector)}
                      onDone={() => {
                        setEditId(null);
                        void load();
                        onChange();
                      }}
                    />
                  </td>
                </tr>
              )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}

      {writebacks.length > 0 && (
        <div className="col" style={{ gap: 6 }}>
          <button
            style={{ alignSelf: "flex-start", fontSize: 12 }}
            onClick={() => setShowWb((v) => !v)}
          >
            {showWb ? "hide" : "show"} {writebacks.length} doc write-back
            {writebacks.length === 1 ? "" : "s"}
          </button>
          {showWb && (
            <table className="runs-table">
              <thead>
                <tr>
                  <th>when</th>
                  <th>status</th>
                  <th>blocks</th>
                  <th>review issue</th>
                </tr>
              </thead>
              <tbody>
                {writebacks.map((w) => (
                  <tr key={w.id}>
                    <td className="muted">{new Date(w.applied_at).toLocaleString()}</td>
                    <td>
                      <span
                        title={w.error ?? undefined}
                        style={{
                          fontSize: 11, padding: "1px 6px", borderRadius: 8, color: "#fff",
                          background:
                            w.status === "applied" || w.status === "verified"
                              ? "#2b6a2b"
                              : w.status === "suggested"
                                ? "#33608a"
                                : w.status === "partial"
                                  ? "#8a5a00"
                                  : w.status === "reverted"
                                    ? "#555"
                                    : "#9b2c2c",
                        }}
                      >
                        {w.status}
                      </span>
                    </td>
                    <td className="muted">
                      {w.blocks.filter((b) => b.applied).length}/{w.blocks.length}
                    </td>
                    <td>
                      {w.github_issue_url ? (
                        <a href={w.github_issue_url} target="_blank" rel="noreferrer">
                          {w.github_repo}#{w.github_issue_number}
                        </a>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

function ConnStatus({ c }: { c: KbConnection }) {
  const map: Record<string, string> = {
    active: "#2b6a2b",
    paused: "#555",
    error: "#9b2c2c",
    archived: "#555",
  };
  const title =
    c.status === "error" ? c.last_result?.error ?? "last sync failed" : undefined;
  return (
    <span
      title={title}
      style={{
        fontSize: 11,
        padding: "1px 6px",
        borderRadius: 8,
        background: map[c.status] ?? "#555",
        color: "#fff",
      }}
    >
      {c.status}
    </span>
  );
}

function AddSourceForm({
  collectionId,
  tenantId,
  catalogue,
  gdocDefaults,
  onDone,
}: {
  collectionId: string;
  tenantId: string;
  catalogue: KbConnector[];
  gdocDefaults: KbDocDefaults;
  onDone: () => void;
}) {
  const [slug, setSlug] = useState(catalogue[0]?.slug ?? "");
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [testMsg, setTestMsg] = useState<{ ok: boolean; detail: string } | null>(null);

  useEffect(() => {
    if (!slug && catalogue.length) setSlug(catalogue[0].slug);
  }, [catalogue, slug]);

  // seed the gdocs knobs from the org default when that connector is picked
  useEffect(() => {
    if (slug !== "gdocs") return;
    setValues((v) => ({
      index: gdocDefaults.index === false ? "no" : "yes",
      on_correction: gdocDefaults.on_correction ?? "off",
      ...(gdocDefaults.github_repo ? { github_repo: gdocDefaults.github_repo } : {}),
      ...v,
    }));
  }, [slug, gdocDefaults]);

  const spec = catalogue.find((c) => c.slug === slug);

  async function submit() {
    if (!spec) return;
    setBusy(true);
    setErr(null);
    const config: Record<string, unknown> = {};
    for (const f of spec.config_fields) {
      if (!kbFieldVisible(f, values)) continue;
      const raw = (values[f.key] ?? "").trim();
      if (!raw) {
        if (f.required) {
          setErr(`${f.label} is required`);
          setBusy(false);
          return;
        }
        continue;
      }
      config[f.key] = f.type === "number" ? Number(raw) : raw;
    }
    try {
      await api.kb.addConnection(collectionId, { connector: slug, config });
      alert("Syncing in the background — entries will appear as they're embedded.");
      onDone();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    setTestMsg(null);
    setErr(null);
    try {
      const r = await api.kb.testConnector(slug, {
        tenant_id: tenantId,
        api_key: (values.api_key ?? "").trim() || undefined,
        board_id: (values.board_id ?? "").trim() || undefined,
      });
      setTestMsg(r);
    } catch (e) {
      setTestMsg({ ok: false, detail: e instanceof ApiError ? String(e.detail) : String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="col" style={{ gap: 8, background: "var(--panel, #00000008)", padding: 10, borderRadius: 6 }}>
      <div className="field">
        <label>source type</label>
        <select value={slug} onChange={(e) => { setSlug(e.target.value); setValues({}); }}>
          {catalogue.map((c) => {
            // an apikey connector stays pickable even with no key yet — you
            // enter it in the form; oauth2 ones need the OAuth done first.
            const pickable = c.available || c.auth === "apikey";
            return (
              <option key={c.slug} value={c.slug} disabled={!pickable}>
                {c.label}
                {!c.available && c.reason ? ` — ${c.reason}` : ""}
              </option>
            );
          })}
        </select>
      </div>

      {spec && !spec.available && (
        <div className="err" style={{ fontSize: 12 }}>{spec.reason ?? "not available"}</div>
      )}

      {spec?.config_fields
        .filter((f) => kbFieldVisible(f, values))
        .map((f) => (
          <KbConfigField
            key={f.key}
            f={f}
            value={values[f.key] ?? ""}
            onChange={(v) => setValues((prev) => ({ ...prev, [f.key]: v }))}
          />
        ))}

      {err && <div className="err" style={{ fontSize: 12 }}>{err}</div>}
      {testMsg && (
        <div
          style={{ fontSize: 12, color: testMsg.ok ? "#2b6a2b" : "var(--crit, #b4432a)" }}
        >
          {testMsg.ok ? "✓ " : "✗ "}
          {testMsg.detail}
        </div>
      )}

      <div className="row">
        <button
          className="primary"
          onClick={submit}
          disabled={busy || !spec || (!spec.available && spec.auth !== "apikey")}
        >
          {busy ? "connecting…" : "connect & sync"}
        </button>
        {spec?.auth === "apikey" && (
          <button onClick={test} disabled={busy}>
            {busy ? "…" : "test connection"}
          </button>
        )}
      </div>
    </div>
  );
}

function DocDefaultsForm({
  tenantId,
  current,
  onDone,
}: {
  tenantId: string;
  current: KbDocDefaults;
  onDone: (effective: KbDocDefaults) => void;
}) {
  const [index, setIndex] = useState(current.index === false ? "no" : "yes");
  const [onCorr, setOnCorr] = useState<string>(current.on_correction ?? "off");
  const [repo, setRepo] = useState(current.github_repo ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const res = await api.kb.setDocDefaults({
        tenant_id: tenantId,
        index: index === "yes",
        on_correction: onCorr,
        github_repo: repo.trim() || undefined,
      });
      onDone(res.effective);
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="col" style={{ gap: 8, background: "var(--panel, #00000008)", padding: 10, borderRadius: 6 }}>
      <span className="muted" style={{ fontSize: 12 }}>
        Default for every <strong>new</strong> Google Doc connection in this workspace. A
        per-doc choice in the form below still overrides it.
      </span>
      <div className="field">
        <label>Read new docs into the knowledge base</label>
        <select value={index} onChange={(e) => setIndex(e.target.value)}>
          <option value="yes">Yes — the bot can use them to answer</option>
          <option value="no">No — connected but not used for answers</option>
        </select>
      </div>
      <div className="field">
        <label>When a support resolution corrects a doc's content</label>
        <select value={onCorr} onChange={(e) => setOnCorr(e.target.value)}>
          <option value="off">Do nothing to the doc</option>
          <option value="suggest">Open a GitHub issue — a person applies it (recommended)</option>
          <option value="write_back">Let the bot edit the doc — a person verifies</option>
        </select>
      </div>
      {onCorr !== "off" && (
        <div className="field">
          <label>GitHub repo for review issues (owner/name)</label>
          <input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="acme/support-kb" />
        </div>
      )}
      {err && <div className="err" style={{ fontSize: 12 }}>{err}</div>}
      <div className="row">
        <button className="primary" onClick={save} disabled={busy}>
          {busy ? "saving…" : "save defaults"}
        </button>
      </div>
    </div>
  );
}

function ConnectionEditForm({
  conn,
  spec,
  onDone,
}: {
  conn: KbConnection;
  spec: KbConnector | undefined;
  onDone: () => void;
}) {
  const seed = () => {
    const v: Record<string, string> = {};
    for (const f of spec?.config_fields ?? []) {
      if (f.secret) continue; // never round-trip a key through the browser
      const raw = (conn.config as Record<string, unknown>)[f.key];
      if (raw !== undefined && raw !== null) v[f.key] = String(raw);
    }
    return v;
  };
  const [values, setValues] = useState<Record<string, string>>(seed);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  if (!spec) {
    return (
      <div className="muted" style={{ fontSize: 12, padding: 8 }}>
        “{conn.connector}” isn’t a known connector on this server — can’t edit its config here.
      </div>
    );
  }

  async function save() {
    setBusy(true);
    setErr(null);
    const config: Record<string, unknown> = {};
    for (const f of spec!.config_fields) {
      if (!kbFieldVisible(f, values)) continue;
      const raw = (values[f.key] ?? "").trim();
      if (!raw) continue; // blank secret => keep saved; blank non-secret => leave stored value
      config[f.key] = f.type === "number" ? Number(raw) : raw;
    }
    try {
      await api.kb.updateConnection(conn.connection_id, { config });
      onDone();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="col" style={{ gap: 8, background: "var(--panel, #00000008)", padding: 10, borderRadius: 6 }}>
      <span className="muted" style={{ fontSize: 12 }}>
        Editing the {spec.label} connection — saving re-syncs it. Leave a field blank to keep its
        current value.
      </span>
      {spec.config_fields
        .filter((f) => kbFieldVisible(f, values))
        .map((f) => (
          <KbConfigField
            key={f.key}
            f={f}
            value={values[f.key] ?? ""}
            onChange={(v) => setValues((prev) => ({ ...prev, [f.key]: v }))}
          />
        ))}
      {err && <div className="err" style={{ fontSize: 12 }}>{err}</div>}
      <div className="row">
        <button className="primary" onClick={save} disabled={busy}>
          {busy ? "saving…" : "save & re-sync"}
        </button>
      </div>
    </div>
  );
}

function KbStatusBadge({ entry }: { entry: KbEntryRow }) {
  const pill = (bg: string, text: string, title?: string) => (
    <span
      title={title}
      style={{
        fontSize: 11,
        marginLeft: 6,
        padding: "1px 6px",
        borderRadius: 8,
        background: bg,
        color: "#fff",
        whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );

  if (entry.status === "provisional") {
    const until = entry.provisional_until
      ? `auto-promotes ${new Date(entry.provisional_until).toLocaleDateString()} if no new contradiction`
      : "held pending review";
    return pill("#8a5a00", "held: disputed", until);
  }
  if (entry.status === "superseded")
    return pill("#555", "superseded", "replaced by a newer entry — not retrieved");
  if (entry.origin === "review_writeback")
    return pill("#33608a", "from a review", "created by the knowledge-integrity loop");
  if (entry.quality === "official")
    return pill("#2b6a2b", "official", "official source — weighted up in retrieval (×1.15)");
  if (entry.quality === "community_resolved")
    return pill("#5a3a8a", "resolved", "a shipped/answered community item — retrieval weight ×1.0");
  return null;
}

function EntryEditor({
  collectionId,
  entryId,
  onDone,
  onCancel,
}: {
  collectionId: string;
  entryId?: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [entry, setEntry] = useState<KbEntry | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!entryId) return;
    api.kb.getEntry(entryId).then((e) => {
      setEntry(e);
      setTitle(e.title);
      setBody(e.body_md);
    });
  }, [entryId]);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      if (entryId) await api.kb.updateEntry(entryId, { title, body_md: body });
      else await api.kb.createEntry(collectionId, { title, body_md: body });
      onDone();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function archive() {
    if (!entryId || !confirm("archive this entry?")) return;
    await api.kb.deleteEntry(entryId);
    onDone();
  }

  const readOnly = entry?.origin === "gdoc" || !!entry?.connection_id;

  return (
    <div className="col" style={{ gap: 8, border: "1px solid var(--border)", padding: 10, borderRadius: 6 }}>
      {readOnly && (
        <div className="muted" style={{ fontSize: 12 }}>
          🔗 synced from a connected source
          {entry?.gdoc_url && (
            <>
              {" "}(<a href={entry.gdoc_url} target="_blank" rel="noreferrer">Google Doc</a>)
            </>
          )}{" "}
          — edit at the source, then “re-sync”. {entry?.sync_error && (
            <span className="err">last sync failed: {entry.sync_error}</span>
          )}
        </div>
      )}
      <div className="field">
        <label>title</label>
        <input
          value={title}
          disabled={readOnly}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Refund approval limits"
        />
      </div>
      <div className="field">
        <label>body (markdown — the bot reads this as authoritative)</label>
        <textarea
          rows={14}
          value={body}
          readOnly={readOnly}
          onChange={(e) => setBody(e.target.value)}
          placeholder={"# Refund approval limits\n\n- < $200: auto-approve\n- $200–$2000: team lead\n- > $2000: manager sign-off"}
          style={{ fontFamily: "ui-monospace, monospace", fontSize: 13, opacity: readOnly ? 0.75 : 1 }}
        />
      </div>
      {entry && (
        <div className="muted" style={{ fontSize: 11 }}>
          {entry.chunk_count} chunk(s){entry.embedded_at ? ` · embedded ${new Date(entry.embedded_at).toLocaleString()}` : " · not embedded yet"}
        </div>
      )}
      {err && <div className="err" style={{ fontSize: 12 }}>{err}</div>}
      <div className="row">
        {!readOnly && (
          <button className="primary" onClick={save} disabled={busy || !title.trim()}>
            {busy ? "saving…" : "save"}
          </button>
        )}
        <button onClick={onCancel}>{readOnly ? "close" : "cancel"}</button>
        <div style={{ flex: 1 }} />
        {entryId && <button className="err" onClick={archive}>archive</button>}
      </div>
    </div>
  );
}
