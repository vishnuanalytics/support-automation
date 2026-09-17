import { useCaseMeta } from "./Inspector";
import { ConditionBuilder } from "./ConditionBuilder";
import { parseCondition, serializeCondition } from "./conditionBuilder";
import type { RFEdge, RFNode } from "./graph";

/**
 * Every conditional edge in the flow, in one place, grouped by the node
 * they branch from — so auditing "what does this flow actually route on"
 * doesn't mean clicking every edge one at a time. Reuses the same row
 * builder as a single edge's inspector; a "too complex for the row
 * builder" (or/not/nested) condition shows its raw expression instead,
 * with a link to open that edge's own Advanced box on the canvas rather
 * than duplicating the raw-textarea-plus-quick-insert UI here too.
 *
 * Unconditional (default/fallback) edges aren't shown — this view is about
 * branching logic specifically, not every edge in the flow.
 */
export function ConditionsOverview({
  nodes,
  edges,
  tenantId,
  onCondition,
  onJumpTo,
}: {
  nodes: RFNode[];
  edges: RFEdge[];
  tenantId: string;
  onCondition: (edgeId: string, cond: Record<string, unknown>) => void;
  onJumpTo: (edgeId: string) => void;
}) {
  const sfMeta = useCaseMeta(tenantId);
  const labelOf = (id: string) => nodes.find((n) => n.id === id)?.data.label ?? id;

  const conditional = edges.filter((e) => !!(e.data?.condition as { if?: string } | undefined)?.if);
  const bySource = new Map<string, RFEdge[]>();
  for (const e of conditional) {
    const group = bySource.get(e.source);
    if (group) group.push(e);
    else bySource.set(e.source, [e]);
  }

  if (conditional.length === 0) {
    return (
      <div className="muted" style={{ fontSize: 12.5 }}>
        No conditional edges yet — every path in this flow is unconditional (a
        node with one outgoing edge always takes it; a node with several
        needs a condition on all but one to actually branch).
      </div>
    );
  }

  return (
    <div className="col" style={{ gap: 16 }}>
      {[...bySource.entries()].map(([sourceId, group]) => (
        <div key={sourceId}>
          <div style={{ font: "600 13px/1.4 var(--font-body)", marginBottom: 6 }}>
            {labelOf(sourceId)}
            <span className="muted" style={{ fontWeight: 400, marginLeft: 6 }}>
              branches {group.length} way{group.length === 1 ? "" : "s"}
            </span>
          </div>
          {group.map((e) => {
            const ifExpr = (e.data?.condition as { if?: string }).if ?? "";
            const clauses = parseCondition(ifExpr);
            const name = typeof e.data?.name === "string" ? e.data.name.trim() : "";
            return (
              <div key={e.id} className="condbuilder__group">
                <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>
                  {name && <strong style={{ color: "var(--text)" }}>{name}</strong>}
                  {name && " — "}
                  → {labelOf(e.target)}
                </div>
                {clauses !== null ? (
                  <ConditionBuilder
                    clauses={clauses}
                    onChange={(next) => onCondition(e.id, { if: serializeCondition(next) })}
                    sfMeta={sfMeta}
                  />
                ) : (
                  <div className="row" style={{ gap: 8, alignItems: "flex-start" }}>
                    <code className="condbuilder__raw">{ifExpr}</code>
                    <button type="button" style={{ width: "auto", flex: "none" }} onClick={() => onJumpTo(e.id)}>
                      Edit on canvas »
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}
