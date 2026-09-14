/**
 * The per-lane window delta (#699), and the detector for it silently going dark.
 *
 * `Δunrealized(W) = standing_now(lane) − base(lane)`. The tile knows the first term from the live
 * frame; the second comes from `GET /pnl/unrealized-base`, which reads #734's end-of-day observation
 * table.
 *
 * IT IS NOT ADDED TO REALIZED. `realized(W)` is FIFO by lot while both ends of this are average
 * cost, so a partial close of heterogeneous lots makes the sum wrong by
 * `closed_qty x (avg − fifo_lot_basis)` — measured, not theorised, and momentum scales out
 * routinely. The cell carries the two figures side by side. See `cellHeadline`.
 */

import type { components } from "@/lib/api/schema";

export type BaseByPeriod = components["schemas"]["WindowBaseResponse"]["by_period"];
export type NetByPeriod = components["schemas"]["WindowBaseResponse"]["net"];
export type NetTerms = components["schemas"]["NetTerms"];
type LaneMap = Record<string, number | null> | null | undefined;
type NetMap = Record<string, NetTerms> | null | undefined;


/**
 * The `strategy_id` a displayed row's base is stored under.
 *
 * THE UNCLAIMED ROW IS NOT A LANE. It is the EXTERNAL bucket wearing a friendlier label, and the
 * observation table keys by the real `strategy_id` (`eod_observer.py` groups by
 * `position.strategy_id`). Asking the base map for "Unclaimed" therefore asks for a name that is
 * never in it.
 *
 * THIS EXACT DEFECT HAS SHIPPED ONCE, for the realized figure — `books.ts` carries its post-mortem:
 * `by_strategy["Unclaimed"]` does not exist, so EXTERNAL money rendered nowhere. `rowPeriodRealized`
 * was taught the mapping; `windowDelta` was not, and two derivations of one fact drifted apart in
 * the usual way.
 *
 * IT LIVES HERE, NOT AT THE CALL SITES, on purpose. A resolver the caller must remember to apply is
 * a list that has to be kept correct as call sites are added; doing it inside the one function that
 * consumes the map makes forgetting impossible instead. That preference — make the dangerous thing
 * unreachable rather than enumerate everywhere it applies — is what finally closed the live-database
 * test leak, after three separate rounds each ended with someone declaring the class closed.
 */
export function baseLaneKey(lane: string): string {
  return lane === "Unclaimed" ? "EXTERNAL" : lane;
}

/**
 * `standing − base` for one lane, or null when either end is unknown.
 *
 * THE THREE STATES, and the middle one is the whole reason #734 built a table rather than doing a
 * subtraction:
 *
 *   - `null` MAP — the window has no base, the day was never captured, or the read failed. UNKNOWN.
 *   - EMPTY map — the manifest says that day WAS captured and every lane was flat, so a lane absent
 *     from it has a base of ZERO. Known, not unknown; treating it as unknown suppresses a value we
 *     have, and treating a never-captured day as zero fabricates one.
 *   - `null` LANE — that lane had an unpriced leg at the base date, so its total was unknown then.
 *
 * A non-finite standing figure is refused rather than propagated: NaN survives every comparison
 * written for numbers and renders as "NaN" in a cell.
 */
export function windowDelta(
  standingNow: number | null | undefined,
  base: LaneMap,
  lane: string,
): number | null {
  if (base === null || base === undefined) return null;
  if (typeof standingNow !== "number" || !Number.isFinite(standingNow)) return null;
  const key = baseLaneKey(lane);
  const at = Object.prototype.hasOwnProperty.call(base, key) ? base[key] : 0;
  if (typeof at !== "number" || !Number.isFinite(at)) return null;
  return standingNow - at;
}

/**
 * Lanes that hold something and appear in NO period's base map.
 *
 * THE SILENT-DARKNESS DETECTOR. A `strategy_id` namespace mismatch between the observation table and
 * the live frame would leave every delta null while looking exactly like "no bases yet" — the
 * feature dark, indefinitely, with nothing wrong on any screen. Agreement is not connection, and
 * this codebase has been bitten by that shape repeatedly.
 *
 * VACUITY GUARD FIRST: when every period map is null, nothing has been captured yet and there is no
 * mismatch to report. A detector that fired on every fresh instance would be muted before it ever
 * meant anything — the notifier-death pattern already paid for here.
 */
