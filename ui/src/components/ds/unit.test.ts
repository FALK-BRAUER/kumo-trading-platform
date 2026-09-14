/**
 * The $ / % toggle switches a relative-change field's UNIT (#586, rule in #392).
 *
 * Operator, 2026-09-06: "it should not add %. it should switch all relative change fields to %."
 *
 * That is the whole design. A percentage rendered BESIDE a dollar figure is a second token per row,
 * and #294 spent a full day buying that width back after `toFixed(2)` pushed the trend strip past a
 * 390px viewport. Switching costs nothing — and on the Portfolio row, which already renders
 * `+$32 +1.7%` together, it gives width back.
 *
 * THE DENOMINATOR IS THE DESIGN. Each figure that switches has exactly one defensible denominator;
 * the rest have none and forcing a percentage onto them would invent a number. `BOOK_FIGURES` is the
 * single place that decision lives, so the panel cannot drift from the ticket that decided it.
 */
import { describe, expect, it } from "vitest";

import { BOOK_FIGURES, fmtPct, percentOf } from "./unit";

describe("percentOf", () => {
  it("is the value as a share of its denominator", () => {
    expect(percentOf(1144.6, 20000)).toBeCloseTo(5.723, 3);
  });

  it("keeps the sign of the VALUE, not of the denominator", () => {
    expect(percentOf(-269.58, 104777)).toBeLessThan(0);
  });

  it("REFUSES a denominator that cannot support a percentage, rather than printing one", () => {
    // `Infinity%` is reachable and has shipped before: `computePnl` guards `avg_px_open` for exactly
    // this reason (`lib/framework/instrument.ts:247-254`) — a reconciled position whose opening fill
    // the cache never saw has a zero basis. A sleeve of 0 is the same shape: #586 says render `—`,
    // never a division by zero and never a silent fall back to a different denominator.
    for (const bad of [0, null, undefined, NaN, Infinity, -Infinity]) {
      expect(percentOf(100, bad as number | null | undefined)).toBeNull();
    }
  });

  it("REFUSES a negative denominator — a share of a negative base is not a return", () => {
    expect(percentOf(100, -5000)).toBeNull();
  });

  it("is null when the VALUE itself is unknown, so `—` propagates rather than becoming 0%", () => {
    // REALIZED is legitimately null when the broker has not swept the window ("unknown, not zero" —
    // the panel says so in three places). A null must not render as 0.0%.
    expect(percentOf(null, 20000)).toBeNull();
  });

  it("is null for a NON-FINITE value too, not just a null one", () => {
    // The bite that survived the first round: dropping `!Number.isFinite(value)` from the guard killed
    // no test, because every case here passed `null`. A derived figure CAN arrive NaN — Δ UNREALIZED is
    // `net - realized` when the broker publishes no measured term, and DEPLOYED/equity ratios divide —
    // and `NaN%` renders as literally "NaN%". A value that is not a number is not a percentage.
    for (const bad of [NaN, Infinity, -Infinity]) {
      expect(percentOf(bad, 20000)).toBeNull();
    }
  });
});

describe("fmtPct", () => {
  it("carries an explicit sign, so + and − read the same width as the $ form", () => {
    expect(fmtPct(5.723)).toBe("+5.7%");
    expect(fmtPct(-1.34)).toBe("-1.3%");
    expect(fmtPct(0)).toBe("+0.0%");
  });

  it("uses ONE decimal — two is what cost #294 a day of row width", () => {
    expect(fmtPct(12.3456)).toBe("+12.3%");
  });
});

describe("BOOK_FIGURES — the denominator table decided in #586", () => {
  it("routes each window figure to the equity at the window's START", () => {
    // NET, REALIZED and Δ UNREALIZED are all shares of the same base, which is what makes
    // "NET = REALIZED + Δ" survive the switch: three percentages of one denominator still sum.
    expect(BOOK_FIGURES.net).toBe("windowBase");
    expect(BOOK_FIGURES.realized).toBe("windowBase");
    expect(BOOK_FIGURES.dUnrealized).toBe("windowBase");
  });

  it("puts standing unrealized on COST BASIS — it is a return on the trade, not on the window", () => {
    expect(BOOK_FIGURES.standingUnrealized).toBe("costBasis");
  });

  it("puts SECURED over DEPLOYED — 'how much of the book is protected'", () => {
    expect(BOOK_FIGURES.secured).toBe("deployed");
  });

  it("puts a lane's figures over its SLEEVE, never the account", () => {
    // QC345-003's $1,144.60 is +5.7% on a 20,000 sleeve and +1.1% on the 104,777 account. The first
    // is the lane's performance, the second its contribution to the portfolio. A lane card is about
    // the lane.
    expect(BOOK_FIGURES.strategyValue).toBe("sleeve");
    expect(BOOK_FIGURES.strategyRealized).toBe("sleeve");
  });

  it("leaves BALANCES absolute in both states — they are not returns", () => {
    // "% of what" has no answer here that is not circular. Held counts are already counts.
    expect(BOOK_FIGURES.cash).toBe("none");
    expect(BOOK_FIGURES.liquidation).toBe("none");
  });

  it("names every figure the panel renders, so a new one cannot default to a denominator", () => {
    // FIXTURE PROPERTY: the table is the decision. A figure absent from it is a figure whose unit
    // behaviour nobody decided, and the panel would silently pick one.
    for (const key of ["net", "realized", "dUnrealized", "standingUnrealized", "secured",
                       "deployed", "cash", "liquidation", "strategyValue", "strategyRealized"]) {
      expect(BOOK_FIGURES[key], `${key} has no decided denominator`).toBeDefined();
    }
  });
});
