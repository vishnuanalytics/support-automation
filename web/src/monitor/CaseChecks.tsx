import { Tag } from "../ui";
import { checkTable, formatTime } from "./util";
import type { CaseCheck } from "./types";

function params(p: Record<string, unknown>): string {
  return Object.entries(p).map(([k, v]) => `${k} ${String(v)}`).join(" · ");
}

function Rows({ rows }: { rows: unknown[] }) {
  const { head, body } = checkTable(rows);
  return (
    <div className="monitor-check-rows">
      <table>
        {head.length > 0 && (
          <thead><tr>{head.map((h) => <th key={h} scope="col">{h}</th>)}</tr></thead>
        )}
        <tbody>
          {body.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j}>{v}</td>)}</tr>)}
        </tbody>
      </table>
    </div>
  );
}

export function CaseChecks({ checks }: { checks: CaseCheck[] }) {
  if (checks.length === 0) {
    return (
      <p className="monitor-muted">
        No saved checks yet. Use "Ask Claude" above: checks Claude runs on this case are saved here.
      </p>
    );
  }
  return (
    <>
      {checks.map((c) => {
        const kept = c.rows?.length ?? 0;
        return (
          <article key={c.id} className="monitor-card">
            <h3>{c.check_name} {c.row_count === 0 && <Tag>no rows</Tag>}</h3>
            {c.summary && <p>{c.summary}</p>}
            <p className="monitor-muted">
              {Object.keys(c.params || {}).length > 0 && <>{params(c.params)} · </>}
              {c.saved_by || "Unknown"} · {formatTime(c.created_at)}
            </p>
            {kept > 0 && (
              <details>
                <summary>
                  {c.row_count != null && c.row_count > kept
                    ? `First ${kept} of ${c.row_count} rows` : `${kept} row${kept === 1 ? "" : "s"}`}
                </summary>
                <Rows rows={c.rows} />
              </details>
            )}
          </article>
        );
      })}
    </>
  );
}
