import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { RFNode } from "./graph";

export function NodeCard({ data, selected }: NodeProps<RFNode>) {
  return (
    <div className={`rf-node${selected ? " selected" : ""}${data.terminal ? " terminal" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <div className="t">{data.nodeType}</div>
      <div className="l">{data.label}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
