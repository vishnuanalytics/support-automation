import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from "@xyflow/react";
import type { RFEdge } from "./graph";

/**
 * A conditional edge always prints something — an unlabelled conditional
 * edge is a bug. A user-given name (edge.label, "VIP escalation") takes
 * over the pill's visible text when set; the raw condition expression
 * still decides the pass/fail color and always shows in the hover tooltip,
 * so naming an edge never hides what it actually does, just what you see
 * at a glance. No name -> exactly the old behaviour, the expression itself
 * is the pill text.
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
  const name = typeof data?.name === "string" ? data.name.trim() : "";
  const displayText = name || expr;
  const tooltip = name && expr ? `${name}\n${expr}` : name || expr;
  const kind = !expr ? "plain" : /\b(fail|else|not)\b|[<!]|>=?\s*0/i.test(expr) ? "fail" : "pass";

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {displayText && (
        <EdgeLabelRenderer>
          <div
            className={`edgelabel edgelabel--${kind}`}
            title={tooltip}
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: "all",
              // focus mode (FlowEditor's `displayEdges`) sets opacity on the
              // edge's own `style` to dim it — without this, the line would
              // fade but its label pill would stay at full strength.
              opacity: style?.opacity,
            }}
          >
            {displayText}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
