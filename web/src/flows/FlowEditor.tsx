import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  addEdge,
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  type Connection,
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
import { NodeCard } from "./NodeCard";
import { EdgeInspector, NodeInspector } from "./Inspector";
import { RunPanel } from "./RunPanel";

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

type Banner = { kind: "ok" | "err"; text: string; list?: string[] };

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
  const [banner, setBanner] = useState<Banner | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [versions, setVersions] = useState<{ version: number; created_at: string }[]>([]);
  const [assist, setAssist] = useState<null | "mermaid" | "ai-edit">(null);
  const [assistText, setAssistText] = useState("");
  const [assistErr, setAssistErr] = useState<string | null>(null);
  const [assistBusy, setAssistBusy] = useState(false);

  const nodeTypes = useMemo(() => ({ flowNode: NodeCard }), []);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [nodeFilter, setNodeFilter] = useState("");

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
          data: { label: t, nodeType: t, terminal: TERMINAL.has(t) },
        },
      ];
    });
    setConfigById((m) => ({ ...m, [id]: structuredClone(types?.defaults[t] ?? {}) }));
    setSelNode(id);
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
      setBanner(
        r.valid
          ? { kind: "ok", text: "valid" }
          : { kind: "err", text: "invalid", list: r.errors },
      );
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
      setBanner({ kind: "ok", text: `saved · draft v${f.version}` });
      onSaved();
    } catch (e) {
      const ae = e as ApiError;
      if (ae.status === 409) {
        await reload();
        setBanner({ kind: "err", text: "someone else saved this flow — reloaded their version" });
      } else {
        setBanner({ kind: "err", text: ae.errors ? "save blocked" : ae.message, list: ae.errors ?? undefined });
      }
    }
    setBusy(false);
  }

  async function doPublish() {
    if (dirty && !confirm("Save first, then publish the draft?")) return;
    setBusy(true);
    try {
      if (dirty) await api.saveFlow(flowId, payload());
      const { published_version } = await api.publishFlow(flowId);
      await reload();
      setVersions(await api.listVersions(flowId));
      setBanner({ kind: "ok", text: `published v${published_version}` });
      onSaved();
    } catch (e) {
      const ae = e as ApiError;
      setBanner({ kind: "err", text: ae.errors ? "can't publish — flow is invalid" : ae.message, list: ae.errors ?? undefined });
    }
    setBusy(false);
  }

  async function doSetSfEntry(on: boolean) {
    setBusy(true);
    try {
      await api.setSfEntry(flowId, on);
      await reload();
      setBanner({
        kind: "ok",
        text: on ? "this flow now runs on new Salesforce Cases" : "disconnected from Salesforce",
      });
      onSaved();
    } catch (e) {
      setBanner({ kind: "err", text: (e as ApiError).message });
    }
    setBusy(false);
  }

  async function doRollback(v: number) {
    if (!confirm(`Roll back the draft + published pointer to v${v}?`)) return;
    setBusy(true);
    try {
      await api.rollbackFlow(flowId, v);
      await reload();
      setBanner({ kind: "ok", text: `rolled back to v${v}` });
      onSaved();
    } catch (e) {
      setBanner({ kind: "err", text: (e as ApiError).message });
    }
    setBusy(false);
  }

  async function doDelete() {
    if (!confirm("Delete this flow and its nodes/edges?")) return;
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
      <div className="toolbar">
        <input
          style={{ width: 260 }}
          value={flow.name}
          onChange={(e) => {
            setFlow({ ...flow, name: e.target.value });
            mark();
          }}
        />
        <span
          className={`pill ${flow.published_version ? "published" : "draft"}`}
          title="what a run executes"
        >
          {flow.published_version ? `published v${flow.published_version}` : "unpublished"}
        </span>
        <span className="muted" title="draft revision (optimistic-concurrency token)">
          draft rev {flow.version}
        </span>
        {!canEdit && (
          <span className="pill" title="your access is view-only">view-only</span>
        )}
        {canEdit ? (
          <label
            className={`pill ${flow.sf_entry ? "published" : ""}`}
            title="when on, POST /api/hooks/salesforce/case runs this flow for every new Case (one flow per workspace)"
            style={{ cursor: busy ? "wait" : "pointer" }}
          >
            <input
              type="checkbox"
              checked={!!flow.sf_entry}
              disabled={busy}
              onChange={(e) => doSetSfEntry(e.target.checked)}
              style={{ marginRight: 4 }}
            />
            Salesforce entry
          </label>
        ) : (
          flow.sf_entry && (
            <span className="pill published" title="the Salesforce Case hook runs this flow">
              Salesforce entry
            </span>
          )
        )}
        {canEdit && versions.length > 0 && (
          <select
            value=""
            onChange={(e) => e.target.value && doRollback(Number(e.target.value))}
            title="roll back to a published version"
            style={{ width: "auto" }}
          >
            <option value="">rollback…</option>
            {versions.map((v) => (
              <option key={v.version} value={v.version}>
                v{v.version} · {new Date(v.created_at).toLocaleDateString()}
              </option>
            ))}
          </select>
        )}
        <div style={{ flex: 1 }} />
        {dirty && <span className="muted" title="unsaved changes">●</span>}
        <button onClick={doValidate} disabled={busy}>Validate</button>
        {canEdit && (
          <>
            <button onClick={() => setNodes((ns) => layout(ns, edges))}>Re-layout</button>
            <button
              onClick={() => { setAssist("mermaid"); setAssistText(""); setAssistErr(null); }}
              title="replace the canvas with a Mermaid flowchart"
            >
              Import Mermaid
            </button>
            <button
              onClick={() => { setAssist("ai-edit"); setAssistText(""); setAssistErr(null); }}
              title="describe a change; AI rewrites the graph for you to review"
            >
              ✨ AI edit
            </button>
            <button className="primary" onClick={doSave} disabled={busy || !dirty}>Save draft</button>
            <button onClick={doPublish} disabled={busy}>Publish</button>
            <button className="err" onClick={doDelete}>Delete</button>
          </>
        )}
      </div>

      <div className="workarea">
        <div className="canvas-wrap">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
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
            deleteKeyCode={canEdit ? ["Backspace", "Delete"] : null}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>

          {canEdit && (
            <div
              style={{
                position: "absolute",
                left: 10,
                top: 10,
                width: paletteOpen ? 210 : "auto",
                maxHeight: "calc(100% - 20px)",
                display: "flex",
                flexDirection: "column",
                gap: 6,
                padding: 8,
                borderRadius: 8,
                background: "color-mix(in srgb, var(--panel) 96%, transparent)",
                border: "1px solid var(--border)",
                zIndex: 5,
              }}
            >
              <div className="row" style={{ gap: 6, justifyContent: "space-between" }}>
                <button
                  className={paletteOpen ? "primary" : ""}
                  onClick={() => setPaletteOpen((o) => !o)}
                >
                  {paletteOpen ? "✕ close" : "＋ add node"}
                </button>
              </div>
              {paletteOpen && (
                <>
                  <input
                    autoFocus
                    value={nodeFilter}
                    placeholder="filter…"
                    onChange={(e) => setNodeFilter(e.target.value)}
                  />
                  <div style={{ overflowY: "auto", display: "flex", flexDirection: "column", gap: 4 }}>
                    {(types?.types ?? [])
                      .filter((t) => t.includes(nodeFilter.trim().toLowerCase()))
                      .map((t) => (
                        <button
                          key={t}
                          onClick={() => addNode(t)}
                          title={`add a ${t} node`}
                          style={{ textAlign: "left" }}
                        >
                          ＋ {t}
                        </button>
                      ))}
                  </div>
                </>
              )}
            </div>
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
            <div
              style={{ position: "absolute", right: 10, bottom: 10, maxWidth: 420 }}
              className={`banner ${banner.kind}`}
            >
              {banner.text}
              {banner.list && (
                <ul style={{ margin: "4px 0 0 16px" }}>
                  {banner.list.map((x, i) => (
                    <li key={i}>{x}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        <div className="inspector col">
          {selectedNode ? (
            <NodeInspector
              node={selectedNode}
              config={configById[selectedNode.id] ?? {}}
              onLabel={(v) => setLabel(selectedNode.id, v)}
              onConfig={(v) => setConfig(selectedNode.id, v)}
              onDelete={() => {
                setNodes((ns) => ns.filter((n) => n.id !== selectedNode.id));
                setEdges((es) =>
                  es.filter((e) => e.source !== selectedNode.id && e.target !== selectedNode.id),
                );
                onNodesDelete([{ id: selectedNode.id }]);
              }}
            />
          ) : selectedEdge ? (
            <EdgeInspector
              edge={selectedEdge}
              onCondition={(c) => setEdgeCond(selectedEdge.id, c)}
              onDelete={() => {
                setEdges((es) => es.filter((e) => e.id !== selectedEdge.id));
                mark();
              }}
            />
          ) : (
            <div className="muted">select a node or edge · drag between handles to connect · Del removes</div>
          )}

          <hr style={{ borderColor: "var(--border)", width: "100%" }} />
          <RunPanel flowId={flowId} />
        </div>
      </div>

      {assist && (
        <div className="assist-overlay" onClick={() => !assistBusy && setAssist(null)}>
          <div className="assist-modal col" onClick={(e) => e.stopPropagation()}>
            <h4>
              {assist === "mermaid"
                ? "Import a Mermaid flowchart"
                : "Edit this flow with AI"}
            </h4>
            <div className="muted" style={{ fontSize: 12 }}>
              {assist === "mermaid"
                ? "Paste a flowchart. It replaces the canvas as an unsaved draft — node types are matched by label, edge labels become warnings to wire up, nothing saves until you hit Save draft."
                : "Describe the change in plain English (e.g. “add a clarify step when the gate fails for non-billing topics”). The AI rewrites the graph for you to review on the canvas; nothing saves until you hit Save draft."}
            </div>
            <textarea
              rows={assist === "mermaid" ? 12 : 4}
              value={assistText}
              autoFocus
              placeholder={
                assist === "mermaid"
                  ? "flowchart TD\n  R[retrieve] --> C[classify] --> D[draft]\n  D --> G{confidence gate}\n  G -->|pass| A[auto reply]\n  G -->|fail| H[ask human]"
                  : "add an identify step before classify, and route unknown senders to a clarify node"
              }
              onChange={(e) => setAssistText(e.target.value)}
            />
            {assistErr && <div className="err" style={{ fontSize: 12 }}>{assistErr}</div>}
            <div className="row" style={{ justifyContent: "flex-end", gap: 6 }}>
              <button onClick={() => setAssist(null)} disabled={assistBusy}>cancel</button>
              <button
                className="primary"
                onClick={runAssist}
                disabled={assistBusy || !assistText.trim()}
              >
                {assistBusy ? "…" : assist === "mermaid" ? "Import" : "Generate"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
