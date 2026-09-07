import type { SelectHTMLAttributes } from "react";

type Option = { value: string; label: string };

type SelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, "children"> & {
  options: Option[];
  placeholder?: string;
};

export function Select({ options, placeholder, className, ...rest }: SelectProps) {
  return (
    <span className="ui-select-wrap">
      <select className={["ui-select", className].filter(Boolean).join(" ")} {...rest}>
        {placeholder != null && (
          <option value="" disabled>
            {placeholder}
          </option>
        )}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </span>
  );
}
