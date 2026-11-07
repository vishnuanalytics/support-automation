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
