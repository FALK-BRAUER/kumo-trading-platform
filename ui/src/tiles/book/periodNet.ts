/**
 * NET for the selected period (#336, #343).
 *
 * 2026-08-18: "net should be net as per time period."
 *
 * WHAT WAS WRONG (#336). NET was `Σ book.total`, and `book.total = book.realized + book.unrealized` where
 * `realized` is the live-cycle/SESSION figure — which reads $0.00 whenever the engine has restarted. So
 * NET collapsed to unrealized and did not move when the period changed: $1,705.31 at 1D, 1W, 1M and 3M
 * alike, sitting directly above a REALIZED that read −$9.69, $2,226.12, $825.52 and $138.50. The headline
 * number on Home ignored every trade closed in the window.
 *
 * WHY NOT "period realized + current unrealized". Because a position opened three months ago carrying
 * +$1,000 of unrealized did not earn that money today. Adding today's realized to the whole standing mark
 * reports months of accrued gain as 1D performance. Only the change in the mark ACROSS the window belongs
 * to the window:
 *
 *     NET(period) = realized(period) + Δunrealized(period)
 *
 * That identity is why NET is not `REALIZED + UNREALIZED` on screen and must not be "corrected" to it.
 *
 * WHAT WAS STILL WRONG (#343): THE WINDOW DID NOT END AT NOW.
 *
 * The quantity above is the broker's own, published per period as `curves[period].pnl`, and this module
 * used to return it verbatim. But Alpaca computes it to the LAST POINT OF ITS PORTFOLIO HISTORY, which is
 * the previous session's close — while NET(1D) was derived from LIVE equity. Measured off the live plane
 * 2026-08-18 12:40 UTC, premarket, funded with exactly $100,000 and standing at $100,232.02:
 *
 *   1D  −615.59 (live)   1W  +2,922.86   1M  +1,556.25   3M/all  +847.61   ← all to Monday's close
 *
 * A week that did not contain the day inside it, and a LIFETIME P&L of +$847.61 on an account up $232.02
 * — overstated by $615.59, exactly the loss the day had taken. The EQUITY tile showed the same split
 * personality: live equity $100,232 beside "+$1,556 this 1M" over a chart starting at $99,291.
 *
 * ONE FORMULA FOR EVERY WINDOW. The window's base is the broker's, the window's end is NOW:
 *
 *     NET(period) = live equity − base(period)
 *
 * This is not a second ledger. It is Alpaca's OWN identity — `exec_client.py::_report_equity_curve`
 * computes `pnl = equity[-1] − base_value` — evaluated at now instead of at `equity[-1]`. Both operands
 * are still the broker's own fields, so the "broker is the only anchor" rule holds. `dayNet` stops being
 * a special case (1D's base is `last_equity` only because Alpaca publishes no 1D curve), the windows
 * compose again, and NET shares one derivation with the EQUITY headline and with LIQUIDATION.
 *
 * NEVER DERIVE THE END FROM `points[]`. The curve's last sample is the stale thing being fixed, and its
 * per-point `pnl` is a per-sample delta rather than a running total (a live case: Alpaca's last 1M
 * `profit_loss` read +1,161.81 on a month that was DOWN 2,244.29). Live equity or nothing.
 *
 * UNKNOWN IS AN ANSWER. With no live equity the broker's to-last-close `pnl` stands in — a node with no
 * broker account still has a curve, and `headlineEquity` next door makes exactly the same trade — and
 * with neither, this returns `null` and the caller renders "—". It must NEVER fall back to unrealized:
 * that fallback IS #336, and a plausible wrong number in the hero slot is worse than an honest blank,
 * because nobody checks a number that looks reasonable.
 */

/** One period's curve as published on the `equity_curve` plane. */
export interface PeriodCurve {
  /** The broker's equity at the instant BEFORE the window — its own field, and the window's anchor. */
  base_value?: number | null;
  /** The broker's P&L for the window, measured to its last history point. Fallback only. */
  pnl?: number | null;
  /** Whether the series actually spans the labelled window (#653) — absent on frames predating it. */
  covered?: boolean | null;
  /** The series' own span in days, engine-stamped; null for inception-to-date. */
  covers_days?: number | null;
}

export interface EquityCurveFrame {
  curves?: Record<string, PeriodCurve | null | undefined> | null;
}

/** The broker's own account fields. Both are Alpaca's, so their difference is Alpaca's P&L. */
export interface AccountLike {
  equity?: number | null;
  last_equity?: number | null;
}

