import { Fragment, useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { KbCollection, KbEntry, KbEntryRow } from "../types";

/**
 * Self-serve internal knowledge base (Phase 14). Per-team collections of
 * markdown SOPs; a `kb_lookup` node in a flow consults chosen collections
 * at a checkpoint. Editors here = anyone in the tenant.
 */
export function KnowledgeView() {
  const [cols, setCols] = useState<KbCollection[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const c = await api.kb.listCollections();
      setCols(c);
      setSel((s) => s ?? c[0]?.source_id ?? null);
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function newCollection() {
    const name = prompt("collection name (e.g. billing-runbook)")?.trim();
    if (!name) return;
    try {
      const { source_id } = await api.kb.createCollection({ name });
      await refresh();
      setSel(source_id);
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  return (
    <div style={{ display: "flex", height: "100%", minHeight: 0 }}>
      <div
        className="col"
        style={{ width: 220, borderRight: "1px solid var(--border)", padding: 10, gap: 8, overflow: "auto" }}
      >
        <div className="row" style={{ justifyContent: "space-between" }}>
          <strong>Knowledge</strong>
          <button onClick={newCollection}>＋</button>
        </div>
        {err && <div className="err" style={{ fontSize: 12 }}>{err}</div>}
        <div className="col" style={{ gap: 2 }}>
          {cols.map((c) => (
            <button
              key={c.source_id}
              className={c.source_id === sel ? "primary" : ""}
              style={{ justifyContent: "space-between", display: "flex" }}
              onClick={() => setSel(c.source_id)}
            >
              <span>{c.name}</span>
              <span className="muted">{c.entry_count}</span>
            </button>
          ))}
          {cols.length === 0 && !err && (
            <div className="muted" style={{ fontSize: 12 }}>
              no collections yet — create one, then add SOP entries
            </div>
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
            select or create a collection
          </div>
        )}
      </div>
    </div>
  );
}

function Collection({ col, onChange }: { col: KbCollection; onChange: () => void }) {
  const [entries, setEntries] = useState<KbEntryRow[]>([]);
  const [openId, setOpenId] = useState<string | "new" | null>(null);

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

  async function linkGdoc() {
    const u = prompt("Google Doc URL")?.trim();
    if (!u) return;
    try {
      await api.kb.linkGdoc(col.source_id, u);
      void load();
      onChange();
    } catch (e) {
      alert(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  async function resync(entryId: string) {
    try {
      await api.kb.resyncGdoc(entryId);
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

  return (
    <div className="col" style={{ padding: 16, gap: 12, overflow: "auto" }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="col" style={{ gap: 2 }}>
          <h3 style={{ margin: 0 }}>{col.name}</h3>
          {col.description && <span className="muted">{col.description}</span>}
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
          {gApi.configured &&
            (gApi.connected ? (
              <button onClick={linkGdoc}>＋ Google Doc</button>
            ) : (
              <button onClick={connectGoogle}>Connect Google</button>
            ))}
          <button className="err" onClick={removeCollection}>archive collection</button>
        </div>
      </div>

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
          {entries.map((e) => (
            <Fragment key={e.entry_id}>
              <tr>
                <td>
                  {e.origin === "gdoc" && <span title={e.gdoc_url ?? "Google Doc"}>🔗 </span>}
                  {e.title}
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
                  {e.origin === "gdoc" && (
                    <button onClick={() => resync(e.entry_id)} title="re-fetch from Google">
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
          {entries.length === 0 && (
            <tr>
              <td colSpan={4} className="muted">
                no entries — add a runbook / workflow / config note
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
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

  const readOnly = entry?.origin === "gdoc";

  return (
    <div className="col" style={{ gap: 8, border: "1px solid var(--border)", padding: 10, borderRadius: 6 }}>
      {readOnly && (
        <div className="muted" style={{ fontSize: 12 }}>
          🔗 synced from{" "}
          <a href={entry?.gdoc_url ?? "#"} target="_blank" rel="noreferrer">Google Doc</a>{" "}
          — edit the doc, then “re-sync”. {entry?.sync_error && (
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
