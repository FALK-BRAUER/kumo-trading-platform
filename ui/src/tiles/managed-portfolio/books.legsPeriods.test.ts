/**
 * #846 — the period figure now comes from Nautilus's own closed legs on every venue, and the panel
 * must say two things it never had to before:
 *
 *   1. HOW FAR BACK it can see. `all` is not account inception — it is the oldest leg this cache
 *      holds (staging2: 2026-09-03). A truncated figure under the label "all" is degrading quietly.
 *   2. WHAT THE BROKER SAYS, where a broker sweep exists (Alpaca). The sweep reaches inception and
 *      carries fees and withholding the legs cannot see, so its total will differ. Both are shown;
 *      neither is averaged into the other — the two disagreeing is the detector that found #846.
 *
 * `unmatched` keeps its name because the tile reads it, but its meaning widened: for the legs it
 * counts partial exits on still-open positions. The docstring in books.ts says so.
 */
import { describe, expect, it } from "vitest";
import { periodBrokerRealized, periodHorizonTs, periodRealized, periodRealizedPartial } from "./books";

const periods = {
  "1D": { total: 571.92, unmatched: 0, by_strategy: { "MANUAL-001": 571.92 }, unclaimed: 0, horizon_ts: 1_756_900_000_000_000_000 },
  all: { total: 2416.97, unmatched: 1, by_strategy: { "MANUAL-001": 2416.97 }, unclaimed: 0, horizon_ts: 1_756_900_000_000_000_000 },
  // The engine stamps the derivation beside the windows; the tile must tolerate a non-window key.
  source: "legs",
};
const frame = {
  realized_periods: periods,
  realized_periods_swept: {
    // `total` is FILLS ONLY by pinned design (test_total_KEEPS_its_meaning); `net` carries the fees and
    // withholding the legs cannot see — that is the broker figure worth showing beside the legs'.
    all: { total: 3530.66, net: 2531.57, unmatched: 0 },
  },
} as never;

describe("legs-derived periods on the panel (#846)", () => {
  it("the headline still reads realized_periods[p].total — the legs figure", () => {
    expect(periodRealized(frame, "all", 0)).toBe(2416.97);
  });

  it("every window says how far back it can see", () => {
    expect(periodHorizonTs(frame, "all")).toBe(1_756_900_000_000_000_000);
    expect(periodHorizonTs(frame, "1D")).toBe(1_756_900_000_000_000_000);
    expect(periodHorizonTs({ realized_periods: { all: { total: 1 } } } as never, "all")).toBeNull();
  });

  it("the broker's own figure is readable beside the legs' where a sweep exists — and absent where none does", () => {
    expect(periodBrokerRealized(frame, "all")).toBe(2531.57);
    expect(periodBrokerRealized(frame, "1D")).toBeNull();
    expect(periodBrokerRealized({ realized_periods: periods } as never, "all")).toBeNull();
  });

  it("the partial marker still fires off `unmatched`, now meaning partial exits on open positions", () => {
    expect(periodRealizedPartial(frame, "all")).toEqual({ partial: true, unmatched: 1 });
    expect(periodRealizedPartial(frame, "1D")).toEqual({ partial: false, unmatched: 0 });
  });

  it("a window that could not be computed renders as unknown, never as $0.00", () => {
    // `empty_windows(error)` used to carry `total: 0.0`; the tile read `total` and nothing read `error`,
    // so a mixed-currency refusal rendered as a confident zero on every window (impl review, #846).
    const refused = { realized_periods: { "1D": { total: null, error: "mixed settlement currencies: SGD, USD" }, all: { total: null, error: "x" } } } as never;
    expect(periodRealized(refused, "all", 999)).toBeNull();
    expect(periodRealized(refused, "1D", 999)).toBeNull();
    const zeroForReal = { realized_periods: { all: { total: 0, error: null } } } as never;
    expect(periodRealized(zeroForReal, "all", 999)).toBe(0);
  });
});
