import { describe, it, expect } from "vitest";

import { headlineEquity } from "./headline";

/**
 * #336 — the EQUITY headline presented a DIFFERENT "current" equity per window.
 *
 * Measured 2026-08-18 11:17 SGT: $100,679 at 1W, $100,116 at 1M and 3M, while LIQUIDATION on the panel
 * beside it read $100,658.95 from the broker. Current equity is one number; only the delta is per-window.
 */

// The live broker account that morning — the same field LIQUIDATION renders.
const ACCOUNT = { equity: 100_658.95 };

describe("EQUITY headline (#336)", () => {
  it("is the broker's equity regardless of which window is selected", () => {
    // The curve's last point differs per window; none of them may win over the broker.
    for (const curveLast of [100_679, 100_116, 97_755.71]) {
      expect(headlineEquity(ACCOUNT, curveLast)).toBe(100_658.95);
    }
  });

  it("equals what LIQUIDATION shows — one fact, one number", () => {
    // Both read `account.equity`. Pinned because they sit inches apart on Home, and a reader who spots
    // them disagreeing has no way to tell which is lying. This is the same disagreement-as-detector rule
    // that caught the DEPLOYED/CASH/LIQUIDATION mismatch in #310.
    const liquidation = ACCOUNT.equity;
    expect(headlineEquity(ACCOUNT, 100_116)).toBe(liquidation);
  });

  it("falls back to the curve only when the broker offers nothing", () => {
    // A synthetic or non-Alpaca node has no account snapshot; a slightly stale figure beats none.
    expect(headlineEquity(undefined, 100_116)).toBe(100_116);
    expect(headlineEquity({}, 100_116)).toBe(100_116);
    expect(headlineEquity({ equity: null }, 100_116)).toBe(100_116);
  });

  it("renders nothing rather than zero when neither source has arrived", () => {
    // An account worth $0.00 is a very different claim from an account whose equity has not loaded.
    expect(headlineEquity(undefined, null)).toBeNull();
    expect(headlineEquity({}, null)).toBeNull();
  });

  it("a genuinely zero equity survives — it is a real value, not a missing one", () => {
    expect(headlineEquity({ equity: 0 }, 100_116)).toBe(0);
  });

  it("a non-finite equity falls through to the curve", () => {
    // "$NaN" in the headline reads as a broken tile and sends the reader hunting a rendering fault.
    expect(headlineEquity({ equity: NaN }, 100_116)).toBe(100_116);
    expect(headlineEquity({ equity: Infinity }, 100_116)).toBe(100_116);
    expect(headlineEquity({ equity: NaN }, NaN)).toBeNull();
  });
});
