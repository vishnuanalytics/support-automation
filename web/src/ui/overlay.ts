import { useEffect, type RefObject } from "react";

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/**
 * Shared overlay behaviour: Esc closes, focus is trapped inside `ref` while
 * open, and focus returns to whatever had it when the overlay opened. Every
 * overlay kind (popover, slide-over, dialog) uses this.
 */
export function useOverlay(
  open: boolean,
  onClose: () => void,
  ref: RefObject<HTMLElement | null>,
) {
  useEffect(() => {
    if (!open) return;
    const trigger = document.activeElement as HTMLElement | null;
    const node = ref.current;

    // move focus into the overlay
    const first = node?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? node)?.focus();

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !node) return;
      const items = Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) return;
      const firstItem = items[0];
      const lastItem = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && active === firstItem) {
        e.preventDefault();
        lastItem.focus();
      } else if (!e.shiftKey && active === lastItem) {
        e.preventDefault();
        firstItem.focus();
      }
    }

    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      // restore focus to the trigger if it's still in the document
      if (trigger && document.contains(trigger)) trigger.focus();
    };
  }, [open, onClose, ref]);
}

/**
 * Fire `onOutside` on a pointer press outside `ref` while `active`. A press on
 * `ignoreRef` (typically the trigger) is not "outside" — so re-clicking the
 * trigger toggles rather than close-then-reopen.
 */
export function useOutsideClick(
  active: boolean,
  ref: RefObject<HTMLElement | null>,
  onOutside: () => void,
  ignoreRef?: RefObject<HTMLElement | null>,
) {
  useEffect(() => {
    if (!active) return;
    function onDown(e: MouseEvent) {
      const target = e.target as Node;
      if (ref.current?.contains(target)) return;
      if (ignoreRef?.current?.contains(target)) return;
      onOutside();
    }
    document.addEventListener("mousedown", onDown, true);
    return () => document.removeEventListener("mousedown", onDown, true);
  }, [active, ref, onOutside, ignoreRef]);
}
