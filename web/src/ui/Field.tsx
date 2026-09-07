import { useId, type ReactElement, type ReactNode, cloneElement } from "react";

/**
 * Label + control + hint/error. Labels are 12px/600 muted, never
 * placeholders. Error text sits under the control in exception-text and
 * clears the hint. The child control is cloned with `id`, `aria-invalid`
 * and `aria-describedby` wired to the label/message.
 */
export function Field({
  label,
  error,
  hint,
  disabled = false,
  children,
}: {
  label?: ReactNode;
  error?: string | null;
  hint?: ReactNode;
  disabled?: boolean;
  children: ReactElement;
}) {
  const id = useId();
  const msgId = `${id}-msg`;
  const cls = ["ui-field", disabled && "ui-field--disabled", error && "ui-field--invalid"]
    .filter(Boolean)
    .join(" ");
  const control = cloneElement(children, {
    id: children.props.id ?? id,
    disabled: children.props.disabled ?? disabled,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": error || hint ? msgId : undefined,
  });
  return (
    <div className={cls}>
      {label != null && (
        <label className="ui-field__label" htmlFor={id}>
          {label}
        </label>
      )}
      {control}
      {error ? (
        <div className="ui-field__error" id={msgId}>
          {error}
        </div>
      ) : hint != null ? (
        <div className="ui-field__hint" id={msgId}>
          {hint}
        </div>
      ) : null}
    </div>
  );
}
