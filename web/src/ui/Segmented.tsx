type Option<T extends string> = { value: T; label: string };

/**
 * A small mutually-exclusive choice rendered inline — run filters, form/JSON
 * switch, on-fail routing. Single row, no wrap.
 */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  disabled = false,
  "aria-label": ariaLabel,
}: {
  options: Option<T>[];
  value: T;
  onChange: (value: T) => void;
  disabled?: boolean;
  "aria-label"?: string;
}) {
  return (
    <div className="ui-segmented" role="radiogroup" aria-label={ariaLabel}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          className={"ui-segmented__opt" + (o.value === value ? " ui-segmented__opt--on" : "")}
          disabled={disabled}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
