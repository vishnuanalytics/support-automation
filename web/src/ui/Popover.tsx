import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { useOverlay, useOutsideClick } from "./overlay";

/**
 * A small, anchored, non-blocking choice next to its trigger — pickers, row
 * menus, workspace switch, the node-type palette. ≤280px, no backdrop dim,
 * Esc + outside click close, focus returns to the trigger.
 */
export function Popover({
  anchorRef,
  open,
  onClose,
  width = 236,
  align = "start",
  children,
}: {
  anchorRef: RefObject<HTMLElement | null>;
  open: boolean;
  onClose: () => void;
  width?: number;
  align?: "start" | "end";
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  useLayoutEffect(() => {
    if (!open || !anchorRef.current) return;
    const r = anchorRef.current.getBoundingClientRect();
    const left = align === "end" ? r.right - width : r.left;
    setPos({
      top: Math.round(r.bottom + 6),
      left: Math.round(Math.max(8, Math.min(left, window.innerWidth - width - 8))),
    });
  }, [open, anchorRef, width, align]);

  useOverlay(open, onClose, ref);
  useOutsideClick(open, ref, onClose, anchorRef);

  if (!open || !pos) return null;
  return createPortal(
    <div
      ref={ref}
      className="ui-popover"
      role="dialog"
      style={{ top: pos.top, left: pos.left, width: Math.min(width, 280) }}
    >
      {children}
    </div>,
    document.body,
  );
}
