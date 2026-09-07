import { useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Button } from "./Button";
import { useOverlay } from "./overlay";

export type DialogAction = {
  label: string;
  onClick: () => void;
  variant?: "primary" | "secondary" | "ghost" | "danger";
};

/**
 * A blocking decision only — publish, delete, invite, add source, create
 * workspace. ≤460px, 60% backdrop, affirmative action last. Replaces every
 * prompt() / confirm() / alert() in the app.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
  actions = [],
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children?: ReactNode;
  actions?: DialogAction[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  useOverlay(open, onClose, ref);
  if (!open) return null;
  return createPortal(
    <div className="ui-scrim ui-scrim--dialog" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={ref}
        className="ui-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
      >
        <div className="ui-dialog__title">{title}</div>
        {children != null && <div className="ui-dialog__body">{children}</div>}
        {actions.length > 0 && (
          <div className="ui-dialog__actions">
            {actions.map((a) => (
              <Button key={a.label} variant={a.variant ?? "secondary"} onClick={a.onClick}>
                {a.label}
              </Button>
            ))}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
