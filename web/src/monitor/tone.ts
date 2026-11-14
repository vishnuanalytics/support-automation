import type { TagTone } from "../ui";
import type { QueueCase } from "./types";

/** How many days without a reply puts a case on the aggregator and engineering pages. */
export const QUIET_DAYS = 3;

export type Party = "customer" | "third_party" | "engineering" | "us" | "none";

/** Who spoke last on a case. */
export function lastParty(c: QueueCase): Party {
  const p = c.latest?.party;
  if (!c.latest) return "none";
  if (p === "customer" || p === "third_party" || p === "engineering") return p;
  return "us";
}

const TONES: Record<string, { label: string; tone: TagTone }> = {
  needs_reply: { label: "Needs a reply", tone: "warn" },
  confirmed: { label: "Confirmed fixed", tone: "success" },
  thanks: { label: "Thanks only", tone: "neutral" },
  solution: { label: "Solution shared", tone: "success" },
  needs_info: { label: "Asks for details", tone: "warn" },
  bug_raised: { label: "Raised as a bug", tone: "neutral" },
  update: { label: "Real update", tone: "neutral" },
  no_answer: { label: "No real answer", tone: "exception" },
  ack: { label: "Ack only", tone: "neutral" },
};

/** What the last reply says, from the labels the sync already gave it. */
export function replyTone(c: QueueCase): string | null {
  const party = lastParty(c);
  if (party === "customer") return c.customer_reply_kind && TONES[c.customer_reply_kind] ? c.customer_reply_kind : null;
  if (party === "third_party") {
    if (c.signal?.code === "third_party_ack") return "ack";
    return c.reply_kind && TONES[c.reply_kind] ? c.reply_kind : null;
  }
  return null;
}

export function toneLabel(kind: string): string {
  return TONES[kind]?.label ?? kind;
}

export function toneTag(kind: string): TagTone {
  return TONES[kind]?.tone ?? "neutral";
}

export const TONE_ORDER = Object.keys(TONES);

export function daysSince(at: string | null | undefined): number | null {
  if (!at) return null;
  const t = new Date(at).getTime();
  return Number.isNaN(t) ? null : (Date.now() - t) / 86400000;
}

/** A support case mirrors its DSP case, so list the DSP case when both are present. */
export function withoutMirrors(cases: QueueCase[], dependencyType = "DSP"): QueueCase[] {
  const ids = new Set(cases.map((c) => c.id));
  return cases.filter((c) => c.record_type === dependencyType
    || !c.related.some((r) => r.record_type === dependencyType && ids.has(r.id)));
}