/**
 * `value` when it is a real number, else null.
 *
 * `Number.isFinite` rather than `!= null`: JSON carries `NaN`/`Infinity` through some encoders as real
 * numbers, and a NaN reaching the hero slot renders "$NaN" — which reads as a broken tile rather than as
 * missing data. Both non-finite cases collapse to the same honest `null`.
 */
function finite(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * The window's opening equity, per the broker.
 *
 * ZERO IS MISSING, NOT A BASE. `exec_client` writes `float(raw.get("base_value") or 0.0)`, so an absent
 * base arrives as `0.0` rather than as null. Subtracting that from live equity would report the ENTIRE
 * account balance as the window's profit — the most dangerous way this can fail, and the only reason
 * this helper exists rather than an inline `finite()`.
 *
 * 1D has no curve of its own (measured 2026-08-18: the broker publishes 1W/1M/3M/all and nothing for 1D),
 * so it anchors on `last_equity` — the PREVIOUS SESSION'S CLOSING equity, which is the same fact a 1D
 * curve's `base_value` would carry. A published 1D curve still wins if one ever appears.
 *
 * Anchoring the day on the previous close means NET(1D) counts the overnight gap, so it can be non-zero
 * before the opening bell. That is deliberate and correct — the gap is real money that moved while the
 * book was held — even though it looks wrong to anyone assuming it measures only regular-hours movement.
 */
/** EXPORTED so the `%` toggle takes its denominator from the SAME derivation NET is built from
 * (#586). A second "equity at the window's start" computed in the tile would be two derivations of
 * one fact, and this file already documents what that costs. */
export function baseValue(curve: PeriodCurve | null | undefined, period: string, account: AccountLike | undefined): number | null {
  const published = finite(curve?.base_value);
  if (published !== null && published !== 0) return published;
  return period === "1D" ? finite(account?.last_equity) : null;
}

/**
 * The broker's P&L for `period` measured to NOW, or `null` when it cannot be known.
 */
export function periodNet(
  frame: EquityCurveFrame | undefined,
  period: string,
  account?: AccountLike,
): number | null {
  const curve = frame?.curves?.[period];
  const live = finite(account?.equity);
  const base = baseValue(curve, period, account);
  if (live !== null && base !== null) return live - base;
  // No live equity: the broker's own to-last-close figure is the best remaining answer. Stale by up to a
  // session, but it is still the broker's, and it is the only figure a non-broker node has at all.
  return finite(curve?.pnl);
}

/**
 * NET for 1D on its own — the broker's day P&L, `equity - last_equity`.
 *
 * Kept as a named export because "today" is asked for outside the period selector, and because both
 * fields come from the broker: their difference IS Alpaca's day P&L, the same number Alpaca's own UI
 * shows, rather than a locally-invented figure.
 *
 * Returns null rather than 0 when either field is missing: a node with no broker account must say
 * "unknown", never claim the day was flat.
 */
export function dayNet(account: AccountLike | undefined): number | null {
  const equity = finite(account?.equity);
  const prior = finite(account?.last_equity);
  if (equity === null || prior === null) return null;
  return equity - prior;
}

/**
 * Label for the NET figure, so the hero number states which window it answers for.
 *
 * The period was invisible on the NET row before, which is how "+$2,841 this 1W" came to sit under a NET
 * of $1,061 with nothing on screen explaining that they answered different questions.
 */
export function periodNetLabel(period: string): string {
  // "Delta net", not "Net" (#654): the figure is realized + change in unrealized over the window —
  // a CHANGE — rendered beside CASH · NOW and LIQUIDATION · NOW, which are levels. The name says so.
  return period === "all" ? "Delta net · account" : `Delta net · ${period}`;
}


/**
 * Coverage of the selected window (#653): does the curve's series actually span the label?
 *
 * Staging, 2026-08-28: NET·3M == NET·1M to the cent on a ~2-week account — every longer window's
 * base clamped to inception, nothing on screen saying so. The number is still a true delta; the
 * LABEL was the lie. `null` = the frame predates the field (an old engine has no opinion): absence
 * must not be read as "fully covered", so the caller renders nothing rather than a claim.
 */
export function periodNetCoverage(
  frame: EquityCurveFrame | null | undefined,
  period: string,
): { covered: boolean; coversDays: number | null } | null {
  const curve = frame?.curves?.[period === "1D" ? "1D" : period];
  if (!curve || typeof curve.covered !== "boolean") return null;
  return { covered: curve.covered, coversDays: finite(curve.covers_days ?? null) };
}
