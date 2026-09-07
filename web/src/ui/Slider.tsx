/**
 * The friendly form of a per-tier threshold — a labelled track with a value
 * readout, not raw JSON. `value` and bounds are 0..1 unless overridden.
 */
export function Slider({
  label,
  value,
  onChange,
  min = 0,
  max = 1,
  step = 0.01,
  format = (v) => v.toFixed(2),
  disabled = false,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  format?: (value: number) => string;
  disabled?: boolean;
}) {
  const pct = max === min ? 0 : ((value - min) / (max - min)) * 100;
  return (
    <div className="ui-slider">
      <span className="ui-slider__label">{label}</span>
      <span className="ui-slider__track">
        <span className="ui-slider__fill" style={{ width: `${pct}%` }} />
        <span className="ui-slider__thumb" style={{ left: `${pct}%` }} />
        <input
          className="ui-slider__range"
          type="range"
          aria-label={label}
          min={min}
          max={max}
          step={step}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(Number(e.target.value))}
        />
      </span>
      <span className="ui-slider__value">{format(value)}</span>
    </div>
  );
}
