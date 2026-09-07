import { TERMINAL } from "./graph";

export type NodeKind = "trigger" | "work" | "terminal" | "invalid";

const TRIGGERS = new Set(["trigger", "inbound_email", "webhook", "schedule", "sf_case_hook"]);

/** The left-rule colour: cyan for work, yellow for a terminal, magenta for an
 *  unknown type. `data.invalid` (stamped once the type registry has loaded)
 *  wins. */
export function nodeKind(data: { nodeType: string; terminal?: boolean; invalid?: boolean }): NodeKind {
  if (data.invalid) return "invalid";
  if (data.terminal || TERMINAL.has(data.nodeType)) return "terminal";
  if (TRIGGERS.has(data.nodeType)) return "trigger";
  return "work";
}

const host = (url: unknown): string => {
  if (typeof url !== "string" || !url) return "";
  try {
    return new URL(url).host;
  } catch {
    return url.replace(/^https?:\/\//, "").split("/")[0];
  }
};

const num = (v: unknown): number | null => (typeof v === "number" && !Number.isNaN(v) ? v : null);

/**
 * The one config value that decides a node's behaviour — printed on line 2 of
 * its card so the canvas is worth looking at, not just the inspector.
 */
export function summarize(type: string, config: Record<string, unknown>): string {
  const c = config ?? {};
  switch (type) {
    case "confidence_gate": {
      const t = c.thresholds && typeof c.thresholds === "object" ? c.thresholds : null;
      if (t) {
        const vals = Object.values(t as Record<string, unknown>).map(num).filter((n): n is number => n != null);
        if (vals.length) {
          const lo = Math.min(...vals).toFixed(2);
          const hi = Math.max(...vals).toFixed(2);
          return `${vals.length} tier${vals.length === 1 ? "" : "s"} · ${lo === hi ? lo : `${lo}–${hi}`}`;
        }
      }
      return num(c.threshold) != null ? `t = ${num(c.threshold)!.toFixed(2)}` : "tier thresholds";
    }
    case "kb_lookup": {
      const cols = Array.isArray(c.collections) ? c.collections : c.collection ? [c.collection] : [];
      const k = num(c.k) ?? num(c.top_k);
      const head = cols.length === 1 ? String(cols[0]) : `${cols.length} collection${cols.length === 1 ? "" : "s"}`;
      return k != null ? `${head} · k=${k}` : head || "internal KB";
    }
    case "retrieve": {
      const k = num(c.k) ?? num(c.top_k);
      return k != null ? `k=${k}` : typeof c.collection === "string" ? String(c.collection) : "public docs";
    }
    case "draft":
    case "classify":
    case "agent":
    case "ai_prompt": {
      if (typeof c.model === "string" && c.model) return c.model;
      const tok = num(c.max_tokens);
      return tok != null ? `${tok} tok` : type === "ai_prompt" ? "free-form prompt" : "";
    }
    case "ask_human":
    case "handover":
    case "notify":
    case "clarify": {
      if (typeof c.queue === "string" && c.queue) return `queue · ${c.queue}`;
      if (typeof c.target === "string" && c.target) return c.target;
      return "";
    }
    case "team_route":
      return "keyword route";
    case "policy_gate":
      return "team rules";
    case "http_request":
      return `${String(c.method ?? "GET").toUpperCase()} ${host(c.url)}`.trim();
    case "connector_action":
      return [c.connector, c.action].filter(Boolean).join(" · ") || "connector action";
    case "transform":
      return typeof c.op === "string" ? c.op : "reshape state";
    case "sf_writeback": {
      const f = c.fields && typeof c.fields === "object" ? Object.keys(c.fields as object).length : 0;
      return f ? `${f} field${f === 1 ? "" : "s"}` : "triage → Case";
    }
    case "trigger":
      return typeof c.source === "string" ? String(c.source) : "webhook / schedule";
    default: {
      const entry = Object.entries(c).find(
        ([, v]) => typeof v === "string" || typeof v === "number" || typeof v === "boolean",
      );
      return entry ? `${entry[0]} ${entry[1]}` : "";
    }
  }
}
