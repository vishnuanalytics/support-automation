import type { ReactNode } from "react";

/**
 * The 240px left rail: a fixed head (workspace switcher), a scrolling middle
 * (nav + any view-specific list), and a pinned foot (account row).
 *
 * Structural only — callers supply the content and keep their own state.
 * Active nav styling lives in `.nav-item.active` (accent wash + 2px left rule).
 */
export function Sidebar({
  head,
  children,
  foot,
}: {
  head?: ReactNode;
  children: ReactNode;
  foot?: ReactNode;
}) {
  return (
    <aside className="app-sidebar">
      {head != null && <div className="app-sidebar-head">{head}</div>}
      <div className="app-sidebar-scroll">{children}</div>
      {foot != null && <div className="app-sidebar-foot">{foot}</div>}
    </aside>
  );
}
