import type { ReactNode } from "react";

export type Column<Row> = {
  key: string;
  header: ReactNode;
  align?: "left" | "right";
  cell: (row: Row) => ReactNode;
};

/**
 * Sticky `thead` inside the scrolling pane, 40px rows, selected row keeps a
 * 1px accent outline. Row click fills the detail rail — it never navigates.
 */
export function DataTable<Row>({
  columns,
  rows,
  rowId,
  selectedId = null,
  onSelect,
  empty,
}: {
  columns: Column<Row>[];
  rows: Row[];
  rowId: (row: Row) => string;
  selectedId?: string | null;
  onSelect?: (row: Row) => void;
  empty?: ReactNode;
}) {
  if (rows.length === 0 && empty != null) {
    return <div className="ui-table-wrap"><div className="ui-table__empty">{empty}</div></div>;
  }
  return (
    <div className="ui-table-wrap">
      <table className="ui-table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.align === "right" ? "ui-num" : undefined}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const id = rowId(row);
            return (
              <tr
                key={id}
                className={id === selectedId ? "ui-row--sel" : undefined}
                aria-selected={id === selectedId || undefined}
                onClick={onSelect ? () => onSelect(row) : undefined}
              >
                {columns.map((c) => (
                  <td key={c.key} className={c.align === "right" ? "ui-num" : undefined}>
                    {c.cell(row)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
