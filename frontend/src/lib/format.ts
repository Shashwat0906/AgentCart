/** ₹1,24,990 — Indian digit grouping, no decimals (all catalogue prices are whole rupees). */
export function formatINR(amount: number): string {
  const sign = amount < 0 ? "-" : "";
  const s = String(Math.abs(Math.round(amount)));
  if (s.length <= 3) return `${sign}₹${s}`;
  const tail = s.slice(-3);
  let head = s.slice(0, -3);
  const groups: string[] = [];
  while (head.length > 2) {
    groups.unshift(head.slice(-2));
    head = head.slice(0, -2);
  }
  if (head) groups.unshift(head);
  return `${sign}₹${groups.join(",")},${tail}`;
}

export function discountPercent(price: number, mrp: number): number {
  if (!mrp || mrp <= price) return 0;
  return Math.round(((mrp - price) / mrp) * 100);
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export function relativeTime(iso: string, now: Date = new Date()): string {
  const diff = (now.getTime() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export const CATEGORY_LABELS: Record<string, string> = {
  laptops: "Laptops",
  smartphones: "Smartphones",
  headphones: "Headphones",
  monitors: "Monitors",
  tablets: "Tablets",
  smartwatches: "Smartwatches",
  keyboards: "Keyboards",
  mice: "Mice",
  speakers: "Speakers",
};

/** Renders **bold** segments from agent summaries without using innerHTML. */
export function splitBold(text: string): { text: string; bold: boolean }[] {
  return text
    .split(/(\*\*[^*]+\*\*)/g)
    .filter(Boolean)
    .map((part) => (part.startsWith("**") && part.endsWith("**") ? { text: part.slice(2, -2), bold: true } : { text: part, bold: false }));
}
