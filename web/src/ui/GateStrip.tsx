import { Tag } from "./Tag";

/**
 * Why a run went the way it did, read as arithmetic:
 *   retrieval · draft → score  vs  threshold  → PASS / FAIL
 * The bar marks the threshold with a 2px tick.
 */
export function GateStrip({
  retrieval,
  draft,
  score,
  threshold,
  passed,
}: {
  retrieval: number;
  draft: number;
  score: number;
  threshold: number;
  passed: boolean;
}) {
  const f = (n: number) => n.toFixed(2);
  return (
    <div className="ui-gate">
      <div className="ui-gate__row">
        <span>
          retrieval <span className="ui-gate__num">{f(retrieval)}</span>
        </span>
        <span className="ui-gate__vs">·</span>
        <span>
          draft <span className="ui-gate__num">{f(draft)}</span>
        </span>
        <span className="ui-gate__arrow">→</span>
        <span>
          score <span className="ui-gate__num">{f(score)}</span>
        </span>
        <span className="ui-gate__vs">vs</span>
        <span>{f(threshold)}</span>
        <Tag tone={passed ? "accent" : "warn"}>{passed ? "PASS" : "FAIL"}</Tag>
      </div>
      <div className="ui-gate__track">
        <span
          className={"ui-gate__fill" + (passed ? "" : " ui-gate__fill--fail")}
          style={{ width: `${Math.min(100, Math.max(0, score * 100))}%` }}
        />
        <span className="ui-gate__tick" style={{ left: `${Math.min(100, Math.max(0, threshold * 100))}%` }} />
      </div>
    </div>
  );
}
