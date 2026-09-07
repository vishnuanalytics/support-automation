import type { ReactNode } from "react";

/**
 * The 44px bar at the top of a view's body: a title, optional mono meta, and
 * right-aligned actions. One primary button per toolbar (the publishing
 * action). Presentational only — no layout of its own beyond the bar.
 */
export function Toolbar({
  title,
  meta,
  children,
}: {
  title: ReactNode;
  meta?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <>
      <span className="app-toolbar-title">{title}</span>
      {meta != null && <span className="app-toolbar-meta">{meta}</span>}
      {children != null && <span className="app-toolbar-actions">{children}</span>}
    </>
  );
}
