export async function copyText(text: string): Promise<void> {
  await navigator.clipboard.writeText(text);
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

export function formatAge(hours: number): string {
  if (hours < 1) return "under 1h";
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

export function formatDay(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleDateString("en-CA") : "";
}

/** e.g. 30 Oct, 13:01 */
export function formatDayTime(value: string | null | undefined): string {
  if (!value) return "";
  const d = new Date(value);
  const day = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  return `${day}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
}

/** Spreadsheets run a cell starting with these as a formula, and subjects come from outside. */
const FORMULA_START = /^[=+\-@\t\r]/;

function csvCell(value: string | number | null | undefined): string {
  if (typeof value === "number") return String(value);
  const text = value ?? "";
  const safe = FORMULA_START.test(text) ? `'${text}` : text;
  return /[",\r\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

export function businessLabel(b: { business?: string | null; biz_id?: string | null }): string {
  return [b.business, b.biz_id].filter(Boolean).join(" · ");
}

export function matchesBusiness(query: string, items: { business?: string | null; biz_id?: string | null }[]): boolean {
  const q = query.trim().toLowerCase();
  return !q || items.some((b) => `${b.business ?? ""} ${b.biz_id ?? ""}`.toLowerCase().includes(q));
}

export function toCsv(rows: (string | number | null | undefined)[][]): string {
  return rows.map((r) => r.map(csvCell).join(",")).join("\r\n");
}

export function downloadCsv(name: string, rows: (string | number | null | undefined)[][]): void {
  // The byte order mark tells Excel this is UTF-8.
  const blob = new Blob([`﻿${toCsv(rows)}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

/** A saved check's rows as a header + body. Rows may be objects (labelled) or plain arrays. */
export function checkTable(rows: unknown[]): { head: string[]; body: string[][] } {
  const cell = (v: unknown) => (v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
  const objects = rows.filter((r): r is Record<string, unknown> =>
    r != null && typeof r === "object" && !Array.isArray(r));
  if (objects.length === rows.length && rows.length > 0) {
    const head = [...new Set(objects.flatMap((r) => Object.keys(r)))];
    return { head, body: objects.map((r) => head.map((k) => cell(r[k]))) };
  }
  return { head: [], body: rows.map((r) => (Array.isArray(r) ? r.map(cell) : [cell(r)])) };
}

/** The prompt the "Ask Claude" button copies, for a Claude session with Case Monitor's MCP server. */
export function askClaudePrompt(caseRef: string): string {
  return [
    `Debug case ${caseRef} and give me a resolution I can send, with proof.`,
    "",
    "1. Start with case-monitor get_case: fields, brief, timeline, linked_cases, check_inputs and " +
      "saved_checks. Don't re-run a check that is already saved.",
    "2. Read every linked GitHub issue (all comments) and Slack thread with your own connectors.",
    "3. Run the Metabase \"Case checks\" questions that fit (ORD-01/02 order and status, ITM-01/02 items " +
      "and stock, MNU-01 menu publishes, STR-01 store actions, ID-01/02 ID checks), using check_inputs. " +
      "Record each one with save_check.",
    "4. Answer in this order:",
    "   - Root cause: one sentence.",
    "   - Evidence: one line per claim, naming its source (check name and rows, GitHub comment, " +
      "Slack message or case email) with the exact value or quote.",
    "   - Not verified: anything you could not confirm, and why (for example app logs that are only in Kibana).",
    "   - Next action: who does what.",
    "   - Draft reply: for the merchant or aggregator, using only verified facts.",
    "",
    "5. Save the write-up on the case with case-monitor save_investigation (root cause, evidence with an " +
      "exact quote per claim, what's not verified, next action, draft reply) so it shows at the top of the case.",
    "",
    "Rules: never state an unverified guess as fact. Show times in IST. " +
      "Don't send, post or change anything: I'll send the reply.",
  ].join("\n");
}
