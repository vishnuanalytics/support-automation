export async function copyText(text: string): Promise<void> {
  await navigator.clipboard.writeText(text);
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}
