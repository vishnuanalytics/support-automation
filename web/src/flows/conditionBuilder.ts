// Parses/serializes the small boolean-expression grammar an edge's
// `condition.if` actually supports (see `interpreter/conditions.py`) into a
// list of simple, AND-joined (field, operator, value) rows a friendly UI can
// render as dropdowns instead of a hand-typed expression.
//
// The backend evaluator parses `if` as a *Python* expression (safe AST
// eval) — `and`/`or`/`not`, comparisons, dotted-attribute/subscript access,
// and literal strings/numbers/True/False/None. It does NOT understand
// `&&`/`||` (that's not valid Python and raises a SyntaxError at eval time,
// on a real case run — there's no syntax check at save time to catch it
// earlier). Anything this module serializes must stay inside that grammar.
//
// Only a flat chain of `and`-joined comparisons round-trips through the
// builder. `or`, `not`, and nested parens fall back to `null` — the caller
// switches to the raw expression box for those (nothing is lost, the
// builder just doesn't claim to handle everything).

export type Op = "eq" | "ne" | "lt" | "lte" | "gt" | "gte" | "in" | "not_in" | "is_set" | "is_not_set";

export type Clause = {
  field: string;
  op: Op;
  /** Display value. For `in`/`not_in`, a comma-separated list. Unused for is_set/is_not_set. */
  value: string;
};

// Short on purpose — these sit in a ~100px-wide <select> in the edge
// inspector's fixed 380px panel; the full sentence ("is less than") just
// clips there instead of wrapping. Full wording still lives in the
// operator's `title` (via OP_TITLES) for anyone unsure what a symbol means.
export const OP_LABELS: Record<Op, string> = {
  eq: "equals",
  ne: "not equal",
  in: "one of",
  not_in: "not one of",
  is_set: "is set",
  is_not_set: "not set",
  lt: "<",
  lte: "≤",
  gt: ">",
  gte: "≥",
};

export const OP_TITLES: Record<Op, string> = {
  eq: "equals",
  ne: "does not equal",
  in: "is one of",
  not_in: "is not one of",
  is_set: "is set (not empty)",
  is_not_set: "is not set (empty)",
  lt: "is less than",
  lte: "is at most",
  gt: "is greater than",
  gte: "is at least",
};

export const OP_ORDER: Op[] = ["eq", "ne", "in", "not_in", "is_set", "is_not_set", "lt", "lte", "gt", "gte"];

const OP_TOKEN: Record<"eq" | "ne" | "lt" | "lte" | "gt" | "gte" | "in" | "not_in", string> = {
  eq: "==", ne: "!=", lt: "<", lte: "<=", gt: ">", gte: ">=", in: "in", not_in: "not in",
};

type Lit = string | number | boolean | null;

function parseLiteral(raw: string): Lit | undefined {
  const s = raw.trim();
  if (/^'([^'\\]|\\.)*'$/.test(s)) return s.slice(1, -1).replace(/\\'/g, "'");
  if (/^"([^"\\]|\\.)*"$/.test(s)) return s.slice(1, -1).replace(/\\"/g, '"');
  if (s === "True") return true;
  if (s === "False") return false;
  if (s === "None") return null;
  if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
  return undefined;
}

function literalToDisplay(lit: Lit): string {
  if (lit === null) return "";
  if (typeof lit === "boolean") return lit ? "true" : "false";
  return String(lit);
}

/** A Python literal a user typed by hand for a value box — quote it only if
 *  it doesn't already look like a number/bool/none. */
