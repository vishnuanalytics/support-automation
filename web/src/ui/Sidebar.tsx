import { useEffect, useRef, type ReactNode } from "react";
import { ChevronsLeft, ChevronsRight } from "lucide-react";

const WIDTH_KEY = "sidebar-width";
const MIN_WIDTH = 200;
const MAX_WIDTH = 420;
const DEFAULT_WIDTH = 260;
const COLLAPSED_WIDTH = 60;

function loadWidth(): number {
  const raw = typeof window !== "undefined" ? Number(window.localStorage.getItem(WIDTH_KEY)) : NaN;
  if (!Number.isFinite(raw) || raw <= 0) return DEFAULT_WIDTH;
  return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, raw));
}

function applyWidth(px: number) {
  document.documentElement.style.setProperty("--sidebar-w", `${px}px`);
}

/**
 * The left rail: a fixed head (workspace switcher), a scrolling middle (nav
 * + any view-specific list), and a pinned foot (account row).
 *
 * Collapse is controlled by the caller (App.tsx needs to know it too, to
 * force nav groups open and hide labels) — width/resize is self-contained
 * here since nothing outside the rail cares about the exact pixel value.
 * The resize handle updates `--sidebar-w` directly on `<html>` during drag
 * (no React re-render per pixel) and only commits to state/localStorage on
 * mouseup; collapsing snaps `--sidebar-w` to a fixed rail width instead.
 *
 * Active nav styling lives in `.nav-item.active` (soft neutral highlight).
 */
export function Sidebar({
  head,
  children,
  foot,
  collapsed,
  onToggleCollapsed,
}: {
  head?: ReactNode;
  children: ReactNode;
  foot?: ReactNode;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}) {
  const widthRef = useRef(DEFAULT_WIDTH);
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);

  useEffect(() => {
    widthRef.current = loadWidth();
    if (!collapsed) applyWidth(widthRef.current);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    applyWidth(collapsed ? COLLAPSED_WIDTH : widthRef.current);
  }, [collapsed]);

  function onResizeStart(e: React.MouseEvent) {
    if (collapsed) return;
    e.preventDefault();
    dragRef.current = { startX: e.clientX, startWidth: widthRef.current };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.documentElement.classList.add("is-resizing-sidebar");

    const onMove = (ev: MouseEvent) => {
      if (!dragRef.current) return;
      const next = Math.min(
        MAX_WIDTH,
        Math.max(MIN_WIDTH, dragRef.current.startWidth + (ev.clientX - dragRef.current.startX)),
      );
      applyWidth(next);
    };
    const onUp = (ev: MouseEvent) => {
      if (dragRef.current) {
        const next = Math.min(
          MAX_WIDTH,
          Math.max(MIN_WIDTH, dragRef.current.startWidth + (ev.clientX - dragRef.current.startX)),
        );
        widthRef.current = next;
        window.localStorage.setItem(WIDTH_KEY, String(next));
      }
      dragRef.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.documentElement.classList.remove("is-resizing-sidebar");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  return (
    <aside className={"app-sidebar" + (collapsed ? " app-sidebar--collapsed" : "")}>
      <div className="app-sidebar-head app-sidebar-head--row">
        {head != null && <div className="app-sidebar-head-content">{head}</div>}
        <button
          type="button"
          className="sidebar-collapse-btn"
          onClick={onToggleCollapsed}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? <ChevronsRight size={15} /> : <ChevronsLeft size={15} />}
        </button>
      </div>
      <div className="app-sidebar-scroll">{children}</div>
      {foot != null && <div className="app-sidebar-foot">{foot}</div>}

      {!collapsed && (
        <div
          className="sidebar-resize-handle"
          onMouseDown={onResizeStart}
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize sidebar"
        />
      )}
    </aside>
  );
}
