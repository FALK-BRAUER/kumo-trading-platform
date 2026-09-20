/**
 * The per-strategy rows must SUM TO THE TOTAL (#596).
 *
 * Operator, twice: "we have a strategy tile for uclaimed so both realised and unrealised need to include
 * unclaimed", then "as I tood you: books.ts drops EXTERNAL from the per-strategy cells (-1,093.30)
 * ... need to include".
 *
 * THE DEFECT. `visibleBooks` drops EXTERNAL — correctly, it is not a strategy and must not grow a
 * second row — and the Unclaimed row then looked itself up by its own LABEL. `by_strategy["Unclaimed"]`
 * does not exist, so `strategyPeriodRealized` took its "the window was swept and this strategy is
 * simply not in it — a genuine zero" branch and the money left the panel.
 *
 * Measured on an Alpaca paper instance 2026-08-27, the ALL window:
 *
 *     MOMENTUM-002   3246.87
 *     MANUAL-001     1522.07
 *     EXTERNAL      -1093.30   <- rendered nowhere
 *     TECHIVOL-005    -77.59
 *     BCTROT-004      -67.53
 *     unclaimed         0.14
 *     total          3530.66   <- includes the -1093.30 the rows left out
 *
 * The cells summed to 4623.82 against a total of 3530.66. The TOTAL was right the whole time; only
 * the breakdown lied — which is the harder failure to spot, because the hero number reconciles.
 *
 * This is the same family as #310, where DEPLOYED + CASH did not equal LIQUIDATION: a panel that
 * shows parts and a whole owes the reader that they agree.
 */

import { describe, expect, it } from "vitest";

import { rowPeriodRealized, strategyPeriodRealized, unclaimedPeriodRealized } from "@/tiles/managed-portfolio/books";

/** Exactly what the engine published for the ALL window on 2026-08-27. */
const FRAME = {
  realized_periods: {
    all: {
      total: 3530.66,
      unclaimed: 0.14,
      by_strategy: {
        "MOMENTUM-002": 3246.87,
        "MANUAL-001": 1522.07,
        EXTERNAL: -1093.3,
        "TECHIVOL-005": -77.59,
        "BCTROT-004": -67.53,
      },
    },
  },
};

/** The labels the panel actually renders: strategies with cycles, plus one "Unclaimed" row. */
const RENDERED = ["MOMENTUM-002", "MANUAL-001", "TECHIVOL-005", "BCTROT-004", "Unclaimed"];

/**
 * THE PRODUCTION ROUTING, not a re-statement of it. This used to hand-replicate the ternary — a THIRD
 * derivation, in test code, of the thing the tiles had just been unified onto. A test that reimplements
 * what it is checking passes when both copies are wrong together, which is the one case that matters.
 */
function renderedRealized(label: string): number | null {
  return rowPeriodRealized(FRAME, "all", label);
}

describe("the fixture can express the defect", () => {
  it("EXTERNAL is in the engine's map but is NOT a rendered label", () => {
    // Fixture property first. If EXTERNAL had its own row, or were absent from the map, the sum
    // below would balance either way and the test would pin nothing.
    expect(FRAME.realized_periods.all.by_strategy.EXTERNAL).toBe(-1093.3);
    expect(RENDERED).not.toContain("EXTERNAL");
  });

  it("EXTERNAL is large enough to be visible in the sum", () => {
    // A near-zero EXTERNAL would make the invariance hold whether or not it is included.
    expect(Math.abs(FRAME.realized_periods.all.by_strategy.EXTERNAL)).toBeGreaterThan(100);
  });
});

describe("the rows add up", () => {
  it("sum of rendered rows equals the published total", () => {
    const sum = RENDERED.reduce((acc, label) => acc + (renderedRealized(label) ?? 0), 0);
    expect(sum).toBeCloseTo(FRAME.realized_periods.all.total, 2);
  });

  it("the Unclaimed row carries EXTERNAL plus the sweep's own residual", () => {
    // BOTH terms: `unclaimed` is the sweep's residual, EXTERNAL is activity attributed to no
    // strategy. Different quantities, and the row owes the reader both.
    expect(unclaimedPeriodRealized(FRAME, "all")).toBeCloseTo(-1093.3 + 0.14, 2);
  });

  it("looking the row up by its LABEL is what lost the money", () => {
    // The exact call the tile used to make. Kept as an assertion so the regression is named rather
    // than merely prevented.
    expect(strategyPeriodRealized(FRAME, "all", "Unclaimed")).toBe(0);
  });
});

describe("unknown stays unknown", () => {
  it("an unswept window is null, not zero", () => {
    expect(unclaimedPeriodRealized({ realized_periods: { all: {} } }, "all")).toBeNull();
    expect(unclaimedPeriodRealized(undefined, "all")).toBeNull();
  });

  it("either term alone still yields a number", () => {
    expect(unclaimedPeriodRealized({ realized_periods: { all: { unclaimed: 5 } } }, "all")).toBe(5);
    expect(
      unclaimedPeriodRealized({ realized_periods: { all: { by_strategy: { EXTERNAL: -7 } } } }, "all"),
    ).toBe(-7);
  });
});
