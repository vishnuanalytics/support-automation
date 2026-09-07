import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "icon";
type Size = "md" | "sm";

export type ButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className"> & {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  children?: ReactNode;
};

/**
 * One primary per toolbar (the publishing action). Icon buttons need a
 * `title` + `aria-label`. Min height 26px (sm) / 32px (md). Forwards its ref
 * so overlays can anchor to it.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading = false, disabled, children, type = "button", ...rest },
  ref,
) {
  const cls = ["ui-btn", `ui-btn--${variant}`, size === "sm" && "ui-btn--sm"]
    .filter(Boolean)
    .join(" ");
  return (
    <button ref={ref} type={type} className={cls} disabled={disabled || loading} {...rest}>
      {loading && <span className="ui-btn__spinner" aria-hidden />}
      {children}
    </button>
  );
});
