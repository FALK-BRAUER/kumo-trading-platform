/** Each lane's market-aware readings, from `/strategies` (#873 phase 1).
 *
 * A SELECTOR over `useStrategyRows` — the one read of `/strategies` — so the badge beside a sleeve comes
 * from the same snapshot as the sleeve. `undefined` per lane when the fetch failed or the api predates
 * the field; `null` when the api says the engine frame could not be read. Both render as their own
 * state in `marketAwareBadge`, never as "nothing to report".
 */
import { useStrategyRows, type LaneMarketAware } from "@/lib/framework/useStrategyRows";

export type { LaneMarketAware };

export function useLaneMarketAware(): Record<string, LaneMarketAware | null> {
  const { rows } = useStrategyRows();
  const out: Record<string, LaneMarketAware | null> = {};
  for (const r of rows ?? []) {
    const id = String(r.strategy_id ?? "");
    if (!id) continue;
    out[id] = r.market_aware === undefined ? null : r.market_aware;
  }
  return out;
}
