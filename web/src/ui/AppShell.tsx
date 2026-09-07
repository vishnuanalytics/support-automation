import type { ReactNode } from "react";

/**
 * The one frame every view sits in: 240px sidebar + 44px toolbar + body.
 *
 * Law 1 — one frame, one scale: views render into `children` and never set
 * their own width, padding or font size.
 * Law 2 — scroll belongs to the pane: the shell itself never scrolls; the
 * body clips, and each pane inside it owns `min-height:0` + `overflow-y:auto`.
 */
export function AppShell({
  sidebar,
  toolbar,
  children,
}: {
  sidebar: ReactNode;
  toolbar?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="app-shell">
      {sidebar}
      <div className="app-main">
        {toolbar != null && <div className="app-toolbar">{toolbar}</div>}
        <div className="app-body">{children}</div>
      </div>
    </div>
  );
}
