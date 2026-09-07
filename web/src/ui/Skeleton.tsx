/** Placeholder shapes while data loads — never a spinner over a whole pane. */
export function Skeleton({
  lines = 3,
  variant = "text",
}: {
  lines?: number;
  variant?: "text" | "row" | "card";
}) {
  return (
    <div aria-hidden>
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          className={`ui-skel ui-skel--${variant}`}
          style={variant === "text" && i === lines - 1 ? { width: "60%" } : undefined}
        />
      ))}
    </div>
  );
}
