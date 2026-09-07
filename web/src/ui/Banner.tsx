import type { ReactNode } from "react";

/**
 * An in-view message with optional actions. 422 from save lists the
 * structural errors with links to the offending nodes; 409 explains the
 * reload in plain words. Never an alert().
 */
export function Banner({
  tone = "accent",
  title,
  detail,
  actions,
}: {
  tone?: "accent" | "warn" | "exception";
  title: ReactNode;
  detail?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className={`ui-banner ui-banner--${tone}`} role="status">
      <div className="ui-banner__title">{title}</div>
      {detail != null && <div className="ui-banner__detail">{detail}</div>}
      {actions != null && <div className="ui-banner__actions">{actions}</div>}
    </div>
  );
}
