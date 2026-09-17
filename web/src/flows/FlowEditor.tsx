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
  neighborhood,
  nodeLabel,
  TERMINAL,
  toFlowPayload,
  toReactFlow,
  uuid,
  type RFEdge,
  type RFNode,
} from "./graph";
import { nodeKind, summarize } from "./nodeSummary";
import { NodeCard } from "./NodeCard";
import { EdgeLabel } from "./EdgeLabel";
import { ZoomControl } from "./ZoomControl";
import { CanvasLegend } from "./CanvasLegend";
import { InspectorPanel } from "./InspectorPanel";
import { RunPanel } from "./RunPanel";
import { ConditionsOverview } from "./ConditionsOverview";
import { EditorHelp } from "./EditorHelp";
import { ChatEditView, type ChatLogEntry } from "./ChatEditView";
import { TriggersPanel } from "./TriggersPanel";
import { Popover, Toolbar, Button, Banner, Dialog, SlideOver, Field, Input, Textarea, Toggle, useToast, useColorMode } from "../ui";

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

// Mirrors .nodecard--{kind}'s border colors (ui.css) so the zoomed-out
// minimap reads as the same map, not a flat blob of one color per node.
const MINIMAP_KIND_COLOR: Record<ReturnType<typeof nodeKind>, string> = {
  trigger: "var(--accent)",
  terminal: "var(--warn)",
  invalid: "var(--exception)",
  broken: "var(--exception)",
  work: "var(--text-faint)",
};

