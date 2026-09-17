import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { RFNode } from "./graph";
import { nodeKind } from "./nodeSummary";

/**
 * A node on the canvas. The left rule carries the kind — cyan for work,
 * yellow for a terminal, magenta for an unknown type. Line 2 is the one
 * config value that decides the node's behaviour (see `summarize`).
 */
export function NodeCard({ data, selected }: NodeProps<RFNode>) {
  const kind = nodeKind(data);
  const kindLabel = data.invalid ? "invalid" : data.tooManyDefaults ? "won't build" : data.nodeType;
  return (
    <div
      className={`nodecard nodecard--${kind}${selected ? " is-selected" : ""}${data.dimmed ? " nodecard--dimmed" : ""}`}
    >
      <Handle type="target" position={Position.Left} />
      <div className="nodecard__kind">{kindLabel}</div>
      <div className="nodecard__label">{data.label}</div>
      {data.invalid ? (
        <div className="nodecard__summary nodecard__summary--bad">unknown node type</div>
      ) : data.tooManyDefaults ? (
        <div className="nodecard__summary nodecard__summary--bad">
          more than one unconditional outgoing edge — give all but one a condition
        </div>
      ) : (
        data.summary && <div className="nodecard__summary">{data.summary}</div>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
