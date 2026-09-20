/**
 * Pure status→tone logic for StatusBadge (#104 / #114). Kept JSX-free so it unit-tests under the
 * node-env vitest config (see StatusBadge.tsx for the rendered component).
 *
 * A terminal ERROR (DENIED / REJECTED) is prominent — a bad order must read at a glance. Benign
 * lifecycle states stay quiet; CANCELED / EXPIRED are terminal-but-not-error (visible, muted).
 */

export type BadgeTone = "error" | "filled" | "partial" | "working" | "done";

const TERMINAL_ERROR = new Set(["DENIED", "REJECTED"]);
const TERMINAL_DONE = new Set(["CANCELED", "CANCELLED", "EXPIRED"]);

/** Classify a raw order status into a badge tone. */
export function toneForStatus(status: string): BadgeTone {
  const s = status.toUpperCase();
  if (TERMINAL_ERROR.has(s)) return "error";
  if (s === "FILLED") return "filled";
  if (TERMINAL_DONE.has(s)) return "done";
  if (s.includes("PARTIALLY")) return "partial";
  return "working"; // ACCEPTED / SUBMITTED / WORKING / PENDING…
}

/** Tone → Tailwind classes (semantic tokens, day/night aware). Error is filled + ringed + bold. */
export const TONE_CLASS: Record<BadgeTone, string> = {
  error: "bg-status-bear/25 text-status-bear ring-1 ring-status-bear/40 font-bold",
  filled: "bg-status-bull/15 text-status-bull",
  partial: "bg-status-info/15 text-status-info",
  working: "bg-status-info/15 text-status-info",
  done: "text-t3 ring-1 ring-ds-line2",
};
