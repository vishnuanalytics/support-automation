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
  return (
    <div className={`nodecard nodecard--${kind}${selected ? " is-selected" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <div className="nodecard__kind">{data.invalid ? "invalid" : data.nodeType}</div>
      <div className="nodecard__label">{data.label}</div>
      {data.invalid ? (
        <div className="nodecard__summary nodecard__summary--bad">unknown node type</div>
      ) : (
        data.summary && <div className="nodecard__summary">{data.summary}</div>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
