import { describe, expect, it } from "vitest";

import { computePnl } from "./instrument";

describe("computePnl basis guards", () => {
  it("does not render Infinity% when the entry price is zero", () => {
    // Reachable: a reconciled position whose opening fill the cache never saw, or a broker report
    // with the field absent. The AMOUNT is still meaningful — only the percentage needs a basis.
    const pos = { side: "LONG", avg_px_open: 0, quantity: 100 } as never;
    const { pct, amt } = computePnl(pos, 10);
    expect(Number.isFinite(pct)).toBe(true);
    expect(pct).toBe(0);
    expect(amt).toBe(1000);
  });

  it("still reports both when the basis is normal", () => {
    const pos = { side: "LONG", avg_px_open: 100, quantity: 10 } as never;
    const { pct, amt } = computePnl(pos, 110);
    expect(pct).toBeCloseTo(10);
    expect(amt).toBeCloseTo(100);
  });

  it("negates the percentage for a short", () => {
    const pos = { side: "SHORT", avg_px_open: 100, quantity: 10 } as never;
    expect(computePnl(pos, 110).pct).toBeCloseTo(-10);
  });
});
