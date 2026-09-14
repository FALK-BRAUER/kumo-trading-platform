/**
 * The lane's realized delta per window, from the broker sweep (#662).
 *
 * Operator: "The strategy tiles should also show a delta as per selected time window." The tile used to
 * show only the session (since-boot) figure, which under a window label is the #653 confusion — a
 * restart at 12:12 ET made TECHIVOL read −$59.98 "today" while the ET day stood at +$221.63.
 *
 * Source is the SWEEP's per-strategy buckets (`realized_periods[W].by_strategy`), the same numbers
 * the Book rows render, so the tile and the Book cannot disagree. Δunrealized per lane per window is
 * NOT derivable (no per-lane equity curves), so this is deliberately realized-only and labelled so —
 * pretending to a NET the data cannot support is how #336 shipped.
 *
 * `null` = the frame predates the sweep or the window is absent: unknown, rendered as a dash, never
 * zero. A lane absent from a window's buckets with the sweep PRESENT genuinely realized nothing
 * there — that is 0, not unknown; the two must not collapse (absence rules, CLAUDE.md).
 */

export interface RealizedPeriodsLike {
  realized_periods?: Record<string, { by_strategy?: Record<string, number> | null } | null> | null;
}

export const WINDOW_ORDER = ["1D", "1W", "1M", "3M", "all"] as const;

export function laneWindowRealized(
  frame: RealizedPeriodsLike | null | undefined,
  strategyId: string,
  period: string,
): number | null {
  const window = frame?.realized_periods?.[period];
  if (!window) return null;
  const buckets = window.by_strategy;
  if (buckets == null) return null;
  return buckets[strategyId] ?? 0;
}
