import { useCallback, useEffect, useState } from "react";
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
import {
  Button,
  Tag,
  Banner,
  Dialog,
  SlideOver,
  Field,
  Input,
  Textarea,
  Select,
  DataTable,
  EmptyState,
  useToast,
  type Column,
  type TagTone,
} from "../ui";

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

  const [newColOpen, setNewColOpen] = useState(false);
  const [newColName, setNewColName] = useState("");

  async function createCollection() {
    const name = newColName.trim();
    if (!name) return;
    try {
      const { source_id } = await api.kb.createCollection({ name, tenant_id: tenantId });
      setNewColOpen(false);
      setNewColName("");
      await refresh();
      setSel(source_id);
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  const orgKb = cols.filter((c) => c.org_kb);
  const teamCols = cols.filter((c) => !c.org_kb);

  const railItem = (c: KbCollection) => (
    <button
      key={c.source_id}
      className={`flow-item${c.source_id === sel ? " active" : ""}`}
      style={{ textAlign: "left", width: "100%", border: 0, background: "transparent" }}
      onClick={() => setSel(c.source_id)}
    >
      <div className="row" style={{ justifyContent: "space-between", gap: 6 }}>
        <span style={{ fontWeight: c.source_id === sel ? 600 : 400 }}>{c.name}</span>
        <span className="row" style={{ gap: 4 }}>
          {!!c.provisional_count && (
            <Tag tone="warn">{c.provisional_count} held</Tag>
          )}
          <span className="muted" style={{ font: "var(--type-mono)" }}>{c.entry_count}</span>
        </span>
      </div>
    </button>
  );

  return (
    <div className="kb-shell">
      <aside className="kb-rail">
        <div className="kb-rail__head">
          <strong>Knowledge</strong>
        </div>
        <div className="kb-rail__scroll">
          {err && <Banner tone="exception" title={err} actions={<Button variant="ghost" size="sm" onClick={() => setErr(null)}>Dismiss</Button>} />}
          <div style={{ display: "grid", gap: 2 }}>{orgKb.map(railItem)}</div>

          <div className="row" style={{ justifyContent: "space-between", margin: "12px 0 4px" }}>
            <span className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".12em" }}>
              Additional collections
            </span>
            <Button
              variant="icon"
              size="sm"
              aria-label="New collection"
              title="Optional — a separate collection a flow can scope to."
              onClick={() => {
                setNewColName("");
                setNewColOpen(true);
              }}
            >
              ＋
            </Button>
          </div>
          <div style={{ display: "grid", gap: 2 }}>
            {teamCols.map(railItem)}
            {teamCols.length === 0 && (
              <div className="muted" style={{ fontSize: 11 }}>none — everything feeds the org KB</div>
            )}
          </div>
        </div>
      </aside>

      <div className="kb-main">
        {sel && cols.find((c) => c.source_id === sel) ? (
          <Collection key={sel} col={cols.find((c) => c.source_id === sel)!} onChange={refresh} />
        ) : (
          <EmptyState title="Select a collection" body="Pick one from the list on the left." />
        )}
      </div>

      <Dialog
        open={newColOpen}
        onClose={() => setNewColOpen(false)}
        title="New collection"
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setNewColOpen(false) },
          { label: "Create", variant: "primary", onClick: createCollection },
        ]}
      >
        <Field
          label="Name"
          hint="Optional — most orgs just use the org knowledge base. A separate collection is one a flow can scope its retrieval to."
        >
          <Input value={newColName} autoFocus onChange={(e) => setNewColName(e.target.value)} />
        </Field>
      </Dialog>
    </div>
  );
}

