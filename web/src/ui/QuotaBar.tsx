/**
 * Usage against a plan quota. Fill is accent below 80%, warn 80–99%,
 * exception at 100%.
 */
export function QuotaBar({
  used,
  limit,
  label,
  format = (n) => n.toLocaleString(),
}: {
  used: number;
  limit: number;
  label: string;
  format?: (n: number) => string;
}) {
  const ratio = limit > 0 ? used / limit : 0;
  const tone = ratio >= 1 ? "exception" : ratio >= 0.8 ? "warn" : "accent";
  return (
    <div className="ui-quota">
      <div className="ui-quota__head">
        <span>{label}</span>
        <span className="ui-quota__num">
          {format(used)} / {format(limit)}
        </span>
      </div>
      <div className="ui-quota__track">
        <span
          className={"ui-quota__fill" + (tone === "accent" ? "" : ` ui-quota__fill--${tone}`)}
          style={{ width: `${Math.min(100, Math.max(0, ratio * 100))}%` }}
        />
      </div>
    </div>
  );
}
