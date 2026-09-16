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
  const [pos, setPos] = useState<
    { left: number; maxHeight: number } & ({ top: number; bottom?: undefined } | { top?: undefined; bottom: number })
  >();

  useLayoutEffect(() => {
    if (!open || !anchorRef.current) return;
    const r = anchorRef.current.getBoundingClientRect();
    const left = align === "end" ? r.right - width : r.left;
    const spaceBelow = window.innerHeight - r.bottom - 6 - 8;
    const spaceAbove = r.top - 6 - 8;
    // Content taller than a popover's content ever used to be (the
    // date-range picker's preset list + custom range) can run past the
    // bottom of the viewport with no way to reach the rest of it — open
    // upward instead when there's genuinely more room there (anchored by
    // `bottom`, not a computed `top`, so it grows from the anchor without
    // needing to know its own height up front), and always cap height +
    // scroll internally as a backstop either way.
    const openUpward = spaceBelow < 160 && spaceAbove > spaceBelow;
    const clampedLeft = Math.round(Math.max(8, Math.min(left, window.innerWidth - width - 8)));
    setPos(
      openUpward
        ? { bottom: Math.round(window.innerHeight - r.top + 6), left: clampedLeft, maxHeight: Math.max(120, Math.round(spaceAbove)) }
        : { top: Math.round(r.bottom + 6), left: clampedLeft, maxHeight: Math.max(120, Math.round(spaceBelow)) },
    );
  }, [open, anchorRef, width, align]);

  useOverlay(open, onClose, ref);
  useOutsideClick(open, ref, onClose, anchorRef);

  if (!open || !pos) return null;
  return createPortal(
    <div
      ref={ref}
      className="ui-popover"
      role="dialog"
      style={{
        top: pos.top,
        bottom: pos.bottom,
        left: pos.left,
        width: Math.min(width, 280),
        maxHeight: pos.maxHeight,
        overflowY: "auto",
      }}
    >
      {children}
    </div>,
    document.body,
  );
}