function Collection({ col, onChange }: { col: KbCollection; onChange: () => void }) {
  const [entries, setEntries] = useState<KbEntryRow[]>([]);
  const [openId, setOpenId] = useState<string | "new" | null>(null);
  const [showRetired, setShowRetired] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [askArchive, setAskArchive] = useState(false);
  const toast = useToast();

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
    setAskArchive(false);
    try {
      await api.kb.deleteCollection(col.source_id);
      onChange();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
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
      toast("Re-syncing in the background — updated entries appear shortly");
      void load();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
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
      toast(`Uploaded "${file.name}" — embedding in the background`);
      void load();
      onChange();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
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
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  async function importBackup(file: File) {
    let bundle: { entries?: { title: string; body_md: string }[] };
    try {
      bundle = JSON.parse(await file.text());
    } catch {
      setErr("That file isn't valid JSON.");
      return;
    }
    const entries = bundle.entries;
    if (!Array.isArray(entries) || entries.length === 0) {
      setErr("No entries found — expected an export-shaped JSON ({ entries: [...] }).");
      return;
    }
    try {
      const res = await api.kb.import(col.source_id, entries);
      toast(
        `Importing ${res.accepted} entries in the background` +
          (res.warnings.length ? ` · ${res.warnings.length} skipped` : ""),
      );
      void load();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  const retiredCount = entries.filter((e) => e.status === "superseded").length;
  const visible = entries.filter((e) => showRetired || e.status !== "superseded");
  const heldCount = entries.filter((e) => e.status === "provisional").length;

  const columns: Column<KbEntryRow>[] = [
    {
      key: "title",
      header: "document",
      cell: (e) => (
        <span style={{ opacity: e.status === "superseded" ? 0.55 : 1 }}>
          {e.origin === "gdoc" && <span title={e.gdoc_url ?? "Google Doc"}>🔗 </span>}
          {e.title}
          <KbStatusBadge entry={e} />
          {e.sync_error && <span className="err" style={{ fontSize: 11 }}> · sync error</span>}
        </span>
      ),
    },
    {
      key: "chunks",
      header: "chunks",
      align: "right",
      cell: (e) => <span className="muted" style={{ font: "var(--type-mono)" }}>{e.chunk_count}</span>,
    },
    {
      key: "updated",
      header: "updated",
      align: "right",
      cell: (e) => (
        <span className="muted" style={{ font: "var(--type-mono)" }}>
          {e.origin === "gdoc" && e.synced_at
            ? `synced ${new Date(e.synced_at).toLocaleString()}`
            : new Date(e.updated_at).toLocaleString()}
        </span>
      ),
    },
    {
      key: "act",
      header: "",
      align: "right",
      cell: (e) =>
        e.connection_id ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={(ev) => {
              ev.stopPropagation();
              void resync(e.entry_id);
            }}
            title="re-sync this source"
          >
            re-sync
          </Button>
        ) : null,
    },
  ];

  const openEntry = openId === "new" ? null : entries.find((e) => e.entry_id === openId) ?? null;

  return (
    <div className="kb-collection">
      <div className="kb-collection__head">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
          <div style={{ display: "grid", gap: 4 }}>
            <div className="row" style={{ gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
              <span style={{ font: "var(--type-view-title)" }}>{col.name}</span>
              {col.org_kb && (
                <span title="The default knowledge base — every connected source feeds it and your chat flows read all of it">
                  <Tag tone="accent">org default</Tag>
                </span>
              )}
              {heldCount > 0 && <Tag tone="warn">{heldCount} held</Tag>}
            </div>
            {col.description && <span className="muted">{col.description}</span>}
            {heldCount > 0 && (
              <span className="muted" style={{ fontSize: 12 }}>
                {heldCount} entr{heldCount === 1 ? "y is" : "ies are"} <strong>held: disputed</strong> —
                retrievable but flagged until review clears.
              </span>
            )}
          </div>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            <Button variant="secondary" size="sm" onClick={() => setOpenId("new")}>
              ＋ Entry
            </Button>
            <label className="ui-btn ui-btn--secondary ui-btn--sm" style={{ cursor: "pointer" }}>
              ⬆ Upload
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
            <Button variant="ghost" size="sm" onClick={exportCollection} title="download a JSON backup">
              ⬇ Export
            </Button>
            <label className="ui-btn ui-btn--ghost ui-btn--sm" style={{ cursor: "pointer" }} title="restore from a backup">
              ⬆ Import
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
            <Button variant="danger" size="sm" onClick={() => setAskArchive(true)}>
              Archive
            </Button>
          </div>
        </div>

        {err && (
          <Banner
            tone="exception"
            title={err}
            actions={<Button variant="ghost" size="sm" onClick={() => setErr(null)}>Dismiss</Button>}
          />
        )}

        <ConnectedSources
          col={col}
          google={gApi}
          onConnectGoogle={connectGoogle}
          onChange={() => {
            void load();
            onChange();
          }}
        />

        {retiredCount > 0 && (
          <label className="row muted" style={{ gap: 6, fontSize: 12 }}>
            <input
              type="checkbox"
              style={{ width: "auto" }}
              checked={showRetired}
              onChange={(e) => setShowRetired(e.target.checked)}
            />
            show {retiredCount} retired (superseded) entr{retiredCount === 1 ? "y" : "ies"}
          </label>
        )}
      </div>

      <div className="kb-collection__docs">
        <DataTable
          columns={columns}
          rows={visible}
          rowId={(e) => e.entry_id}
          selectedId={openEntry?.entry_id ?? null}
          onSelect={(e) => setOpenId(e.entry_id)}
          empty={
            <EmptyState
              title="No entries yet"
              body="Add a runbook or config note, or connect a source above."
              action={
                <Button variant="secondary" size="sm" onClick={() => setOpenId("new")}>
                  ＋ Entry
                </Button>
              }
            />
          }
        />
      </div>

      <SlideOver
        open={openId != null}
        onClose={() => setOpenId(null)}
        width={720}
        title={openId === "new" ? "New entry" : openEntry?.title ?? "Entry"}
      >
        {openId === "new" ? (
          <EntryEditor
            collectionId={col.source_id}
            onDone={() => {
              setOpenId(null);
              void load();
              onChange();
            }}
            onCancel={() => setOpenId(null)}
          />
        ) : openEntry ? (
          <EntryEditor
            collectionId={col.source_id}
            entryId={openEntry.entry_id}
            onDone={() => {
              setOpenId(null);
              void load();
              onChange();
            }}
            onCancel={() => setOpenId(null)}
          />
        ) : null}
      </SlideOver>

      <Dialog
        open={askArchive}
        onClose={() => setAskArchive(false)}
        title={`Archive "${col.name}"?`}
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setAskArchive(false) },
          { label: "Archive", variant: "danger", onClick: removeCollection },
        ]}
      >
        The collection and all of its entries are archived. Flows that read it stop
        getting results from it.
      </Dialog>
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
    <Field label={`${f.label}${f.required ? " *" : ""}`} hint={f.help}>
      {f.type === "select" ? (
        <Select
          value={value || f.options?.[0] || ""}
          onChange={(e) => onChange(e.target.value)}
          options={(f.options ?? []).map((o) => ({ value: o, label: f.option_labels?.[o] ?? o }))}
        />
      ) : (
        <Input
          type={f.secret ? "password" : f.type === "number" ? "number" : "text"}
          autoComplete={f.secret ? "off" : undefined}
          placeholder={f.secret ? "leave blank to keep the saved key" : f.placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </Field>
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
  const [panel, setPanel] = useState<null | "add" | "defaults" | { edit: KbConnection }>(null);
  const [removeTarget, setRemoveTarget] = useState<KbConnection | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const toast = useToast();

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
      toast("Re-syncing in the background");
      await load();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  async function toggle(c: KbConnection) {
    try {
      await api.kb.setConnectionStatus(c.connection_id, c.status === "paused" ? "active" : "paused");
      await load();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  async function runRemove(c: KbConnection) {
    setRemoveTarget(null);
    try {
      await api.kb.deleteConnection(c.connection_id);
      await load();
      onChange();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  const needsGoogle = catalogue.some(
    (c) => c.auth === "oauth2" && !c.available && google.configured && !google.connected,
  );

  const failing = conns.filter((c) => c.status === "error");

  const connColumns: Column<KbConnection>[] = [
    {
      key: "source",
      header: "source",
      cell: (c) => (
        <span>
          {c.label}
          {docMode(c) && (
            <span
              style={{ marginLeft: 6 }}
              title={
                docMode(c) === "suggest"
                  ? "An approved KB correction opens a GitHub issue with the diff — a human applies it"
                  : "An approved KB correction is written into the doc, then a GitHub issue opens for verification"
              }
            >
              <Tag tone="accent">{docMode(c) === "suggest" ? "suggests edits" : "write-back"}</Tag>
            </span>
          )}
          {notIndexed(c) && (
            <span style={{ marginLeft: 6 }} title="Connected but not read into the knowledge base">
              <Tag tone="neutral">not indexed</Tag>
            </span>
          )}
        </span>
      ),
    },
    { key: "type", header: "type", cell: (c) => <span className="muted">{c.connector}</span> },
    { key: "status", header: "status", cell: (c) => <ConnStatus c={c} /> },
    {
      key: "entries",
      header: "entries",
      align: "right",
      cell: (c) => <span className="muted" style={{ font: "var(--type-mono)" }}>{c.entry_count}</span>,
    },
    {
      key: "synced",
      header: "last synced",
      align: "right",
      cell: (c) => (
        <span className="muted" style={{ font: "var(--type-mono)" }}>
          {c.last_synced_at ? new Date(c.last_synced_at).toLocaleString() : "—"}
        </span>
      ),
    },
    {
      key: "act",
      header: "",
      align: "right",
      cell: (c) => (
        <span className="row" style={{ gap: 4, justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); void sync(c.connection_id); }}>
            re-sync
          </Button>
          <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); void toggle(c); }}>
            {c.status === "paused" ? "resume" : "pause"}
          </Button>
          <Button variant="danger" size="sm" onClick={(e) => { e.stopPropagation(); setRemoveTarget(c); }}>
            remove
          </Button>
        </span>
      ),
    },
  ];

  return (
    <div className="kb-sources">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <strong style={{ fontSize: 13 }}>Connected sources</strong>
        <div className="row" style={{ gap: 6 }}>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setPanel("defaults")}
            title="Org-wide default for a new Google Doc's read / correction behaviour"
          >
            Google Docs defaults
          </Button>
          {needsGoogle && (
            <Button variant="secondary" size="sm" onClick={onConnectGoogle} title="OAuth so Google Docs / Sheets can sync">
              Connect Google
            </Button>
          )}
          <Button variant="primary" size="sm" onClick={() => setPanel("add")}>
            ＋ Add source
          </Button>
        </div>
      </div>

      {err && (
        <Banner
          tone="exception"
          title={err}
          actions={<Button variant="ghost" size="sm" onClick={() => setErr(null)}>Dismiss</Button>}
        />
      )}

      {failing.length > 0 && (
        <Banner
          tone="exception"
          title={`${failing.length} source${failing.length === 1 ? "" : "s"} failing to sync`}
          detail={failing[0].last_result?.error ?? undefined}
          actions={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => failing.forEach((c) => void sync(c.connection_id))}
            >
              Retry all
            </Button>
          }
        />
      )}

      {conns.length === 0 ? (
        <div className="muted" style={{ fontSize: 12 }}>
          Nothing connected — “Add source” to crawl a docs site or sync a Google Sheet / Doc. The
          chat flow’s retrieval reads every connected source together.
        </div>
      ) : (
        <DataTable
          columns={connColumns}
          rows={conns}
          rowId={(c) => c.connection_id}
          selectedId={typeof panel === "object" && panel ? panel.edit.connection_id : null}
          onSelect={(c) => setPanel({ edit: c })}
        />
      )}

      {writebacks.length > 0 && (
        <div style={{ display: "grid", gap: 6 }}>
          <Button variant="ghost" size="sm" style={{ alignSelf: "flex-start" }} onClick={() => setShowWb((v) => !v)}>
            {showWb ? "Hide" : "Show"} {writebacks.length} doc write-back{writebacks.length === 1 ? "" : "s"}
          </Button>
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
                {writebacks.map((w) => {
                  const tone: TagTone =
                    w.status === "applied" || w.status === "verified"
                      ? "accent"
                      : w.status === "suggested"
                        ? "accent"
                        : w.status === "partial"
                          ? "warn"
                          : w.status === "reverted"
                            ? "neutral"
                            : "exception";
                  return (
                    <tr key={w.id}>
                      <td className="muted">{new Date(w.applied_at).toLocaleString()}</td>
                      <td>
                        <span title={w.error ?? undefined}>
                          <Tag tone={tone}>{w.status}</Tag>
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
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      <SlideOver open={panel === "add"} onClose={() => setPanel(null)} width={520} title="Add a source">
        <AddSourceForm
          collectionId={col.source_id}
          tenantId={col.tenant_id}
          catalogue={catalogue}
          gdocDefaults={docDefaults}
          onDone={() => {
            setPanel(null);
            void load();
            onChange();
          }}
        />
      </SlideOver>

      <SlideOver
        open={panel === "defaults"}
        onClose={() => setPanel(null)}
        width={520}
        title="Google Docs defaults"
      >
        <DocDefaultsForm
          tenantId={col.tenant_id}
          current={docDefaults}
          onDone={(eff) => {
            setDocDefaults(eff);
            setPanel(null);
          }}
        />
      </SlideOver>

      <SlideOver
        open={typeof panel === "object" && panel != null}
        onClose={() => setPanel(null)}
        width={520}
        title={typeof panel === "object" && panel ? `Edit ${panel.edit.label}` : "Edit source"}
      >
        {typeof panel === "object" && panel && (
          <ConnectionEditForm
            conn={panel.edit}
            spec={catalogue.find((s) => s.slug === panel.edit.connector)}
            onDone={() => {
              setPanel(null);
              void load();
              onChange();
            }}
          />
        )}
      </SlideOver>

      <Dialog
        open={removeTarget != null}
        onClose={() => setRemoveTarget(null)}
        title={removeTarget ? `Disconnect "${removeTarget.label}"?` : ""}
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setRemoveTarget(null) },
          {
            label: "Disconnect",
            variant: "danger",
            onClick: () => removeTarget && runRemove(removeTarget),
          },
        ]}
      >
        The connection is removed and the entries it produced are archived.
      </Dialog>
    </div>
  );
}

function ConnStatus({ c }: { c: KbConnection }) {
  const tone: TagTone = c.status === "error" ? "exception" : c.status === "active" ? "accent" : "neutral";
  const title = c.status === "error" ? c.last_result?.error ?? "last sync failed" : undefined;
  return (
    <span title={title}>
      <Tag tone={tone} dot>
        {c.status}
      </Tag>
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
  const toast = useToast();

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
      toast("Syncing in the background — entries appear as they're embedded");
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
        base_url: (values.base_url ?? "").trim() || undefined,
        api_username: (values.api_username ?? "").trim() || undefined,
      });
      setTestMsg(r);
    } catch (e) {
      setTestMsg({ ok: false, detail: e instanceof ApiError ? String(e.detail) : String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <Field label="Source type">
        <Select
          value={slug}
          onChange={(e) => {
            setSlug(e.target.value);
            setValues({});
          }}
          options={catalogue.map((c) => ({
            value: c.slug,
            label: c.label + (!c.available && c.reason ? ` — ${c.reason}` : ""),
          }))}
        />
      </Field>

      {spec && !spec.available && <Banner tone="warn" title={spec.reason ?? "not available"} />}

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

      {err && <Banner tone="exception" title={err} />}
      {testMsg && (
        <div style={{ fontSize: 12, color: testMsg.ok ? "var(--accent)" : "var(--exception-text)" }}>
          {testMsg.ok ? "✓ " : "✗ "}
          {testMsg.detail}
        </div>
      )}

      <div className="row" style={{ gap: 8 }}>
        <Button
          variant="primary"
          onClick={submit}
          loading={busy}
          disabled={!spec || (!spec.available && spec.auth !== "apikey")}
        >
          Connect &amp; sync
        </Button>
        {spec?.auth === "apikey" && (
          <Button variant="secondary" onClick={test} disabled={busy}>
            Test connection
          </Button>
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
    <div style={{ display: "grid", gap: 12 }}>
      <span className="muted" style={{ fontSize: 12 }}>
        Default for every <strong>new</strong> Google Doc connection in this workspace. A per-doc
        choice in the Add-source form still overrides it.
      </span>
      <Field label="Read new docs into the knowledge base">
        <Select
          value={index}
          onChange={(e) => setIndex(e.target.value)}
          options={[
            { value: "yes", label: "Yes — the bot can use them to answer" },
            { value: "no", label: "No — connected but not used for answers" },
          ]}
        />
      </Field>
      <Field label="When a support resolution corrects a doc's content">
        <Select
          value={onCorr}
          onChange={(e) => setOnCorr(e.target.value)}
          options={[
            { value: "off", label: "Do nothing to the doc" },
            { value: "suggest", label: "Open a GitHub issue — a person applies it (recommended)" },
            { value: "write_back", label: "Let the bot edit the doc — a person verifies" },
          ]}
        />
      </Field>
      {onCorr !== "off" && (
        <Field label="GitHub repo for review issues (owner/name)">
          <Input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="acme/support-kb" />
        </Field>
      )}
      {err && <Banner tone="exception" title={err} />}
      <div className="row">
        <Button variant="primary" onClick={save} loading={busy}>
          Save defaults
        </Button>
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
      <div className="muted" style={{ fontSize: 12 }}>
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
    <div style={{ display: "grid", gap: 12 }}>
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
      {err && <Banner tone="exception" title={err} />}
      <div className="row">
        <Button variant="primary" onClick={save} loading={busy}>
          Save &amp; re-sync
        </Button>
      </div>
    </div>
  );
}

function KbStatusBadge({ entry }: { entry: KbEntryRow }) {
  const badge = (tone: TagTone, text: string, title?: string) => (
    <span style={{ marginLeft: 6, whiteSpace: "nowrap" }} title={title}>
      <Tag tone={tone}>{text}</Tag>
    </span>
  );

  if (entry.status === "provisional") {
    const until = entry.provisional_until
      ? `auto-promotes ${new Date(entry.provisional_until).toLocaleDateString()} if no new contradiction`
      : "held pending review";
    return badge("warn", "held: disputed", until);
  }
  if (entry.status === "superseded")
    return badge("neutral", "superseded", "replaced by a newer entry — not retrieved");
  if (entry.origin === "review_writeback")
    return badge("accent", "from a review", "created by the knowledge-integrity loop");
  if (entry.quality === "official")
    return badge("accent", "official", "official source — weighted up in retrieval (×1.15)");
  if (entry.quality === "community_resolved")
    return badge("neutral", "resolved", "a shipped/answered community item — retrieval weight ×1.0");
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
  const [confirmArchive, setConfirmArchive] = useState(false);

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
    if (!entryId) return;
    setConfirmArchive(false);
    try {
      await api.kb.deleteEntry(entryId);
      onDone();
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  const readOnly = entry?.origin === "gdoc" || !!entry?.connection_id;

  return (
    <div style={{ display: "grid", gap: 12 }}>
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
      <Field label="Title">
        <Input
          value={title}
          disabled={readOnly}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Refund approval limits"
        />
      </Field>
      <Field label="Body" hint="Markdown — the bot reads this as authoritative.">
        <Textarea
          rows={16}
          value={body}
          readOnly={readOnly}
          onChange={(e) => setBody(e.target.value)}
          placeholder={"# Refund approval limits\n\n- < $200: auto-approve\n- $200–$2000: team lead\n- > $2000: manager sign-off"}
          style={{ fontFamily: "var(--font-mono)", fontSize: 13, opacity: readOnly ? 0.75 : 1 }}
        />
      </Field>
      {entry && (
        <div className="muted" style={{ fontSize: 11 }}>
          {entry.chunk_count} chunk(s)
          {entry.embedded_at ? ` · embedded ${new Date(entry.embedded_at).toLocaleString()}` : " · not embedded yet"}
        </div>
      )}
      {err && <Banner tone="exception" title={err} />}
      <div className="row" style={{ gap: 8 }}>
        {!readOnly && (
          <Button variant="primary" onClick={save} loading={busy} disabled={!title.trim()}>
            Save
          </Button>
        )}
        <Button variant="ghost" onClick={onCancel}>
          {readOnly ? "Close" : "Cancel"}
        </Button>
        <span style={{ flex: 1 }} />
        {entryId &&
          (confirmArchive ? (
            <>
              <Button variant="ghost" size="sm" onClick={() => setConfirmArchive(false)}>
                Cancel
              </Button>
              <Button variant="danger" size="sm" onClick={archive}>
                Archive entry
              </Button>
            </>
          ) : (
            <Button variant="danger" size="sm" onClick={() => setConfirmArchive(true)}>
              Archive
            </Button>
          ))}
      </div>
    </div>
  );
}
