import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from "@xyflow/react";
import type { RFEdge } from "./graph";

/**
 * A conditional edge always prints its expression — an unlabelled
 * conditional edge is a bug. `pass` reads cyan, `fail`/`else` magenta.
 */
export function EdgeLabel({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  style,
  data,
  label,
}: EdgeProps<RFEdge>) {
  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });
  const expr =
    (data?.condition as { if?: string } | undefined)?.if ??
    (typeof label === "string" ? label : "");
  const kind = !expr ? "plain" : /\b(fail|else|not)\b|[<!]|>=?\s*0/i.test(expr) ? "fail" : "pass";

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {expr && (
        <EdgeLabelRenderer>
          <div
            className={`edgelabel edgelabel--${kind}`}
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: "all",
            }}
          >
            {expr}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
