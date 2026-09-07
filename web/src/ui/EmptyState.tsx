import type { ReactNode } from "react";

/** Always name the next action. */
export function EmptyState({
  title,
  body,
  action,
}: {
  title: ReactNode;
  body?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="ui-empty">
      <div className="ui-empty__title">{title}</div>
      {body != null && <div className="ui-empty__body">{body}</div>}
      {action != null && <div>{action}</div>}
    </div>
  );
}
