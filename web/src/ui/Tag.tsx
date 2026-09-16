import type { ReactNode } from "react";

export type TagTone = "accent" | "success" | "warn" | "warn-soft" | "exception" | "neutral";

/**
 * Outcome / tier / status chip. Tone is a function of the *value*, never of
 * the view — the same run reads the same everywhere. `valueToTone` maps the
 * interpreter's outcome strings; status dots carry a text label too, colour
 * is never the only signal. `accent` is the neutral/default/"featured"
 * treatment (it's the monochrome primary colour) — `success` is the actual
 * positive-outcome tone, so the two don't collide now that accent no longer
 * doubles as "good."
 */
const VALUE_TONE: Record<string, TagTone> = {
  auto_reply: "success",
  ask_human: "warn",
  need_info: "warn-soft",
  handover: "exception",
  published: "success",
  draft: "neutral",
};

export function valueToTone(value: string): TagTone {
  return VALUE_TONE[value] ?? "neutral";
}

export function Tag({
  children,
  tone = "neutral",
  outline = false,
  dot = false,
}: {
  children: ReactNode;
  tone?: TagTone;
  outline?: boolean;
  dot?: boolean;
}) {
  const cls = ["ui-tag", `ui-tag--${tone}`, outline && "ui-tag--outline"]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={cls}>
      {dot && <span className="ui-tag__dot" aria-hidden />}
      {children}
    </span>
  );
}
