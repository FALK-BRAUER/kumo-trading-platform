import { describe, it, expect } from "vitest";
import { marketSession, isUsMarketOpen } from "./market";
import { hasTradedToday, priceState } from "./priceState";

/** 2026-08-19 08:15 ET — the incident. Volume 0 all premarket; 1 lot bid vs 570 lots ask. */
const AMGN = {
  price: 431.0,
  todayRange: { high: 0, low: 0, prevClose: 425.28 },
  quote: { bid: 427.0, ask: 431.0, bid_size: 1, ask_size: 570 },
};

describe("R1 — a percentage requires a TRADE (#355)", () => {
  it("refuses the +1.34% that started this", () => {
    // The number that reached the entry rules. `price` here is the ASK: 431.00 against a 425.28 close is
    // +1.34%, and it pointed at lifting a 570-lot offer with one lot on the bid — the exact gap-up chase
    // "Pre-market up >1%? → Gap-up" exists to prevent. The tape disagreed: AMGN closed -0.39% into it.
    const s = priceState({ ...AMGN, session: "PRE" });
    expect(s.pct).toBeNull();
    expect(s.kind).toBe("quote");
    // and the naive computation, as the control — this is what shipped
    expect(((431.0 - 425.28) / 425.28) * 100).toBeCloseTo(1.345, 2);
  });

  it("still quotes a REAL premarket trade — refusing those would be the opposite error", () => {
    const s = priceState({
      price: 430.0,
      session: "PRE",
      todayRange: { high: 431, low: 428, prevClose: 425.28 },
      quote: AMGN.quote,
    });
    expect(s.kind).toBe("traded");
    expect(s.pct).toBeCloseTo(1.11, 2);
    expect(s.label).toBe("Pre-market");
  });

  it("never renders an untraded name as flat", () => {
    // VCTR, 2026-08-19 08:49 ET: last trade 20:00 the PREVIOUS day, "current price" 117.77 carried
    // forward, intraday P&L exactly +0.00 — rendered as FLAT when the truth was UNPRICED. The range is
    // DELIVERED and degenerate here, which is how we know it did not trade.
    const s = priceState({ price: 117.77, session: "PRE", todayRange: { high: 0, low: 0, prevClose: 117.77 } });
    expect(s.pct).toBeNull();
    expect(s.kind).toBe("closed");
    expect(s.price).toBe(117.77);
  });

  it("makes NO claim when the range was never delivered", () => {
    // MY OWN REGRESSION, CAUGHT LIVE. The `today_ranges` plane is empty for every held symbol (#345
    // item 6), and the first version treated "absent" exactly like "degenerate" — so at 07:01 ET the
    // Portfolio showed "Prior close" under BDX at 187.87 and WHD at 72.99 while the marks were moving
    // and equity had shifted $135. Asserting a name had not traded when it demonstrably had is the same
    // false certainty this module exists to prevent, produced by the module itself.
    const s = priceState({ price: 187.87, session: "PRE", todayRange: { prevClose: 186.0 } });
    expect(s.kind).toBe("unknown");
    expect(s.label).toBe("");     // no claim — the price renders bare
    expect(s.price).toBe(187.87); // still shown; it is the number we hold
    expect(s.pct).toBeNull();     // R1 holds: no trade established, no percentage
  });
});

describe("R2 — provenance is text, not colour", () => {
  it("names every state in words", () => {
    expect(priceState({ ...AMGN, session: "PRE" }).label).toBe("Quote only");
    // "Prior close" requires a DELIVERED range that says nothing printed — absent is a different state.
    expect(priceState({ price: 10, session: "CLOSED", todayRange: { high: 0, low: 0, prevClose: 9 } }).label)
      .toBe("Prior close");
    expect(priceState({ price: 10, session: "CLOSED", todayRange: { prevClose: 9 } }).label).toBe("");
    expect(priceState({ session: "CLOSED" }).label).toBe("No price");
    expect(priceState({ price: 10, session: "OPEN", todayRange: { high: 11, low: 9, prevClose: 9 } }).label).toBe("Traded");
    expect(priceState({ price: 10, session: "AFTER", todayRange: { high: 11, low: 9, prevClose: 9 } }).label).toBe("After hours");
  });
});