export function laneCoverageGap(
  standingByLane: Record<string, number | null>,
  byPeriod: BaseByPeriod | null | undefined,
): string[] {
  const maps = Object.values(byPeriod ?? {}).filter(
    (m): m is Record<string, number | null> => m !== null && m !== undefined,
  );
  if (maps.length === 0) return [];
  // AN EMPTY MAP COVERS EVERY LANE. It means the manifest says that day was captured and everything
  // was flat, so a lane absent from it has a legitimate base of ZERO — not a missing name. Counting
  // it as coverage of nothing would report every held lane as a namespace mismatch on the first
  // day after a flat close, which is a detector that cries and therefore gets muted.
  if (maps.some((m) => Object.keys(m).length === 0)) return [];
  const covered = new Set(maps.flatMap((m) => Object.keys(m)));
  return Object.entries(standingByLane)
    .filter(([lane, standing]) =>
      typeof standing === "number" && Number.isFinite(standing) && standing !== 0 &&
      !covered.has(lane))
    .map(([lane]) => lane)
    .sort();
}


/** A lane's window NET OF FLOWS, or why it is unknown (#699 option a). */
export interface WindowNet {
  /** `mv_now − mv_base − invested(W)`; null when any term is unknown. */
  value: number | null;
  /** The served, NAMED reason the window is partial ("first observed <d>", "not captured: <d>"). */
  partial: string | null;
  /** Why `value` is null, for a title attribute. Never rendered as a number. */
  reason: string | null;
}

/**
 * `net(W) = mv_now − mv_base − invested(W)` for one lane — THE IDENTITY, with no basis rule in it.
 *
 * WHY THIS AND NOT `realized(W) + Δunrealized(W)`: those two terms use different cost bases (FIFO by
 * lot vs average) and a partial close of heterogeneous lots makes their sum wrong by
 * `closed_qty x (avg − fifo_lot_basis)` — `cellHeadline` carries the counter-example. Change in
 * market value net of what was invested has no basis in it at all: the counter-example nets to zero.
 * It is also the account headline's own identity (equity change net of flows), so the lane cells
 * compose like DELTA NET above them.
 *
 * THREE STATES, as for `windowDelta`: a null MAP is a window with no base (unknown); a lane ABSENT
 * from a served map held nothing at the base and traded nothing after it (captured-and-flat: both
 * terms are a KNOWN zero, so its net is its live market value); a null TERM is unknown — an unpriced
 * leg at the base, or flows the cache cannot vouch for. A non-finite live market value is refused.
 */
export function windowNet(mvNow: number | null | undefined, net: NetMap, lane: string): WindowNet {
  if (net === null || net === undefined) return { value: null, partial: null, reason: "no base for this window" };
  if (typeof mvNow !== "number" || !Number.isFinite(mvNow)) {
    return { value: null, partial: null, reason: "this lane's live market value is unknown (an unmarked leg)" };
  }
  const key = baseLaneKey(lane);
  // A lane ABSENT from a served map held nothing at the base and had no fill after it — so whatever
  // it holds NOW arrived with no fill behind it. Both terms are a KNOWN zero (captured-and-flat),
  // the net is its live market value, and the cell SAYS the lane was not in the served terms.
  const terms: NetTerms = Object.prototype.hasOwnProperty.call(net, key)
    ? net[key]
    : { mv_base: 0, invested: 0, partial: mvNow === 0 ? null : "not in the served terms: no base row and no fill after the base" };
  const partial = terms.partial ?? null;
  const mvBase = terms.mv_base;
  const invested = terms.invested;
  if (typeof mvBase !== "number" || !Number.isFinite(mvBase)) {
    return { value: null, partial, reason: "market value at the window's start is unknown (an unpriced leg)" };
  }
  if (typeof invested !== "number" || !Number.isFinite(invested)) {
    return { value: null, partial, reason: "flows over the window are unknown (no engine flows, or the cache cannot vouch back to the base)" };
  }
  return { value: mvNow - mvBase - invested, partial, reason: null };
}