function serializeLiteral(raw: string): string {
  const t = raw.trim();
  // NOT "" -> None: an eq/ne clause with an empty (not-yet-filled) value
  // box must serialize to '' (a real, if useless, comparison) and re-parse
  // back to the same op — silently flipping a fresh "equals" row to
  // "is not set" the instant its value box is empty would surprise anyone
  // who just added a row and hasn't picked a value yet. `is_set`/
  // `is_not_set` are a distinct Clause.op, handled in serializeCondition
  // directly (never reaches this function) precisely to keep None explicit
  // and opt-in rather than an empty-string default.
  if (/^-?\d+(\.\d+)?$/.test(t)) return t;
  if (t.toLowerCase() === "true") return "True";
  if (t.toLowerCase() === "false") return "False";
  if (t.toLowerCase() === "none" || t.toLowerCase() === "null") return "None";
  return `'${t.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
}

function splitTopLevel(s: string, sep: "and" | ","): string[] | null {
  const parts: string[] = [];
  let quote: string | null = null;
  let depth = 0;
  let cur = "";
  let i = 0;
  while (i < s.length) {
    const ch = s[i];
    if (quote) {
      cur += ch;
      if (ch === "\\" && i + 1 < s.length) {
        cur += s[i + 1];
        i += 2;
        continue;
      }
      if (ch === quote) quote = null;
      i++;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      cur += ch;
      i++;
      continue;
    }
    if (sep === "and") {
      if (ch === "(") { depth++; cur += ch; i++; continue; }
      if (ch === ")") { depth--; cur += ch; i++; continue; }
      if (depth === 0) {
        const rest = s.slice(i);
        const boundaryBefore = i === 0 || /\s/.test(s[i - 1]);
        // "not" alone is the unsupported unary-negation keyword; "not in" is
        // just the second half of a comparison operator (`x not in [...]`)
        // and must NOT trip this bail-out.
        if (boundaryBefore && (/^or\b/.test(rest) || /^not\b(?!\s+in\b)/.test(rest))) return null;
        const andMatch = /^and\b/.exec(rest);
        if (boundaryBefore && andMatch) {
          parts.push(cur.trim());
          cur = "";
          i += andMatch[0].length;
          continue;
        }
      }
      cur += ch;
      i++;
      continue;
    }
    // comma split (depth-agnostic — only called on the inside of one pair of parens)
    if (ch === ",") {
      parts.push(cur);
      cur = "";
      i++;
      continue;
    }
    cur += ch;
    i++;
  }
  if (quote || (sep === "and" && depth !== 0)) return null;
  if (cur.trim() || parts.length === 0) parts.push(cur);
  return parts;
}

const CLAUSE_RE = /^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*(==|!=|<=|>=|<|>|not\s+in|in)\s*([\s\S]+)$/;

const TOKEN_OP: Record<string, "eq" | "ne" | "lt" | "lte" | "gt" | "gte" | "in"> = {
  "==": "eq", "!=": "ne", "<": "lt", "<=": "lte", ">": "gt", ">=": "gte", in: "in",
};

function parseClause(text: string): Clause | null {
  const m = CLAUSE_RE.exec(text.trim());
  if (!m) return null;
  const [, field, rawOp] = m;
  const rawValue = m[3].trim();
  const op: "eq" | "ne" | "lt" | "lte" | "gt" | "gte" | "in" | "not_in" =
    /^not\s+in$/.test(rawOp) ? "not_in" : TOKEN_OP[rawOp];
  if (!op) return null;

  if (op === "in" || op === "not_in") {
    const listMatch = /^\((?<inner>[\s\S]*)\)$/.exec(rawValue) ?? /^\[(?<inner>[\s\S]*)\]$/.exec(rawValue);
    if (!listMatch) return null;
    const inner = (listMatch.groups?.inner ?? "").trim();
    if (!inner) return { field, op, value: "" };
    const items = splitTopLevel(inner, ",");
    if (!items) return null;
    const vals: string[] = [];
    for (const it of items) {
      const lit = parseLiteral(it);
      if (lit === undefined) return null;
      vals.push(literalToDisplay(lit));
    }
    return { field, op, value: vals.join(", ") };
  }

  const lit = parseLiteral(rawValue);
  if (lit === undefined) return null;
  if (lit === null && op === "ne") return { field, op: "is_set", value: "" };
  if (lit === null && op === "eq") return { field, op: "is_not_set", value: "" };
  return { field, op, value: literalToDisplay(lit) };
}

/** `null` = too complex for the builder (or/not/unparseable) — use the raw box. */
export function parseCondition(expr: string): Clause[] | null {
  const trimmed = expr.trim();
  if (!trimmed) return [];
  const parts = splitTopLevel(trimmed, "and");
  if (!parts) return null;
  const clauses: Clause[] = [];
  for (const p of parts) {
    if (!p.trim()) return null;
    const c = parseClause(p);
    if (!c) return null;
    clauses.push(c);
  }
  return clauses;
}

export function serializeCondition(clauses: Clause[]): string {
  return clauses
    .map((c) => {
      if (c.op === "is_set") return `${c.field} != None`;
      if (c.op === "is_not_set") return `${c.field} == None`;
      if (c.op === "in" || c.op === "not_in") {
        // a square-bracket list, not a parenthesized tuple — `(x)` alone
        // isn't a 1-element Python tuple (needs a trailing comma), and a
        // list is just as legal here (conditions.py's ast whitelist allows
        // ast.List same as ast.Tuple).
        const items = c.value.split(",").map((s) => s.trim()).filter(Boolean).map(serializeLiteral);
        return `${c.field} ${OP_TOKEN[c.op]} [${items.join(", ")}]`;
      }
      return `${c.field} ${OP_TOKEN[c.op]} ${serializeLiteral(c.value)}`;
    })
    .join(" and ");
}

// Common field paths a case-handling flow actually has in state (see
// registry.py's classify/policy_gate/team_route outputs and builder.py's
// `_context()`) — shown in the field picker; typing any other dotted path
// still works, this is suggestions, not a closed enum.
export const FIELD_OPTIONS: { value: string; label: string }[] = [
  { value: "tier", label: "Tier" },
  { value: "region", label: "Region" },
  { value: "routed_team", label: "Routed team" },
  { value: "case.channel", label: "Case channel" },
  { value: "classification.urgency", label: "Urgency" },
  { value: "classification.case_type", label: "Case type" },
  { value: "classification.answer_mode", label: "Answer mode" },
  { value: "confidence", label: "Confidence score" },
  { value: "retrieval_score", label: "Retrieval score" },
  { value: "draft_confidence", label: "Draft confidence" },
  { value: "confidence_gate.pass", label: "Confidence gate passed" },
  { value: "policy.task", label: "Policy task" },
  { value: "policy.matched", label: "Policy matched rule" },
];

export const ROUTED_TEAMS = ["support", "csm", "sales", "offboarding"];

// the channel values `case.channel` actually carries (see each ingestion
// module's own case-shaping) — lets an edge branch on which platform a
// case arrived from, same values the Connections tab's "Route by channel"
// panel maps to a connector (migration 110).
export const EDGE_CHANNELS: { value: string; label: string }[] = [
  { value: "salesforce", label: "Salesforce" },
  { value: "email", label: "Email" },
  { value: "hubspot", label: "HubSpot" },
  { value: "freshchat", label: "Freshchat" },
  { value: "zendesk", label: "Zendesk" },
];

export const URGENCY_VALUES = ["low", "normal", "high", "critical"];
export const ANSWER_MODE_VALUES = ["informational", "diagnostic", "action", "status"];
