import type { ReactNode } from "react";

export function StatTile({
  value,
  label,
  tone = "neutral",
}: {
  value: ReactNode;
  label: ReactNode;
  tone?: "neutral" | "accent" | "warn" | "exception";
}) {
  return (
    <div className={"ui-tile" + (tone === "neutral" ? "" : ` ui-tile--${tone}`)}>
      <div className="ui-tile__v">{value}</div>
      <div className="ui-tile__l">{label}</div>
    </div>
  );
}
