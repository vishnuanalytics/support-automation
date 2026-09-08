import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  addEdge,
  Background,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  type Connection,
  type Viewport,
} from "@xyflow/react";
import { api, ApiError } from "../api";
import type { Flow, FlowCandidate, NodeTypesResp } from "../types";
import {
  candidateToCanvas,
  layout,
  TERMINAL,
  toFlowPayload,
  toReactFlow,
  uuid,
  type RFEdge,
  type RFNode,
} from "./graph";
import { summarize } from "./nodeSummary";
import { NodeCard } from "./NodeCard";
import { EdgeLabel } from "./EdgeLabel";
import { ZoomControl } from "./ZoomControl";
import { InspectorPanel } from "./InspectorPanel";
import { TriggersPanel } from "./TriggersPanel";
import { Popover, Toolbar, Button, Banner, Dialog, SlideOver, Field, Input, Textarea, Toggle, useToast } from "../ui";

export function FlowEditor(props: {
  flowId: string;
  canEdit: boolean;
  onSaved: () => void;
  onDeleted: () => void;
}) {
  return (
    <ReactFlowProvider>
      <Inner {...props} />
    </ReactFlowProvider>
  );
}

type EditorBanner = { kind: "ok" | "err"; text: string; list?: string[] };

function Inner({ flowId, canEdit, onSaved, onDeleted }: {
  flowId: string;
  canEdit: boolean;
  onSaved: () => void;
  onDeleted: () => void;
}) {
  const [flow, setFlow] = useState<Flow | null>(null);
  const [nodes, setNodes, onNodesChange] = useNodesState<RFNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<RFEdge>([]);
  const [configById, setConfigById] = useState<Record<string, Record<string, unknown>>>({});
  const [types, setTypes] = useState<NodeTypesResp | null>(null);
  const [selNode, setSelNode] = useState<string | null>(null);
  const [selEdge, setSelEdge] = useState<string | null>(null);
  const [banner, setBanner] = useState<EditorBanner | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [versions, setVersions] = useState<{ version: number; created_at: string }[]>([]);
  const [assist, setAssist] = useState<null | "mermaid" | "ai-edit">(null);
  const [assistText, setAssistText] = useState("");
  const [assistErr, setAssistErr] = useState<string | null>(null);
  const [assistBusy, setAssistBusy] = useState(false);

  const nodeTypes = useMemo(() => ({ flowNode: NodeCard }), []);
  const edgeTypes = useMemo(() => ({ default: EdgeLabel }), []);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [nodeFilter, setNodeFilter] = useState("");
  const paletteBtnRef = useRef<HTMLButtonElement>(null);

  const toast = useToast();
  // overlay chrome — three kinds only (popover / slide-over / dialog); no
  // prompt()/confirm()/alert() anywhere in this component.
  const [moreOpen, setMoreOpen] = useState(false);
  const moreBtnRef = useRef<HTMLButtonElement>(null);
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const rollbackBtnRef = useRef<HTMLButtonElement>(null);
  const [dialog, setDialog] = useState<null | "publish" | "delete" | "template" | { rollback: number }>(null);
  const [tplName, setTplName] = useState("");
  const [tplDesc, setTplDesc] = useState("");

  // Law 3 — the canvas viewport is persisted per flow id. Read once at mount
  // (Inner is keyed by flowId in App, so it remounts per flow).
  const savedViewport = useMemo<Viewport | null>(() => {
    try {
      const s = localStorage.getItem(`flow-viewport:${flowId}`);
      return s ? (JSON.parse(s) as Viewport) : null;
    } catch {
      return null;
    }
  }, [flowId]);

  // ── undo / redo (Ctrl/Cmd+Z) ───────────────────────────────────────
  type Snap = { nodes: RFNode[]; edges: RFEdge[]; cfg: Record<string, Record<string, unknown>> };
  const cur = useRef<Snap>({ nodes: [], edges: [], cfg: {} });
  const past = useRef<Snap[]>([]);
  const future = useRef<Snap[]>([]);
  const applyingHistory = useRef(false);
  useEffect(() => {
    cur.current = { nodes, edges, cfg: configById };
  }, [nodes, edges, configById]);

  const snapshot = useCallback(() => {
    if (applyingHistory.current) return;
    past.current.push({
      nodes: cur.current.nodes,
      edges: cur.current.edges,
      cfg: cur.current.cfg,
    });
    if (past.current.length > 60) past.current.shift();
    future.current = [];
  }, []);

  const mark = useCallback(() => {
    snapshot();
    setDirty(true);
  }, [snapshot]);

  const restore = useCallback(
    (s: Snap) => {
      applyingHistory.current = true;
      setNodes(s.nodes);
      setEdges(s.edges);
      setConfigById(s.cfg);
      setDirty(true);
      setSelNode(null);
      setSelEdge(null);
      requestAnimationFrame(() => (applyingHistory.current = false));
    },
    [setNodes, setEdges],
  );
  const undo = useCallback(() => {
    const prev = past.current.pop();
    if (!prev) return;
    future.current.push(cur.current);
    restore(prev);
  }, [restore]);
  const redo = useCallback(() => {
    const next = future.current.pop();
    if (!next) return;
    past.current.push(cur.current);
    restore(next);
  }, [restore]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!canEdit) return;
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "z") {
        e.preventDefault();
        e.shiftKey ? redo() : undo();
      } else if (mod && e.key.toLowerCase() === "y") {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canEdit, undo, redo]);

  // Phase 19 — drop a proposed graph (Mermaid import / AI assist) onto the
  // canvas as unsaved state. Nothing is persisted until the user hits Save.
  const putCandidate = useCallback(
    (base: Flow, res: FlowCandidate, note: string) => {
      const { nodes: n, edges: e, configById: cfg } = candidateToCanvas(base, res);
      setNodes(n);
      setEdges(e);
      setConfigById(cfg);
      setSelNode(null);
      setSelEdge(null);
      setDirty(true);
      const list = [...res.errors, ...res.warnings];
      setBanner(
        res.errors.length
          ? { kind: "err", text: `${note} — fix the problems below before saving`, list }
          : { kind: "ok", text: note, list: list.length ? list : undefined },
      );
    },
    [setNodes, setEdges],
  );

  useEffect(() => {
    let alive = true;
    api.nodeTypes().then((t) => alive && setTypes(t));
    api.listVersions(flowId).then((v) => alive && setVersions(v)).catch(() => {});
    api
      .getFlow(flowId)
      .then((f) => {
        if (!alive) return;
        setFlow(f);
        const { nodes: n, edges: e } = toReactFlow(f);
        setNodes(n);
        setEdges(e);
        setConfigById(Object.fromEntries(f.nodes.map((x) => [x.node_id, x.config ?? {}])));
        setDirty(false);
        setBanner(null);

        const pend = sessionStorage.getItem(`pendingCandidate:${flowId}`);
        if (pend) {
          sessionStorage.removeItem(`pendingCandidate:${flowId}`);
          try {
            putCandidate(
              f,
              JSON.parse(pend) as FlowCandidate,
              "loaded onto the canvas — review, then Save draft",
            );
          } catch {
            /* stale handoff — ignore */
          }
        } else if (sessionStorage.getItem(`pendingAssistMode:${flowId}`) === "mermaid") {
          sessionStorage.removeItem(`pendingAssistMode:${flowId}`);
          setAssist("mermaid");
          setAssistText("");
          setAssistErr(null);
        }
      })
      .catch((e: ApiError) => setBanner({ kind: "err", text: e.message }));
    return () => {
      alive = false;
    };
  }, [flowId, setNodes, setEdges, putCandidate]);

  // once the type registry has loaded, flag any node whose type isn't in it
  useEffect(() => {
    if (!types) return;
    setNodes((ns) => {
      let changed = false;
      const next = ns.map((n) => {
        const invalid = !types.types.includes(n.data.nodeType);
        if (invalid === !!n.data.invalid) return n;
        changed = true;
        return { ...n, data: { ...n.data, invalid } };
      });
      return changed ? next : ns;
    });
  }, [types, setNodes]);

  async function runAssist() {
    if (!flow) return;
    setAssistBusy(true);
    setAssistErr(null);
    try {
      if (assist === "mermaid") {
        const res = await api.importMermaid(assistText);
        putCandidate(flow, res, `imported ${res.nodes.length} node(s) from Mermaid`);
      } else {
        const res = await api.assistEditFlow(flowId, assistText);
        const d = res.diff;
        const tail = res.summary ? ` — ${res.summary}` : "";
        const note = d
          ? `AI edit · +${d.added_nodes.length}/−${d.removed_nodes.length}/~${d.changed_nodes.length} nodes${tail}`
          : `AI edit applied${tail}`;
        putCandidate(flow, res, note);
      }
      setAssist(null);
      setAssistText("");
    } catch (e) {
      setAssistErr((e as ApiError).message);
    }
    setAssistBusy(false);
  }

  const onConnect = useCallback(
    (c: Connection) => {
      const id = uuid();
      setEdges((es) =>
        addEdge(
          { id, source: c.source!, target: c.target!, data: { condition: {} }, label: "" },
          es,
        ),
      );
      // jump straight to the new edge so its condition ("if") form is editable
      setSelNode(null);
      setSelEdge(id);
      mark();
    },
    [setEdges, mark],
  );

  function addNode(t: string) {
    const id = uuid();
    mark();
    setNodes((ns) => {
      const k = ns.length;
      return [
        ...ns,
        {
          id,
          type: "flowNode",
          // spread new nodes on a clear grid away from the palette / controls
          position: { x: 220 + (k % 4) * 210, y: 90 + (k % 6) * 90 },
          data: {
            label: t,
            nodeType: t,
            terminal: TERMINAL.has(t),
            summary: summarize(t, types?.defaults[t] ?? {}),
            invalid: types ? !types.types.includes(t) : false,
          },
        },
      ];
    });
    setConfigById((m) => ({ ...m, [id]: structuredClone(types?.defaults[t] ?? {}) }));
    setSelNode(id);
    setPaletteOpen(false);
  }

  const onNodesDelete = useCallback(
    (dels: { id: string }[]) => {
      setConfigById((m) => {
        const n = { ...m };
        dels.forEach((d) => delete n[d.id]);
        return n;
      });
      mark();
    },
    [mark],
  );

  function setLabel(id: string, v: string) {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, label: v } } : n)));
    mark();
  }
  function setConfig(id: string, v: Record<string, unknown>) {
    setConfigById((m) => ({ ...m, [id]: v }));
    setNodes((ns) =>
      ns.map((n) =>
        n.id === id ? { ...n, data: { ...n.data, summary: summarize(n.data.nodeType, v) } } : n,
      ),
    );
    mark();
  }
  function setEdgeCond(id: string, c: Record<string, unknown>) {
    const ifExpr = (c as { if?: string }).if ?? "";
    setEdges((es) =>
      es.map((e) =>
        e.id === id ? { ...e, data: { condition: c }, label: ifExpr, animated: !!ifExpr } : e,
      ),
    );
    mark();
  }

  const payload = () => toFlowPayload(flow!, nodes, edges, configById);

  async function doValidate() {
    setBusy(true);
    try {
      const r = await api.validateFlow(flowId, payload());
      if (r.valid) {
        setBanner(null);
        toast("Flow is valid");
      } else {
        setBanner({ kind: "err", text: "This flow won't run yet", list: r.errors });
      }
    } catch (e) {
      setBanner({ kind: "err", text: (e as ApiError).message });
    }
    setBusy(false);
  }

  async function reload() {
    const f = await api.getFlow(flowId);
    setFlow(f);
    const { nodes: n, edges: e } = toReactFlow(f);
    setNodes(n);
    setEdges(e);
    setConfigById(Object.fromEntries(f.nodes.map((x) => [x.node_id, x.config ?? {}])));
    setDirty(false);
  }

  async function doSave() {
    setBusy(true);
    try {
      const f = await api.saveFlow(flowId, payload());
      setFlow(f);
      setDirty(false);
      setBanner(null);
      toast(`Saved · draft v${f.version}`);
      onSaved();
    } catch (e) {
      const ae = e as ApiError;
      if (ae.status === 409) {
        await reload();
        setBanner({
          kind: "err",
          text: "Someone else saved this flow while you were editing. Their version is now loaded — re-apply your changes and save again.",
        });
      } else {
        setBanner({
          kind: "err",
          text: ae.errors ? "This flow won't save yet" : ae.message,
          list: ae.errors ?? undefined,
        });
      }
    }
    setBusy(false);
  }

  async function runPublish() {
    setDialog(null);
    setBusy(true);
    try {
      if (dirty) await api.saveFlow(flowId, payload());
      const { published_version } = await api.publishFlow(flowId);
      await reload();
      setVersions(await api.listVersions(flowId));
      setBanner(null);
      toast(`Published v${published_version}`);
      onSaved();
    } catch (e) {
      const ae = e as ApiError;
      setBanner({
        kind: "err",
        text: ae.errors ? "Can't publish — this flow is invalid" : ae.message,
        list: ae.errors ?? undefined,
      });
    }
    setBusy(false);
  }

  async function runSaveTemplate() {
    if (!flow || !tplName.trim()) return;
    setDialog(null);
    setBusy(true);
    try {
      if (dirty) await api.saveFlow(flowId, payload());
      await api.templates.save(flowId, { name: tplName.trim(), description: tplDesc.trim() || undefined });
      toast(`Saved as template "${tplName.trim()}"`);
    } catch (e) {
      setBanner({ kind: "err", text: (e as ApiError).message });
    }
    setBusy(false);
  }

  async function doSetSfEntry(on: boolean) {
    setBusy(true);
    try {
      await api.setSfEntry(flowId, on);
      await reload();
      toast(on ? "This flow now runs on new Salesforce Cases" : "Disconnected from Salesforce");
      onSaved();
    } catch (e) {
      setBanner({ kind: "err", text: (e as ApiError).message });
    }
    setBusy(false);
  }

  async function runRollback(v: number) {
    setDialog(null);
    setBusy(true);
    try {
      await api.rollbackFlow(flowId, v);
      await reload();
      toast(`Rolled back to v${v}`);
      onSaved();
    } catch (e) {
      setBanner({ kind: "err", text: (e as ApiError).message });
    }
    setBusy(false);
  }

  async function runDelete() {
    setDialog(null);
    try {
      await api.deleteFlow(flowId);
      onDeleted();
    } catch (e) {
      setBanner({ kind: "err", text: (e as ApiError).message });
    }
  }

  if (!flow) return <div style={{ padding: 20 }} className="muted">loading…</div>;

  const selectedNode = nodes.find((n) => n.id === selNode) || null;
  const selectedEdge = edges.find((e) => e.id === selEdge) || null;

  return (
    <>
      <div className="app-toolbar">
        <Toolbar
          title={
            <input
              className="toolbar__name"
              value={flow.name}
              disabled={!canEdit}
              aria-label="Flow name"
              onChange={(e) => {
                setFlow({ ...flow, name: e.target.value });
                mark();
              }}
            />
          }
          meta={
            <>
              <span
                className={`pill ${flow.published_version ? "published" : ""}`}
                title="what a run executes"
              >
                {flow.published_version ? `published v${flow.published_version}` : "unpublished"}
              </span>
              <span title="draft revision (optimistic-concurrency token)">draft rev {flow.version}</span>
              {dirty && <span title="unsaved changes" style={{ color: "var(--accent)" }}>● unsaved</span>}
              {!canEdit && (
                <span className="pill" title="your access is view-only">view-only</span>
              )}
              {canEdit ? (
                <Toggle checked={!!flow.sf_entry} disabled={busy} onChange={(on) => doSetSfEntry(on)}>
                  <span
                    className="muted"
                    title="when on, POST /api/hooks/salesforce/case runs this flow for every new Case (one flow per workspace)"
                  >
                    Salesforce entry
                  </span>
                </Toggle>
              ) : (
                flow.sf_entry && (
                  <span className="pill published" title="the Salesforce Case hook runs this flow">
                    Salesforce entry
                  </span>
                )
              )}
            </>
          }
        >
          <Button variant="ghost" onClick={doValidate} disabled={busy}>
            Validate
          </Button>
          {canEdit && (
            <>
              <Button variant="primary" onClick={doSave} disabled={busy || !dirty}>
                Save draft
              </Button>
              <Button variant="secondary" onClick={() => setDialog("publish")} disabled={busy}>
                Publish
              </Button>
              <Button
                ref={moreBtnRef}
                variant="secondary"
                aria-haspopup="menu"
                aria-expanded={moreOpen}
                onClick={() => setMoreOpen((o) => !o)}
              >
                More ▾
              </Button>
              <Popover anchorRef={moreBtnRef} open={moreOpen} onClose={() => setMoreOpen(false)} width={220} align="end">
                <div className="palette__list">
                  <button className="palette__item" onClick={() => { setMoreOpen(false); setNodes((ns) => layout(ns, edges)); }}>
                    Re-layout
                  </button>
                  <button className="palette__item" onClick={() => { setMoreOpen(false); setAssist("mermaid"); setAssistText(""); setAssistErr(null); }}>
                    Import Mermaid
                  </button>
                  <button className="palette__item" onClick={() => { setMoreOpen(false); setAssist("ai-edit"); setAssistText(""); setAssistErr(null); }}>
                    ✨ AI edit
                  </button>
                  <button
                    className="palette__item"
                    onClick={() => { setMoreOpen(false); setTplName(flow.name); setTplDesc(""); setDialog("template"); }}
                  >
                    Save as template
                  </button>
                  <button className="palette__item" style={{ color: "var(--exception-text)" }} onClick={() => { setMoreOpen(false); setDialog("delete"); }}>
                    Delete flow
                  </button>
                </div>
              </Popover>
              {versions.length > 0 && (
                <>
                  <Button
                    ref={rollbackBtnRef}
                    variant="secondary"
                    aria-haspopup="menu"
                    aria-expanded={rollbackOpen}
                    onClick={() => setRollbackOpen((o) => !o)}
                  >
                    rollback ▾
                  </Button>
                  <Popover
                    anchorRef={rollbackBtnRef}
                    open={rollbackOpen}
                    onClose={() => setRollbackOpen(false)}
                    width={220}
                    align="end"
                  >
                    <div className="palette__kicker">restore a version</div>
                    <div className="palette__list">
                      {versions.map((v) => (
                        <button
                          key={v.version}
                          className="palette__item"
                          onClick={() => { setRollbackOpen(false); setDialog({ rollback: v.version }); }}
                        >
                          v{v.version}
                          <span className="muted" style={{ fontSize: 11 }}>
                            {new Date(v.created_at).toLocaleDateString()}
                          </span>
                        </button>
                      ))}
                    </div>
                  </Popover>
                </>
              )}
            </>
          )}
        </Toolbar>
      </div>

      <TriggersPanel flowId={flowId} canEdit={canEdit} />

      <div className="workarea">
        <div className="canvas-wrap">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodesDelete={onNodesDelete}
            onEdgesDelete={mark}
            onNodeClick={(_, n) => {
              setSelNode(n.id);
              setSelEdge(null);
            }}
            onEdgeClick={(_, e) => {
              setSelEdge(e.id);
              setSelNode(null);
            }}
            onPaneClick={() => {
              setSelNode(null);
              setSelEdge(null);
            }}
            edgesFocusable
            nodesDraggable={canEdit}
            nodesConnectable={canEdit}
            elementsSelectable
            defaultEdgeOptions={{ interactionWidth: 24 }}
            onNodeDragStart={() => canEdit && snapshot()}
            onNodeDragStop={() => canEdit && setDirty(true)}
            onMoveEnd={(_, vp) => {
              try {
                localStorage.setItem(`flow-viewport:${flowId}`, JSON.stringify(vp));
              } catch {
                /* private mode — persistence is best-effort */
              }
            }}
            deleteKeyCode={canEdit ? ["Backspace", "Delete"] : null}
            defaultViewport={savedViewport ?? undefined}
            fitView={!savedViewport}
            minZoom={0.25}
            maxZoom={2}
            /* wheel / two-finger trackpad scrolls the canvas (both axes —
               React Flow's default panOnScrollMode is "free"); pinch or
               ⌘/Ctrl+wheel still zooms */
            panOnScroll
            colorMode="dark"
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={18} />
            <MiniMap
              className="flow-minimap"
              pannable
              zoomable
              maskColor="rgba(20, 19, 18, 0.62)"
              nodeColor="var(--accent)"
              nodeStrokeColor="transparent"
            />
          </ReactFlow>

          <ZoomControl />

          {canEdit && (
            <>
              <button
                type="button"
                ref={paletteBtnRef}
                className="canvas-fab"
                aria-haspopup="menu"
                aria-expanded={paletteOpen}
                onClick={() => setPaletteOpen((o) => !o)}
              >
                ＋ Add node ▾
              </button>
              <Popover
                anchorRef={paletteBtnRef}
                open={paletteOpen}
                onClose={() => setPaletteOpen(false)}
                width={240}
              >
                <div className="palette__kicker">Node types</div>
                <input
                  autoFocus
                  className="ui-input"
                  value={nodeFilter}
                  placeholder="filter…"
                  onChange={(e) => setNodeFilter(e.target.value)}
                  style={{ margin: "0 2px 4px" }}
                />
                <div className="palette__list">
                  {(types?.types ?? [])
                    .filter((t) => t.includes(nodeFilter.trim().toLowerCase()))
                    .map((t) => (
                      <button
                        key={t}
                        type="button"
                        className="palette__item"
                        onClick={() => addNode(t)}
                        title={`add a ${t} node`}
                      >
                        {t}
                        <span className="muted" style={{ fontSize: 10.5 }}>
                          {TERMINAL.has(t) ? "terminal" : "node"}
                        </span>
                      </button>
                    ))}
                  {(types?.types ?? []).filter((t) => t.includes(nodeFilter.trim().toLowerCase())).length === 0 && (
                    <div className="muted" style={{ padding: "8px 10px", fontSize: 12 }}>
                      no match
                    </div>
                  )}
                </div>
              </Popover>
            </>
          )}

          {canEdit && (past.current.length > 0 || future.current.length > 0) && (
            <div
              style={{ position: "absolute", right: 10, top: 10, display: "flex", gap: 4, zIndex: 5 }}
            >
              <button onClick={undo} disabled={past.current.length === 0} title="Ctrl/Cmd+Z">
                undo
              </button>
              <button onClick={redo} disabled={future.current.length === 0} title="Ctrl/Cmd+Shift+Z">
                redo
              </button>
            </div>
          )}

          {banner && (
            <div style={{ position: "absolute", right: 15, bottom: 15, maxWidth: 460, zIndex: 12 }}>
              <Banner
                tone={banner.kind === "err" ? "exception" : "warn"}
                title={banner.text}
                detail={
                  banner.list && banner.list.length > 0 ? (
                    <ul style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                      {banner.list.map((x, i) => {
                        const hit = nodes.find(
                          (n) => x.includes(n.id) || (n.data.label && x.includes(n.data.label)),
                        );
                        return (
                          <li key={i}>
                            {hit ? (
                              <Button
                                variant="ghost"
                                size="sm"
                                style={{ padding: 0, height: "auto", font: "inherit" }}
                                onClick={() => {
                                  setSelNode(hit.id);
                                  setSelEdge(null);
                                }}
                              >
                                {x}
                              </Button>
                            ) : (
                              x
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  ) : undefined
                }
                actions={
                  <Button variant="ghost" size="sm" onClick={() => setBanner(null)}>
                    Dismiss
                  </Button>
                }
              />
            </div>
          )}
        </div>
      </div>

      <InspectorPanel
        node={selectedNode}
        edge={selectedNode ? null : selectedEdge}
        config={selectedNode ? configById[selectedNode.id] ?? {} : {}}
        tenantId={flow?.tenant_id || ""}
        flowId={flowId}
        dirty={dirty}
        inCount={selectedNode ? edges.filter((e) => e.target === selectedNode.id).length : 0}
        outCount={selectedNode ? edges.filter((e) => e.source === selectedNode.id).length : 0}
        onLabel={(v) => selectedNode && setLabel(selectedNode.id, v)}
        onConfig={(v) => selectedNode && setConfig(selectedNode.id, v)}
        onCondition={(c) => selectedEdge && setEdgeCond(selectedEdge.id, c)}
        onRevert={() => {
          if (!selectedNode || !flow) return;
          const saved = flow.nodes.find((n) => n.node_id === selectedNode.id);
          if (!saved) return;
          setLabel(selectedNode.id, saved.label || saved.type);
          setConfig(selectedNode.id, saved.config ?? {});
        }}
        onDeleteNode={() => {
          if (!selectedNode) return;
          const id = selectedNode.id;
          setNodes((ns) => ns.filter((n) => n.id !== id));
          setEdges((es) => es.filter((e) => e.source !== id && e.target !== id));
          onNodesDelete([{ id }]);
          setSelNode(null);
        }}
        onDeleteEdge={() => {
          if (!selectedEdge) return;
          const id = selectedEdge.id;
          setEdges((es) => es.filter((e) => e.id !== id));
          mark();
          setSelEdge(null);
        }}
        onClose={() => {
          setSelNode(null);
          setSelEdge(null);
        }}
      />

      <SlideOver
        open={!!assist}
        onClose={() => !assistBusy && setAssist(null)}
        title={assist === "mermaid" ? "Import a Mermaid flowchart" : "Edit this flow with AI"}
        width={assist === "mermaid" ? 520 : 380}
        footer={
          <>
            <Button
              variant="primary"
              onClick={runAssist}
              loading={assistBusy}
              disabled={!assistText.trim()}
            >
              {assist === "mermaid" ? "Import" : "Generate"}
            </Button>
            <Button variant="ghost" onClick={() => setAssist(null)} disabled={assistBusy}>
              Cancel
            </Button>
          </>
        }
      >
        <p className="muted" style={{ margin: "0 0 12px", fontSize: 12.5, lineHeight: 1.6 }}>
          {assist === "mermaid"
            ? "Paste a flowchart. It replaces the canvas as an unsaved draft — node types are matched by label, edge labels become warnings to wire up, nothing saves until you hit Save draft."
            : "Describe the change in plain English (e.g. “add a clarify step when the gate fails for non-billing topics”). The AI rewrites the graph for you to review on the canvas; nothing saves until you hit Save draft."}
        </p>
        <Textarea
          rows={assist === "mermaid" ? 14 : 5}
          value={assistText}
          autoFocus
          placeholder={
            assist === "mermaid"
              ? "flowchart TD\n  R[retrieve] --> C[classify] --> D[draft]\n  D --> G{confidence gate}\n  G -->|pass| A[auto reply]\n  G -->|fail| H[ask human]"
              : "add an identify step before classify, and route unknown senders to a clarify node"
          }
          onChange={(e) => setAssistText(e.target.value)}
        />
        {assistErr && <div className="err" style={{ fontSize: 12, marginTop: 8 }}>{assistErr}</div>}
      </SlideOver>

      <Dialog
        open={dialog === "publish"}
        onClose={() => setDialog(null)}
        title={`Publish v${(flow.published_version ?? 0) + 1}?`}
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setDialog(null) },
          { label: "Publish", variant: "primary", onClick: runPublish },
        ]}
      >
        Runs switch to this snapshot immediately
        {dirty ? " — your unsaved draft is saved first" : ""}. The working draft is
        unaffected.
      </Dialog>

      <Dialog
        open={dialog === "delete"}
        onClose={() => setDialog(null)}
        title="Delete this flow?"
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setDialog(null) },
          { label: "Delete flow", variant: "danger", onClick: runDelete },
        ]}
      >
        This removes the flow and all of its nodes and edges. This can't be undone.
      </Dialog>

      <Dialog
        open={typeof dialog === "object" && dialog !== null}
        onClose={() => setDialog(null)}
        title={`Roll back to v${typeof dialog === "object" && dialog ? dialog.rollback : ""}?`}
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setDialog(null) },
          {
            label: "Roll back",
            variant: "primary",
            onClick: () => {
              if (typeof dialog === "object" && dialog) runRollback(dialog.rollback);
            },
          },
        ]}
      >
        The working draft and the published pointer both move to this version.
      </Dialog>

      <Dialog
        open={dialog === "template"}
        onClose={() => setDialog(null)}
        title="Save as template"
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setDialog(null) },
          { label: "Save template", variant: "primary", onClick: runSaveTemplate },
        ]}
      >
        <div style={{ display: "grid", gap: 12 }}>
          <Field label="Template name">
            <Input value={tplName} autoFocus onChange={(e) => setTplName(e.target.value)} />
          </Field>
          <Field label="Description" hint="optional">
            <Input value={tplDesc} onChange={(e) => setTplDesc(e.target.value)} />
          </Field>
        </div>
      </Dialog>
    </>
  );
}
