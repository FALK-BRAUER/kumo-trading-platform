import type { BarDTO } from "@/lib/api/types";
import type { Quote, TodayRange, Fundamentals } from "../instrument";

/** Input a KPI formatter reads from. Framework-generic — knows nothing about any specific KPI; those
 *  live entirely in `@/config/kpis.ts`. */
export interface KpiContext {
  instrumentId: string;
  price: number | null;
  quote: Quote | null;
  bars: BarDTO[];
  vwap: number | null;
  todayRange: TodayRange | null;
  fundamentals: Fundamentals | null;
}

/** One KPI's display logic — a registrable definition, not hardcoded row logic. `format` returns null
 *  (not a placeholder string) when the value isn't available yet — "unknown" and "zero" are different.
 *  `sortValue` is optional and separate from `format` (code review, #182 follow-up: sorting needs a raw
 *  comparable value, not the formatted display string — a future "sort by KPI" feature reads this, not
 *  `format`'s output) — not every KPI has a natural single sortable value, so it's fine to omit. */
export interface KpiDef {
  id: string;
  label: string;
  /** Short (1-4 char) prefix shown inline with the value in the watchlist row — the row has no room for
   *  the full `label`, which stays reserved for a tooltip/future config-UI listing. Falls back to `label`
   *  if omitted (the operator feedback: 4 unlabeled stacked numbers under the Blue Flag chips read as noise —
   *  every KPI needs a visible label, not just a hover-only tooltip that doesn't even work on touch). */
  shortLabel?: string;
  format: (ctx: KpiContext) => string | null;
  sortValue?: (ctx: KpiContext) => number | string | null;
}