function Inner({ flowId, canEdit, onSaved, onDeleted }: {
  flowId: string;
  canEdit: boolean;
  onSaved: () => void;
  onDeleted: () => void;
}) {
  const colorMode = useColorMode();
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
  const [assist, setAssist] = useState<null | "mermaid">(null);
  const [assistText, setAssistText] = useState("");
  const [assistErr, setAssistErr] = useState<string | null>(null);
  const [assistBusy, setAssistBusy] = useState(false);
  // separate from the Mermaid-import overlay's assist* state above — chat
  // and Mermaid import are two independent actions now (chat is inline,
  // not an overlay), and sharing one busy/error pair would show a stale
  // Mermaid error inside the chat view or vice versa.
  const [chatBusy, setChatBusy] = useState(false);
  const [chatErr, setChatErr] = useState<string | null>(null);
  const [testRunOpen, setTestRunOpen] = useState(false);
  const [conditionsOpen, setConditionsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);

  // Which main panel occupies the canvas-wrap slot. Defaults to "chat" (the
  // "AI-edit as the front door" ask — describing changes in plain English,
  // not the node graph, is the first thing anyone sees) but remembers a
  // manual switch per flow, and a view-only caller never gets it at all —
  // there's nothing to generate without edit rights, so it'd just be an
  // empty room. `Inner` remounts per flowId (see App), so this doesn't leak
  // between flows without going through localStorage.
  const [mainView, setMainView] = useState<"chat" | "canvas">(() => {
    if (!canEdit) return "canvas";
    try {
      return localStorage.getItem(`flow-mainview:${flowId}`) === "canvas" ? "canvas" : "chat";
    } catch {
      return "chat";
    }
  });
  const [chatLog, setChatLog] = useState<ChatLogEntry[]>([]);

  function switchView(v: "chat" | "canvas") {
    setMainView(v);
    if (v === "chat") {
      setSelNode(null);
      setSelEdge(null);
    }
    try {
      localStorage.setItem(`flow-mainview:${flowId}`, v);
    } catch {
      /* private mode — best-effort */
    }
  }

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
  const [findOpen, setFindOpen] = useState(false);
  const [findQuery, setFindQuery] = useState("");
  const findBtnRef = useRef<HTMLButtonElement>(null);
  const findInputRef = useRef<HTMLInputElement>(null);
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

  // Once the type registry has loaded, flag any node whose type isn't in
  // it. Also re-runs on `nodes.length` (not just `types`) — the type
  // registry and the flow's own nodes load via two independent fetches,
  // and when node-types happens to resolve before the flow does, this ran
  // once against the still-empty node list and never fired again once the
  // flow's real nodes showed up, silently leaving a genuinely invalid node
  // type unflagged. Cheap to re-run: the inner setNodes bails out to the
  // same array reference when nothing actually changed.
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
  }, [types, nodes.length, setNodes]);

  // Live version of one of builder.py's build_graph checks: a node with >1
  // unconditional (no condition.if) outgoing edge is a FlowBuildError at
  // publish time — router precedence for two "always take this" edges is
  // undefined. Flagging it on the node itself, live, catches it while
  // wiring edges instead of only at Validate/Publish. (The other build_graph
  // checks — referential integrity, cycles, exactly-one-entry-point — stay
  // server-side in validate_flow.py/Validate; this is the one cheap to
  // recompute on every edge edit without forking that heavier logic.)
  useEffect(() => {
    const defaultOutCount = new Map<string, number>();
    for (const e of edges) {
      if (!(e.data?.condition as { if?: string } | undefined)?.if) {
        defaultOutCount.set(e.source, (defaultOutCount.get(e.source) ?? 0) + 1);
      }
    }
    setNodes((ns) => {
      let changed = false;
      const next = ns.map((n) => {
        const tooManyDefaults = (defaultOutCount.get(n.id) ?? 0) > 1;
        if (tooManyDefaults === !!n.data.tooManyDefaults) return n;
        changed = true;
        return { ...n, data: { ...n.data, tooManyDefaults } };
      });
      return changed ? next : ns;
    });
  }, [edges, setNodes]);

  // Another live build_graph check: build_graph requires exactly one root
  // (a node with no incoming edge). Zero is impossible once there's at
  // least one node with edges into it; >1 usually means a node got dropped
  // onto the canvas without ever being wired in from anywhere — plausible
  // to do by accident, and otherwise invisible until Publish fails.
  const disconnectedRoots = useMemo(() => {
    if (nodes.length < 2) return [];
    const hasIncoming = new Set(edges.map((e) => e.target));
    const roots = nodes.filter((n) => !hasIncoming.has(n.id));
    return roots.length > 1 ? roots : [];
  }, [nodes, edges]);

  // Selecting a node dims everything outside its focus neighborhood
  // (shared upstream + its own downstream, see graph.ts's `neighborhood`) —
  // on a flow with real branches (a confidence_gate fanning 6 ways), this
  // is what actually declutters the canvas: the sibling branches you're
  // not looking at fade, the shared setup stays visible. Deriving this into
  // `displayNodes`/`displayEdges` (never through `setNodes`/`setEdges`) so
  // it's purely a render-time overlay — it can't mark the flow dirty or
  // pollute undo/redo history. Only for a node selection; an edge selection
  // leaves everything at full opacity.
  const focus = useMemo(
    () => (selNode ? neighborhood(selNode, edges) : null),
    [selNode, edges],
  );
  const displayNodes = useMemo(
    () => (focus ? nodes.map((n) => ({ ...n, data: { ...n.data, dimmed: !focus.nodeIds.has(n.id) } })) : nodes),
    [nodes, focus],
  );
  const displayEdges = useMemo(
    () =>
      focus
        ? edges.map((e) => ({
            ...e,
            style: { ...e.style, opacity: focus.edgeIds.has(e.id) ? 1 : 0.15 },
          }))
        : edges,
    [edges, focus],
  );

  async function runMermaidImport() {
    if (!flow) return;
    setAssistBusy(true);
    setAssistErr(null);
    try {
      const res = await api.importMermaid(assistText);
      putCandidate(flow, res, `imported ${res.nodes.length} node(s) from Mermaid`);
      setAssist(null);
      setAssistText("");
    } catch (e) {
      setAssistErr((e as ApiError).message);
    }
    setAssistBusy(false);
  }

  // The chat view's own generate action — used to live inside runAssist's
  // "ai-edit" branch when AI-edit was a buried SlideOver overlay; now it's
  // the default landing view, driven by its own function (not the
  // mermaid-only `assist` state machine) plus a running, session-only
  // `chatLog` (never persisted — a page reload starts a fresh log, same as
  // this app has never had a real multi-turn chat memory on the backend;
  // pretending otherwise would be worse than just saying so in the UI).
  async function runChatEdit(promptText: string) {
    if (!flow || !promptText.trim()) return;
    setChatBusy(true);
    setChatErr(null);
    try {
      const res = await api.assistEditFlow(flowId, promptText.trim());
      const d = res.diff;
      const tail = res.summary ? ` — ${res.summary}` : "";
      const note = d
        ? `+${d.added_nodes.length} / −${d.removed_nodes.length} / ~${d.changed_nodes.length} nodes${tail}`
        : `applied${tail}`;
      putCandidate(flow, res, `AI edit · ${note}`);
      setChatLog((log) => [
        ...log,
        { id: uuid(), prompt: promptText.trim(), ok: res.errors.length === 0, note, list: [...res.errors, ...res.warnings] },
      ]);
    } catch (e) {
      const msg = (e as ApiError).message;
      setChatErr(msg);
      setChatLog((log) => [...log, { id: uuid(), prompt: promptText.trim(), ok: false, note: msg }]);
    }
    setChatBusy(false);
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
        // spread the existing data bag, not replace it -- a plain
        // `data: { condition: c }` here would silently drop `data.name`
        // (the edge's own display name) every time the condition changes.
        e.id === id ? { ...e, data: { ...e.data, condition: c }, label: ifExpr, animated: !!ifExpr } : e,
      ),
    );
    mark();
  }
  function setEdgeLabel(id: string, v: string) {
    setEdges((es) =>
      es.map((e) =>
        e.id === id ? { ...e, data: { condition: e.data?.condition ?? {}, name: v } } : e,
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
      toast(on ? "This flow now runs on new cases/tickets from your connected case system"
               : "Disconnected as the case-system entry flow");
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
              {disconnectedRoots.length > 1 && (
                <span
                  className="pill"
                  style={{ color: "var(--exception-text)", borderColor: "var(--exception)" }}
                  title={
                    "won't publish: " + disconnectedRoots.length + " nodes have no incoming edge from " +
                    "anywhere in this flow (" + disconnectedRoots.map((n) => n.data.label).join(", ") +
                    ") — a flow needs exactly one entry point"
                  }
                >
                  ⚠ {disconnectedRoots.length} disconnected entry points
                </span>
              )}
              {!canEdit && (
                <span className="pill" title="your access is view-only">view-only</span>
              )}
              {canEdit ? (
                <Toggle checked={!!flow.sf_entry} disabled={busy} onChange={(on) => doSetSfEntry(on)}>
                  <span
                    className="muted"
                    title="when on, this flow runs for every new case/ticket from your connected case system (Salesforce, HubSpot, ...) — one entry flow per workspace"
                  >
                    Case system entry
                  </span>
                </Toggle>
              ) : (
                flow.sf_entry && (
                  <span className="pill published" title="your connected case system's inbound hook/watcher runs this flow">
                    Case system entry
                  </span>
                )
              )}
            </>
          }
        >
          {canEdit && (
            <div className="view-toggle" role="group" aria-label="Main view">
              <button
                type="button"
                className={mainView === "chat" ? "is-active" : ""}
                aria-pressed={mainView === "chat"}
                onClick={() => switchView("chat")}
              >
                💬 Chat
              </button>
              <button
                type="button"
                className={mainView === "canvas" ? "is-active" : ""}
                aria-pressed={mainView === "canvas"}
                onClick={() => switchView("canvas")}
              >
                🗺️ Graph
              </button>
            </div>
          )}
          <Button variant="ghost" onClick={doValidate} disabled={busy}>
            Validate
          </Button>
          <Button
            variant="ghost"
            onClick={() => setTestRunOpen(true)}
            title="Run a sample case through this flow and see the full trace — doesn't require selecting a node"
          >
            Test run
          </Button>
          <Button
            variant="ghost"
            onClick={() => setConditionsOpen(true)}
            title="See every conditional edge in this flow in one place, instead of clicking each one"
          >
            Conditions
          </Button>
          <Button
            variant="ghost"
            onClick={() => setHelpOpen(true)}
            title="A quick reference for building, testing and publishing a flow on this screen"
          >
            ❓ Help
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
          {mainView === "chat" ? (
            <ChatEditView
              canEdit={canEdit}
              busy={chatBusy}
              err={chatErr}
              log={chatLog}
              hasNodes={nodes.length > 0}
              onGenerate={runChatEdit}
              onViewGraph={() => switchView("canvas")}
            />
          ) : (
          <>
          <ReactFlow
            nodes={displayNodes}
            edges={displayEdges}
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
            colorMode={colorMode}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={18} />
            <MiniMap
              className="flow-minimap"
              pannable
              zoomable
              maskColor="rgba(0, 0, 0, 0.4)"
              nodeColor={(n) => MINIMAP_KIND_COLOR[nodeKind(n.data as RFNode["data"])]}
              nodeStrokeColor="transparent"
            />
          </ReactFlow>

          <ZoomControl />
          <CanvasLegend />

          <div className="canvas-toolbar-tl">
            <button
              type="button"
              ref={findBtnRef}
              className="canvas-fab"
              aria-haspopup="menu"
              aria-expanded={findOpen}
              title="Jump to a node by name or type — useful once a flow has more nodes than fit on screen"
              onClick={() => {
                setFindOpen((o) => !o);
                setFindQuery("");
                requestAnimationFrame(() => findInputRef.current?.focus());
              }}
            >
              🔎 Find
            </button>
            <Popover anchorRef={findBtnRef} open={findOpen} onClose={() => setFindOpen(false)} width={240}>
              <input
                ref={findInputRef}
                autoFocus
                className="ui-input"
                value={findQuery}
                placeholder="node name or type…"
                onChange={(e) => setFindQuery(e.target.value)}
                style={{ margin: "0 2px 4px" }}
              />
              <div className="palette__list">
                {(() => {
                  const q = findQuery.trim().toLowerCase();
                  const hits = q
                    ? nodes.filter(
                        (n) => n.data.label.toLowerCase().includes(q) || n.data.nodeType.toLowerCase().includes(q),
                      )
                    : nodes;
                  if (hits.length === 0) {
                    return (
                      <div className="muted" style={{ padding: "8px 10px", fontSize: 12 }}>
                        no match
                      </div>
                    );
                  }
                  return hits.map((n) => (
                    <button
                      key={n.id}
                      type="button"
                      className="palette__item"
                      onClick={() => {
                        setSelNode(n.id);
                        setSelEdge(null);
                        setFindOpen(false);
                      }}
                    >
                      {n.data.label}
                      <span className="muted" style={{ fontSize: 10.5 }}>{n.data.nodeType}</span>
                    </button>
                  ));
                })()}
              </div>
            </Popover>

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
                    {(() => {
                      const q = nodeFilter.trim().toLowerCase();
                      const filtered = (types?.types ?? [])
                        .filter((t) => t.includes(q) || nodeLabel(t).toLowerCase().includes(q));
                      if (filtered.length === 0) {
                        return (
                          <div className="muted" style={{ padding: "8px 10px", fontSize: 12 }}>
                            no match
                          </div>
                        );
                      }
                      return filtered.map((t) => (
                        <button
                          key={t}
                          type="button"
                          className="palette__item"
                          onClick={() => addNode(t)}
                          title={`add a ${t} node`}
                        >
                          {nodeLabel(t)}
                          <span className="muted" style={{ fontSize: 10.5 }}>
                            {TERMINAL.has(t) ? "terminal" : "node"}
                          </span>
                        </button>
                      ));
                    })()}
                  </div>
                </Popover>
              </>
            )}
          </div>

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
          </>
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
        dirty={dirty}
        inCount={selectedNode ? edges.filter((e) => e.target === selectedNode.id).length : 0}
        outCount={selectedNode ? edges.filter((e) => e.source === selectedNode.id).length : 0}
        onLabel={(v) => selectedNode && setLabel(selectedNode.id, v)}
        onEdgeLabel={(v) => selectedEdge && setEdgeLabel(selectedEdge.id, v)}
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
        title="Import a Mermaid flowchart"
        width={520}
        footer={
          <>
            <Button
              variant="primary"
              onClick={runMermaidImport}
              loading={assistBusy}
              disabled={!assistText.trim()}
            >
              Import
            </Button>
            <Button variant="ghost" onClick={() => setAssist(null)} disabled={assistBusy}>
              Cancel
            </Button>
          </>
        }
      >
        <p className="muted" style={{ margin: "0 0 12px", fontSize: 12.5, lineHeight: 1.6 }}>
          Paste a flowchart. It replaces the canvas as an unsaved draft — node types are
          matched by label, edge labels become warnings to wire up, nothing saves until you
          hit Save draft.
        </p>
        <Textarea
          rows={14}
          value={assistText}
          autoFocus
          placeholder={"flowchart TD\n  R[retrieve] --> C[classify] --> D[draft]\n  D --> G{confidence gate}\n  G -->|pass| A[auto reply]\n  G -->|fail| H[ask human]"}
          onChange={(e) => setAssistText(e.target.value)}
        />
        {assistErr && <div className="err" style={{ fontSize: 12, marginTop: 8 }}>{assistErr}</div>}
      </SlideOver>

      <SlideOver
        open={testRunOpen}
        onClose={() => setTestRunOpen(false)}
        title="Test run"
        width={520}
        footer={
          <Button variant="secondary" onClick={() => setTestRunOpen(false)}>
            Close
          </Button>
        }
      >
        <RunPanel flowId={flowId} tenantId={flow?.tenant_id || ""} />
      </SlideOver>

      <SlideOver
        open={conditionsOpen}
        onClose={() => setConditionsOpen(false)}
        title="Conditions"
        width={520}
        footer={
          <Button variant="secondary" onClick={() => setConditionsOpen(false)}>
            Close
          </Button>
        }
      >
        <ConditionsOverview
          nodes={nodes}
          edges={edges}
          tenantId={flow?.tenant_id || ""}
          onCondition={setEdgeCond}
          onJumpTo={(edgeId) => {
            setConditionsOpen(false);
            setSelEdge(edgeId);
            setSelNode(null);
          }}
        />
      </SlideOver>

      <SlideOver
        open={helpOpen}
        onClose={() => setHelpOpen(false)}
        title="How to use this editor"
        width={380}
        footer={
          <Button variant="secondary" onClick={() => setHelpOpen(false)}>
            Close
          </Button>
        }
      >
        <EditorHelp />
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
