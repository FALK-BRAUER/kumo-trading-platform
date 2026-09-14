/**
 * The EQUITY headline figure (#336).
 *
 * WHAT WAS WRONG. The headline was the last point of the SELECTED window's curve, so switching period
 * changed the number it presented as current equity: $100,679 at 1W, $100,116 at 1M and 3M — while the
 * BOOK panel beside it read LIQUIDATION $100,658.95 from the broker. Current equity is ONE number. Only
 * the DELTA belongs to the window.
 *
 * Two causes, both real:
 *   1. Each curve is sampled independently, so its final point is not necessarily "now" — a coarser
 *      window's last sample can be hours stale.
 *   2. That sample is the broker's period-history figure, which is the PREVIOUS SESSION'S CLOSE for a
 *      window that has not ticked today — the same `last_equity` confusion behind NET(1D).
 *
 * So the headline now comes from the broker's live account equity, which is the same field LIQUIDATION
 * renders. Two derivations of one fact become one: they are the same number by construction and can no
 * longer disagree on screen.
 *
 * The curve's last point stays as the fallback for a node with no broker account (synthetic/non-Alpaca),
 * where a slightly stale figure beats no figure — but it is the fallback, never the preferred source.
 */

export interface AccountLike {
  equity?: number | null;
}

/**
 * Current account equity for the headline: the broker's figure, independent of the selected period.
 *
 * `curveLast` is used ONLY when the broker offers nothing. Returns null when neither is available, so
 * the caller renders nothing rather than a zero — an account worth $0 is a very different claim from an
 * account whose equity has not arrived yet.
 */
export function headlineEquity(account: AccountLike | undefined, curveLast: number | null): number | null {
  const equity = account?.equity;
  if (typeof equity === "number" && Number.isFinite(equity)) return equity;
  return typeof curveLast === "number" && Number.isFinite(curveLast) ? curveLast : null;
}
