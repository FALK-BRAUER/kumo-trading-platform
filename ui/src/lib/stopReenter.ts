/**
 * STOP-AND-REENTER (#47) toggle helpers — pure logic, split out from `PositionDetail.tsx` so a
 * node-environment test can import it without pulling the whole React component (JSX) transitively (same
 * circular-import class already fixed once this session for the watchlist tile's schema.ts/defaults.ts).
 */
import type { Levels } from "@/lib/ichimoku";

/** Default levels for the toggle — a starting point, not a precise stop read: the UI has no way today to
 *  identify a position's REAL resting protective-stop price (no `is_reduce_only` on `WorkingOrderDTO` yet,
 *  pending #73/#77), so `reclaim_price` here is necessarily a placeholder. The engine overwrites it with
 *  the ACTUAL fill price the moment the stop fires (`_StopReenterWatch.apply`, engine_node.py) — this
 *  default is never used for a real trading decision. `floor`/`base` use Ichimoku structure (cloud edge /
 *  Kijun) as reasonable supports for a v1 default. */
export function stopReenterDefaults(side: "LONG" | "SHORT", price: number | null, levels: Levels | null) {
  const long = side === "LONG";
  const kijun = levels?.kijun ?? null;
  const cloudEdge = long ? (levels?.cloudBot ?? null) : (levels?.cloudTop ?? null);
  const ma200 = levels?.ma200 ?? null;
  const floorCandidates = [cloudEdge, ma200].filter((v): v is number => v != null);
  const floor = floorCandidates.length ? (long ? Math.min(...floorCandidates) : Math.max(...floorCandidates)) : null;
  return {
    reclaim_price: price,
    base_price: kijun ?? price,
    floor_price: floor ?? (price != null ? price * (long ? 0.95 : 1.05) : null),
  };
}
