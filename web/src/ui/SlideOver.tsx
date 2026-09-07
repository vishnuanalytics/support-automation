import { useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useOverlay } from "./overlay";

export type SlideOverTab = { key: string; label: string; content: ReactNode };

/**
 * Right-hand panel that replaces the cramped inspector — node config, run
 * detail, connector setup, document preview. 380 / 520 / 720px by content.
 * Header and footer are fixed; the body scrolls. Esc + backdrop close, focus
 * trapped, focus returned to the trigger.
 */
export function SlideOver({
  open,
  onClose,
  title,
  width = 520,
  tabs,
  footer,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  width?: 380 | 520 | 720;
  tabs?: SlideOverTab[];
  footer?: ReactNode;
  children?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(0);
  useOverlay(open, onClose, ref);

  if (!open) return null;
  const current = tabs && tabs[active];
  return createPortal(
    <>
      <div className="ui-scrim" onClick={onClose} />
      <div
        ref={ref}
        className="ui-slideover"
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        style={{ width }}
      >
        <div className="ui-slideover__head">
          <span className="ui-slideover__title">{title}</span>
          <button type="button" className="ui-slideover__close" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        {tabs && tabs.length > 0 && (
          <div className="ui-slideover__tabs" role="tablist">
            {tabs.map((t, i) => (
              <button
                key={t.key}
                type="button"
                role="tab"
                aria-selected={i === active}
                className={"ui-slideover__tab" + (i === active ? " ui-slideover__tab--on" : "")}
                onClick={() => setActive(i)}
              >
                {t.label}
              </button>
            ))}
          </div>
        )}
        <div className="ui-slideover__body">{current ? current.content : children}</div>
        {footer != null && <div className="ui-slideover__foot">{footer}</div>}
      </div>
    </>,
    document.body,
  );
}
