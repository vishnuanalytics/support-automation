import { SlideOver, Button, JsonEditor, type SlideOverTab } from "../ui";
import type { RFEdge, RFNode } from "./graph";
import { EdgeInspector, NodeInspector } from "./Inspector";
import { RunPanel } from "./RunPanel";

/**
 * The inspector as a right-hand slide-over (Screen 02): Config / JSON /
 * Recent-runs tabs, a sticky footer with Revert + Delete, and the canvas at
 * full width behind it. Edits apply live — the real commit is "Save draft" in
 * the toolbar — so the footer reflects the unsaved state rather than staging.
 */
export function InspectorPanel({
  node,
  edge,
  config,
  tenantId,
  flowId,
  dirty,
  inCount,
  outCount,
  onLabel,
  onConfig,
  onCondition,
  onRevert,
  onDeleteNode,
  onDeleteEdge,
  onClose,
}: {
  node: RFNode | null;
  edge: RFEdge | null;
  config: Record<string, unknown>;
  tenantId: string;
  flowId: string;
  dirty: boolean;
  inCount: number;
  outCount: number;
  onLabel: (v: string) => void;
  onConfig: (v: Record<string, unknown>) => void;
  onCondition: (c: Record<string, unknown>) => void;
  onRevert: () => void;
  onDeleteNode: () => void;
  onDeleteEdge: () => void;
  onClose: () => void;
}) {
  const open = !!(node || edge);

  const tabs: SlideOverTab[] = node
    ? [
        {
          key: "config",
          label: "Config",
          content: (
            <NodeInspector
              embedded
              node={node}
              config={config}
              tenantId={tenantId}
              onLabel={onLabel}
              onConfig={onConfig}
              onDelete={onDeleteNode}
            />
          ),
        },
        {
          key: "json",
          label: "JSON",
          content: <JsonEditor value={config} onChange={onConfig} />,
        },
        {
          key: "runs",
          label: "Recent runs",
          content: <RunPanel flowId={flowId} />,
        },
      ]
    : edge
      ? [
          {
            key: "config",
            label: "Condition",
            content: (
              <EdgeInspector
                embedded
                edge={edge}
                tenantId={tenantId}
                onCondition={onCondition}
                onDelete={onDeleteEdge}
              />
            ),
          },
          {
            key: "json",
            label: "JSON",
            content: (
              <JsonEditor
                value={(edge.data?.condition as Record<string, unknown>) ?? {}}
                onChange={onCondition}
              />
            ),
          },
        ]
      : [];

  const title = node ? (
    <span>
      <span className="nodecard__kind" style={{ display: "block" }}>
        {node.data.invalid ? "invalid" : node.data.nodeType}
      </span>
      {node.data.label}
      <span className="muted" style={{ display: "block", font: "var(--type-mono)", marginTop: 2 }}>
        {inCount} in · {outCount} out
      </span>
    </span>
  ) : (
    <span>Edge condition</span>
  );

  return (
    <SlideOver
      open={open}
      onClose={onClose}
      title={title}
      width={380}
      tabs={tabs}
      footer={
        <>
          <Button
            variant="danger"
            size="sm"
            onClick={node ? onDeleteNode : onDeleteEdge}
          >
            Delete {node ? "node" : "edge"}
          </Button>
          <Button variant="ghost" size="sm" onClick={onRevert}>
            Revert
          </Button>
          <span className="muted" style={{ marginLeft: "auto", font: "var(--type-mono)" }}>
            {dirty ? "unsaved" : "saved"}
          </span>
          <Button variant="secondary" size="sm" onClick={onClose}>
            Done
          </Button>
        </>
      }
    />
  );
}
