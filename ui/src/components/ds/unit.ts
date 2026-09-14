/** The `$` / `%` unit toggle's vocabulary and arithmetic (#586; the rule is #392).
 *
 * Operator, 2026-09-06: *"it should not add %. it should switch all relative change fields to %."*
 *
 * SWITCH, NEVER ADD. A percentage rendered beside a dollar figure is a second token on every row, and
 * #294 spent a full day buying that width back after `toFixed(2)` pushed the trend strip past a 390px
 * viewport — `pctLabel` exists because of it. Switching costs zero width, and on the Portfolio row,
 * which renders `+$32 +1.7%` together today (`tiles/portfolio/PortfolioTile.tsx:85`), it gives width
 * back. That is why this ticket stopped being blocked on the width question.
 *
 * A plain module, not a `.tsx`: the vocabulary is shared and the toggle is one renderer of it — the
 * same split `periods.ts` makes, and for the same reason (a `.ts` test can import it without dragging
 * JSX through the parser).
 */

/** The two states. `$` is the default because every figure was a dollar figure before this existed. */
export type Unit = "$" | "%";

export const UNITS: ReadonlyArray<[key: Unit, label: string]> = [
  ["$", "$"],
  ["%", "%"],
];

/** Which denominator a figure's percentage is taken against — the whole design decision (#586).
 *
 * `none` means the figure NEVER switches: it is a balance, not a return, and "% of what" has no answer
 * that is not circular.
 */
export type Denominator = "windowBase" | "costBasis" | "deployed" | "equity" | "sleeve" | "none";

/** THE DECISION, in one place, so the panel cannot drift from the ticket that made it.
 *
 * A figure absent from this table is a figure whose unit behaviour nobody decided — which is how a
 * panel ends up picking a denominator by accident. `unit.test.ts` pins every key the Book panel
 * renders, so adding a figure without deciding its denominator fails rather than defaults.
 */
export const BOOK_FIGURES: Readonly<Record<string, Denominator>> = {
  // The three window figures share ONE base, which is what makes the stated identity
  // "NET = REALIZED + Δ UNREALIZED" survive the switch: three shares of one denominator still sum.
  net: "windowBase",
  realized: "windowBase",
  dUnrealized: "windowBase",
  // A return on the TRADE, not on the window — this is gain accrued before the window too.
  standingUnrealized: "costBasis",
  // "How much of the book is protected", which is a share of what is deployed, not of the account.
  secured: "deployed",
  // Already dual today (`$60,654.49` / `58% of liq`). It keeps both; only which one leads swaps, so
  // the same two facts stay on screen in either state.
  deployed: "equity",
  // Balances. Absolute in both states.
  cash: "none",
  liquidation: "none",
  // A lane card is about the LANE. QC345-003's $1,144.60 is +5.7% on its 20,000 sleeve and +1.1% on
  // the 104,777 account: the first is the lane's performance, the second its contribution to the
  // portfolio. The allocation is already in `settings/strategies`.
  strategyValue: "sleeve",
  strategyRealized: "sleeve",
};

/** `value` as a percentage of `denominator`, or null when no percentage can honestly be formed.
 *
 * REFUSES RATHER THAN INVENTS. `Infinity%` is not hypothetical — `computePnl` guards `avg_px_open`
 * for exactly this reason (`lib/framework/instrument.ts:247-254`): a reconciled position whose opening
 * fill the cache never saw has a zero basis. A sleeve of 0 is the same shape. #586: render `—`, never
 * a division by zero and never a silent fall back to a different denominator, because a percentage of
 * the wrong base is worse than no percentage.
 *
 * A NULL VALUE STAYS NULL. REALIZED is legitimately unknown when the broker has not swept the window,
 * and the panel says so in three places. Rendering that as `0.0%` would turn "not asked" into "zero".
 */
export function percentOf(
  value: number | null | undefined,
  denominator: number | null | undefined,
): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  // `> 0` rather than `!= 0`: a share of a negative base is not a return, and it flips the sign of
  // every figure taken against it.
  if (denominator == null || !Number.isFinite(denominator) || denominator <= 0) return null;
  return (value / denominator) * 100;
}

/** One decimal and an explicit sign, so `+` and `−` occupy the same width as the `$` form.
 *
 * ONE decimal, not two: two is precisely what cost #294 a day of row width.
 */
export function fmtPct(n: number): string {
  return `${n >= 0 ? "+" : "-"}${Math.abs(n).toFixed(1)}%`;
}

/** Dollars, two decimals, sign outside the `$`.
 *
 * CANONICAL rather than copied. This exact function was defined character-for-character in both
 * `BookTile.tsx` and `ManagedPortfolioTile.tsx` — the two tiles that render a lane cell — which is the
 * same duplication that let one of them miss the unit toggle for eight days (#392). `EquityTile` keeps
 * its own deliberately different one (no decimals, for a six-figure balance in a hero slot).
 */
export function fmtUsd(n: number): string {
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * ONE LANE FIGURE, IN THE SELECTED UNIT (#392, rule from #586).
 *
 * Operator, 2026-09-14, reading the same lane on two tabs: *"2 tabs disagree on momentum p&l"*. They did
 * not — Home rendered `-0.0%` and Portfolio `-$12.87`, which are the same number, because the Portfolio
 * cell never read the toggle. The two cells each carried their own copy of this three-line decision;
 * now there is one, and `laneCellUnitSwitch.test.ts` holds every lane cell to it.
 *
 * THREE ANSWERS, AND THE THIRD IS NOT A NUMBER. `null` in means unknown (an unswept window, an
 * unpriced leg) and stays unknown. A sleeve that is missing or zero yields `—` rather than a
 * percentage of some other denominator: `percentOf` refuses, and a percentage of the wrong base is
 * worse than no percentage — see its own note.
 */
export function laneFigure(
  value: number | null,
  unit: Unit,
  sleeve: number | null | undefined,
): string {
  if (value === null) return "—";
  if (unit === "$") return fmtUsd(value);
  const pct = percentOf(value, sleeve);
  return pct === null ? "—" : fmtPct(pct);
}
