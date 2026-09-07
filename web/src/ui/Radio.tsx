import type { ReactNode } from "react";

export function Radio({
  checked,
  onChange,
  name,
  value,
  disabled = false,
  children,
}: {
  checked: boolean;
  onChange: (value: string) => void;
  name: string;
  value: string;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <label className={"ui-radio" + (checked ? " ui-radio--on" : "")}>
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={() => onChange(value)}
      />
      <span className="ui-radio__dot" aria-hidden />
      {children}
    </label>
  );
}