describe("R3 — a quote-only price is a range", () => {
  it("carries both sides, both sizes and the spread", () => {
    const s = priceState({ ...AMGN, session: "PRE" });
    expect([s.bid, s.ask]).toEqual([427.0, 431.0]);
    expect([s.bidSize, s.askSize]).toEqual([1, 570]);
    expect(s.spreadPct).toBeCloseTo(0.93, 2); // the issue's own figure
    expect(s.price).toBeNull(); // there is no single number to print
  });

  it("falls back to the close rather than half a quote", () => {
    const s = priceState({ price: 100, session: "PRE", todayRange: { high: 0, low: 0 }, quote: { bid: 99 } });
    expect(s.kind).toBe("closed");
    expect(s.bid).toBeNull();
  });
});

describe("hasTradedToday", () => {
  it("reads a degenerate 0/0 range as no trade", () => {
    expect(hasTradedToday({ high: 0, low: 0 })).toBe(false);
    expect(hasTradedToday({})).toBe(false);
    expect(hasTradedToday(null)).toBe(false);
    expect(hasTradedToday({ high: 431, low: 428 })).toBe(true);
  });
});

describe("the session clock has ONE derivation", () => {
  const et = (h: number, m: number, day = 20) =>
    Date.parse(`2026-08-${day}T${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:00-04:00`);

  it("names each window", () => {
    expect(marketSession(et(3, 59))).toBe("CLOSED");
    expect(marketSession(et(4, 0))).toBe("PRE");
    expect(marketSession(et(9, 29))).toBe("PRE");
    expect(marketSession(et(9, 30))).toBe("OPEN");
    expect(marketSession(et(15, 59))).toBe("OPEN");
    expect(marketSession(et(16, 0))).toBe("AFTER");
    expect(marketSession(et(19, 59))).toBe("AFTER");
    expect(marketSession(et(20, 0))).toBe("CLOSED");
    expect(marketSession(et(12, 0, 22))).toBe("CLOSED"); // Saturday
  });

  it("keeps isUsMarketOpen and marketSession the SAME predicate", () => {
    // Not two clocks. The order path and the health banner gate on `isUsMarketOpen` while the tiles label
    // with `marketSession`; if these could drift, one screen would say PRE while another accepted a
    // market order. Derived, so they cannot.
    for (let h = 0; h < 24; h++) {
      for (const m of [0, 29, 30, 31, 59]) {
        expect(isUsMarketOpen(et(h, m))).toBe(marketSession(et(h, m)) === "OPEN");
      }
    }
  });
});

describe("the trap that a missing today-range creates", () => {
  it("a caller that omits high/low can NEVER report a trade — so callers must supply it", () => {
    // FOUND IN THE RENDERED ROW, NOT BY A TEST. The Portfolio wiring first passed only `prevClose`, so
    // `hasTradedToday` was unconditionally false and every held position would have read "Prior close"
    // straight through the regular session — mislabelling a genuinely traded price, which is the mirror
    // image of the bug this module exists to prevent.
    //
    // Pinned as a property of the CONTRACT: with no range, the answer is never "traded", whatever the
    // session and whatever the price. Anyone wiring a third tile hits this test rather than the screen.
    for (const session of ["PRE", "OPEN", "AFTER", "CLOSED"] as const) {
      const s = priceState({ price: 100, session, todayRange: { prevClose: 99 } });
      expect(s.kind).not.toBe("traded");
      expect(s.pct).toBeNull();
    }
    // and WITH a range it reports the trade, so the assertion above is about the missing range and not
    // about something else being broken
    expect(priceState({ price: 100, session: "OPEN", todayRange: { high: 101, low: 98, prevClose: 99 } }).kind)
      .toBe("traded");
  });
})
