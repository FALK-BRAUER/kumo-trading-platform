/** Each lane's CADENCE and next rebalance date, from `/strategies` (#888).
 *
 * The book tile's lane cell shows the liveness figures beside a sleeve; the liveness alarm (#349) is
 * calibrated for DAILY lanes. On 2026-09-11 QC345-003 — a MONTHLY rotation, exit-only on every other
 * session — read `7 sessions since decision` and was taken for a lane dead for a week. Nothing on the
 * screen said "monthly". This hook fetches what does.
 *
 * NULL PER LANE, never a default. A lane absent from the map renders `cadence —` (see `cadenceNote`),
 * because a fetch failure rendering like a daily lane is precisely the misreading this exists to end.
 * A SELECTOR over `useStrategyRows`, the one read of `/strategies` — the sleeve beside this note on a
 * cell must come from the same snapshot, so there is one query key, not one per hook.
 */
import { useStrategyRows } from "@/lib/framework/useStrategyRows";

export interface LaneCadence {
  /** `manual` | `daily` | `monthly` from the registry; `null` when the api did not say. */
  cadence: string | null;
  /** ISO date of the next scheduled decision for a rotation cadence; `null` otherwise or unknown. */
  next_rebalance: string | null;
}

export function useLaneCadence(): Record<string, LaneCadence> {
  const { rows } = useStrategyRows();
  const out: Record<string, LaneCadence> = {};
  for (const r of rows ?? []) {
    const id = String(r.strategy_id ?? "");
    if (!id) continue;
    out[id] = {
      cadence: typeof r.cadence === "string" ? r.cadence : null,
      next_rebalance: typeof r.next_rebalance === "string" ? r.next_rebalance : null,
    };
  }
  // AN EMPTY MAP ON FAILURE: every lane then renders `cadence —`, loud per lane, never as daily.
  return out;
}
