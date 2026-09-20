/**
 * The BOOK panel's stated identity (#345).
 *
 * NET's own subtitle promises "realized + change in unrealized over the window". The cell beside it
 * rendered the unrealized LEVEL — the whole standing mark — so a reader who added the two numbers the
 * subtitle names got a third number that was not NET. Measured on a live instance paper account 2026-08-19:
 *
 *     window   REALIZED + UNREALIZED        NET
 *     1W       2,650.21 + 1,176.79 = 3,827.00      2,922.86
 *     1M       1,819.57 +   176.79 = 1,996.36      1,556.25
 *     3M         138.50 + 1,176.79 = 1,315.29        847.61
 *
 * The operator added them up and asked why it did not reconcile — the same reading that produced #310's
 * DEPLOYED + CASH ≠ LIQUIDATION. The term NET actually uses was never on screen.
 *
 * WHY NOT RELABEL INSTEAD. "Unrealized · now" would make the panel honest and still leave the reader
 * unable to see where NET came from. The window's Δ is the load-bearing quantity and it MOVES — 272.65 /
 * 736.68 / 709.11 / −679.77 across the four windows on one snapshot — so it earns the cell. The standing
 * level survives as the sub-line, because it is what the per-strategy cells below sum to.
 *
 * DERIVED, NOT RECOMPUTED — REVERSED BY #596, and the reasoning is worth keeping because half of it
 * still holds. Recomputing the mark's movement from per-position marks against window-start prices
 * WOULD build a second ledger, and that trap is real (#242/#233/#343). What the argument missed is
 * that `unrealized_intraday_pl` is not our recomputation: it is the broker's own field, from the same
 * payload and the same provenance as REALIZED. Reading it is not building a ledger.
 *
 * And the disagreement this feared is the POINT. `Δ = NET − REALIZED` made the panel's stated identity
 * true by construction, so it could never disagree and therefore never detect anything — which is how
 * 2026-08-27 rendered Δ as exactly minus REALIZED in every period with NET $0.00. See
 * `measuredUnrealizedDelta` and `netFromComponents` below for what replaced it.
 */

/**
 * The window's change in unrealized: what NET contains beyond what was realized in it.
 *
 * `null` when either operand is unknown. NOT zero: "no broker P&L for this window yet" and "the mark did
 * not move" are different claims, and the second one is a much stronger thing to assert on a hero panel.
 * A window can legitimately be negative here while REALIZED is positive — a week that booked gains while
 * the remaining book gave some back — so no clamping.
 */
export function deltaUnrealized(net: number | null | undefined, realized: number | null | undefined): number | null {
  if (typeof net !== "number" || !Number.isFinite(net)) return null;
  if (typeof realized !== "number" || !Number.isFinite(realized)) return null;
  return net - realized;
}

/**
 * The broker's own unrealized totals, as the account plane delivers them.
 *
 * NULL MEANS UNKNOWN AND MUST SURVIVE AS UNKNOWN. The backend sums per-position broker fields and
 * publishes null if ANY position lacks one — a total built from a partial book is a wrong number
 * wearing a measurement's clothes, and IBKR publishes no intraday field at all.
 */
export interface UnrealizedTotals {
  /** Σ unrealized P&L since entry, across open positions. Venue-neutral. */
  unrealized_standing_total?: number | null;
  /** Σ of the DAY's change in unrealized. Alpaca reports it per position; IBKR does not. */
  unrealized_intraday_total?: number | null;
}

/** Which broker total actually means "this period's change in unrealized". */
const FIELD_FOR_PERIOD: Record<string, keyof UnrealizedTotals> = {
  // THIS IS AN ASSUMPTION, NOT AN IDENTITY, and the first version of this comment claimed otherwise.
  // Standing unrealized equals the account's LIFETIME change only if nothing was held before the
  // broker's record begins. Transferred-in positions, holdings predating data inception, corporate
  // actions with adjusted basis, or broker cost-basis corrections all break it (codex, scope
  // review). It is used here because those do not apply to these paper accounts, and it is written
  // down so the next reader can check rather than inherit it.
  all: "unrealized_standing_total",
  // The broker measures the day's move itself. We do not recompute it from marks.
  "1D": "unrealized_intraday_total",
  // 1W/1M/3M are deliberately absent: no broker reports "unrealized as of seven days ago", and
  // nothing records it yet (#596). A number here would be a guess wearing a measurement's clothes.
};

/**
 * The window's change in unrealized, MEASURED — or null when it cannot be.
 *
 * REPLACES THE BACK-SOLVE (#596). This used to be `net - realized`, which made the panel's stated
 * identity `NET = REALIZED + ΔUNREALIZED` true by construction: it could not disagree, so it could
 * not detect anything. On 2026-08-27 every period printed Δ = exactly minus REALIZED, because NET
 * was 0 and `0 - realized` is what that produces.
 *
 * NOT A SECOND LEDGER, which is what the comment above this one feared. We are not marking
 * positions against window-start prices; we are reading the broker's own figures, from the same
 * payload and the same provenance as REALIZED.
 *
 * NULL, NEVER ZERO. "This venue does not report it" and "the mark did not move" are different
 * claims, and only one of them is safe to put on a hero panel.
 */
export function measuredUnrealizedDelta(
  period: string,
  totals: UnrealizedTotals | null | undefined,
): number | null {
  const field = FIELD_FOR_PERIOD[period];
  if (!field || !totals) return null;
  const v = totals[field];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * NET for the window: realized + the window's change in unrealized. Both measured, neither derived
 * from the other.
 *
 * 2026-08-27: "Net at the top is simply realised + unrealised for the period. cash and
 * liquidation and balance are completely different things."
 *
 * `equity - base_value` (`periodNet`) is NOT the source any more. It stays as an independent CHECK:
 * two derivations of one fact, so a disagreement is a detector rather than an impossibility.
 *
 * A KNOWN, EXPECTED GAP between the two. Realized is recognised AT THE SALE with the lot's original
 * basis (`realized_broker.py:126` — the statement and tax-lot convention), so a lot bought before a
 * window and sold inside it puts its ENTIRE lifetime gain in that window. Δunrealized only covers
 * what is still open. So for 1D/1W/1M/3M this sum legitimately exceeds the equity change by the
 * pre-window unrealized of anything closed in the window. ALL is exact, because nothing precedes
 * inception. Whoever compares the check to the sum must expect that term rather than chase it.
 */
export function netFromComponents(
  realized: number | null | undefined,
  deltaUnrealized: number | null | undefined,
): number | null {
  if (typeof realized !== "number" || !Number.isFinite(realized)) return null;
  if (typeof deltaUnrealized !== "number" || !Number.isFinite(deltaUnrealized)) return null;
  return realized + deltaUnrealized;
}
