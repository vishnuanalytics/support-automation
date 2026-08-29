import { useCallback, useEffect, useMemo, useState } from "react";
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
import type { Flow, NodeTypesResp } from "../types";
import { layout, toFlowPayload, toReactFlow, uuid, type RFEdge, type RFNode } from "./graph";
import { NodeCard } from "./NodeCard";
import { EdgeInspector, NodeInspector } from "./Inspector";
import { RunPanel } from "./RunPanel";

const TERMINAL = new Set(["auto_reply", "ask_human", "handover"]);

export function FlowEditor(props: {
  flowId: string;
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

function Inner({ flowId, onSaved, onDeleted }: {
  flowId: string;
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

  const nodeTypes = useMemo(() => ({ flowNode: NodeCard }), []);
  const mark = useCallback(() => setDirty(true), []);

  useEffect(() => {
    let alive = true;
    api.nodeTypes().then((t) => alive && setTypes(t));
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
      })
      .catch((e: ApiError) => setBanner({ kind: "err", text: e.message }));
    return () => {
      alive = false;
    };
  }, [flowId, setNodes, setEdges]);

  const onConnect = useCallback(
    (c: Connection) => {
      setEdges((es) =>
        addEdge(
          { id: uuid(), source: c.source!, target: c.target!, data: { condition: {} }, label: "" },
          es,
        ),
      );
      mark();
    },
    [setEdges, mark],
  );

  function addNode(t: string) {
    const id = uuid();
    setNodes((ns) => [
      ...ns,
      {
        id,
        type: "flowNode",
        position: { x: 60, y: 40 + ns.length * 16 },
        data: { label: t, nodeType: t, terminal: TERMINAL.has(t) },
      },
    ]);
    setConfigById((m) => ({ ...m, [id]: structuredClone(types?.defaults[t] ?? {}) }));
    mark();
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

  async function doSave() {
    setBusy(true);
    try {
      const f = await api.saveFlow(flowId, payload());
      setFlow(f);
      setDirty(false);
      setBanner({ kind: "ok", text: "saved" });
      onSaved();
    } catch (e) {
      const ae = e as ApiError;
      setBanner({ kind: "err", text: ae.errors ? "save blocked" : ae.message, list: ae.errors ?? undefined });
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
        <button
          className={`pill ${flow.status}`}
          onClick={() => {
            setFlow({ ...flow, status: flow.status === "published" ? "draft" : "published" });
            mark();
          }}
          title="toggle draft / published"
        >
          {flow.status}
        </button>
        <span className="muted">v{flow.version}</span>
        <div style={{ flex: 1 }} />
        {dirty && <span className="muted" title="unsaved changes">●</span>}
        <button onClick={() => setNodes((ns) => layout(ns, edges))}>Re-layout</button>
        <button onClick={doValidate} disabled={busy}>Validate</button>
        <button className="primary" onClick={doSave} disabled={busy || !dirty}>Save</button>
        <button className="err" onClick={doDelete}>Delete</button>
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
            onSelectionChange={({ nodes: sn, edges: se }) => {
              setSelNode(sn[0]?.id ?? null);
              setSelEdge(se[0]?.id ?? null);
            }}
            onNodeDragStop={mark}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>

          <div style={{ position: "absolute", left: 10, top: 10 }} className="row">
            {(types?.types ?? []).map((t) => (
              <button key={t} onClick={() => addNode(t)} title={`add ${t}`}>
                ＋ {t}
              </button>
            ))}
          </div>

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
    </>
  );
}
