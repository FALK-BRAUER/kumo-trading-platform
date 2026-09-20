import { describe, expect, it } from "vitest";
import { atr, computePrefill, sizePosition } from "./prefill";
import type { OrderProfile, SizingParams } from "@/config/orderTicket";
import type { BarDTO } from "@/lib/api/types";

const bar = (close: number, high = close + 0.5, low = close - 0.5): BarDTO => ({
  instrument_id: "T.XNAS",
  open: close,
  high,
  low,
  close,
  volume: 1000,
  ts_event: 0,
});

// N bars from a close series (ascending index = older→newer, matching lastLevels/atr).
const series = (closes: number[], hl = 0.5): BarDTO[] => closes.map((c) => bar(c, c + hl, c - hl));

const SIZING: SizingParams = {
  riskMode: "fraction_of_equity",
  riskUsd: 500,
  riskFraction: 0.005, // 0.5%
  maxPositionFraction: 0.1, // 10%
  minShares: 1,
  maxShares: 10_000,
};

const profile = (over: Partial<OrderProfile> = {}): OrderProfile => ({
  label: "test",
  entry: { source: "last", offsetTicks: 0, maxSpreadPct: 0.005 },
  stop: { model: "atr", atrLookback: 14, atrMult: 1.0, percentStop: 0.02, ichimokuRef: "kijun" },
  target: { model: "r_multiple", targetR: 2, percentTarget: 0.04 },
  sizing: SIZING,
  defaultOrderType: "limit",
  defaultTif: "day",
  ...over,
});

describe("atr", () => {
  it("is null until n+1 bars", () => {
    expect(atr(series([1, 2, 3]), 14)).toBeNull();
  });
  it("averages true range", () => {
    // constant 1.0 high-low, no gaps → ATR = 1.0
    const bars = series(Array.from({ length: 20 }, () => 100), 0.5);
    expect(atr(bars, 14)).toBeCloseTo(1.0, 6);
  });
});

describe("sizePosition (the capped path)", () => {
  it("risks the budget: fraction of equity / risk-per-share", () => {
    // budget = 0.5% * 100k = 500; rps = 2 → 250 raw, but max-position (10% of 100k / 100 = 100) caps it
    const r = sizePosition(100, 98, "BUY", SIZING, 100_000, 400_000);
    expect(r.shares).toBe(100);
    expect(r.notes).toContain("capped by max position %");
  });

  it("respects buying-power affordability on a BUY", () => {
    const loose = { ...SIZING, maxPositionFraction: 1.0 }; // 1000 max by position
    // rps 0.1 → raw 5000; pos cap 1000; BP 50k/100 = 500 → final 500
    const r = sizePosition(100, 99.9, "BUY", loose, 100_000, 50_000);
    expect(r.shares).toBe(500);
    expect(r.notes).toContain("capped by buying power");
  });

  it("does NOT cap a SELL by buying power (selling held shares consumes none)", () => {
    const loose = { ...SIZING, maxPositionFraction: 1.0 };
    // SELL: rps = stop-entry = 0.1 → raw 5000; pos cap 1000; BP is ignored → 1000
    const r = sizePosition(100, 100.1, "SELL", loose, 100_000, 50_000);
    expect(r.shares).toBe(1000);
    expect(r.notes).not.toContain("capped by buying power");
  });

  it("returns 0 for a wrong-side stop (BUY stop >= entry, SELL stop <= entry)", () => {
    expect(sizePosition(100, 101, "BUY", SIZING, 100_000, 400_000).shares).toBe(0); // stop above entry
    expect(sizePosition(100, 99, "SELL", SIZING, 100_000, 400_000).shares).toBe(0); // stop below entry
    expect(sizePosition(100, 100, "BUY", SIZING, 100_000, 400_000).shares).toBe(0); // equal
  });

  it("clamps to maxShares", () => {
    const r = sizePosition(100, 99.99, "BUY", { ...SIZING, maxPositionFraction: 100, maxShares: 10_000 }, 1e12, 1e12);
    expect(r.shares).toBe(10_000);
  });

  it("returns 0 when below minShares", () => {
    // budget 500, rps 1000 → 0.5 raw → floor 0
    const r = sizePosition(1000, 0, "BUY", SIZING, 100_000, 1e9);
    expect(r.shares).toBe(0);
  });

  it("does not zero forever when minShares > maxShares (misconfig) — cap wins", () => {
    const bad = { ...SIZING, minShares: 500, maxShares: 100, maxPositionFraction: 100 };
    // rps 2 → raw 250 → capped to maxShares 100; min floor (500) is inconsistent → NOT applied
    expect(sizePosition(100, 98, "BUY", bad, 100_000, 1e9).shares).toBe(100);
  });

  it("fixed_usd mode sizes off the dollar budget", () => {
    const fixed = { ...SIZING, riskMode: "fixed_usd" as const, riskUsd: 200, maxPositionFraction: 100 };
    // rps 2 → 100 shares
    expect(sizePosition(100, 98, "BUY", fixed, null, 1e9).shares).toBe(100);
  });
});

describe("computePrefill", () => {
  const flat = series(Array.from({ length: 30 }, () => 100), 0.5); // high-low 1.0 → ATR = 1.0, kijun ≈ 100

  it("BUY: ATR stop below entry, R-multiple target above, sized", () => {
    const r = computePrefill({
      action: "BUY",
      profile: profile({ stop: { model: "atr", atrLookback: 14, atrMult: 2, percentStop: 0.02, ichimokuRef: "kijun" } }),
      bars: flat,
      last: 100,
      mid: null,
      spreadPct: null,
      equity: 100_000,
      buyingPower: 400_000,
    });
    expect(r.entry).toBe(100);
    expect(r.stop).toBeCloseTo(98, 2); // 100 - 2*ATR(1)
    expect(r.target).toBeCloseTo(104, 2); // entry + 2R, R=2 → +4
    expect(r.shares).toBeGreaterThan(0);
  });

  it("SELL: stop above entry, target below", () => {
    const r = computePrefill({
      action: "SELL",
      profile: profile({ stop: { model: "atr", atrLookback: 14, atrMult: 2, percentStop: 0.02, ichimokuRef: "kijun" } }),
      bars: flat,
      last: 100,
      mid: null,
      spreadPct: null,
      equity: 100_000,
      buyingPower: 400_000,
    });
    expect(r.stop).toBeCloseTo(102, 2);
    expect(r.target).toBeCloseTo(96, 2);
  });

  it("falls back to ATR when the Ichimoku stop is on the wrong side of entry", () => {
    // descending series → kijun (26-mid) sits ABOVE a low last price → wrong-side for a BUY
    const desc = series(Array.from({ length: 30 }, (_, i) => 130 - i)); // newest ≈ 101
    const last = desc[desc.length - 1].close;
    const r = computePrefill({
      action: "BUY",
      profile: profile({ stop: { model: "ichimoku", atrLookback: 14, atrMult: 1, percentStop: 0.02, ichimokuRef: "kijun" } }),
      bars: desc,
      last,
      mid: null,
      spreadPct: null,
      equity: 100_000,
      buyingPower: 400_000,
    });
    expect(r.notes).toContain("Ichimoku stop wrong side → ATR");
    expect(r.stop).not.toBeNull();
    expect(r.stop!).toBeLessThan(r.entry!); // protective side after fallback
  });

  it("spread-sanity gate: wide mid falls back to last", () => {
    const r = computePrefill({
      action: "BUY",
      profile: profile({ entry: { source: "mid", offsetTicks: 0, maxSpreadPct: 0.005 } }),
      bars: flat,
      last: 100,
      mid: 110, // absurd mid
      spreadPct: 0.08, // 8% spread (IEX-wide) → gate rejects
      equity: 100_000,
      buyingPower: 400_000,
    });
    expect(r.entry).toBe(100); // used last, not the wide mid
    expect(r.notes).toContain("spread too wide → last price");
  });
});

describe("entryMechanism override (#51)", () => {
  // 15 flat bars at 100 then a push to 104 → last close 104, recent swing high 104.5.
  const s = series([...Array(15).fill(100), 104]);
  const base = {
    action: "BUY" as const,
    profile: profile(),
    bars: s,
    last: 104,
    mid: null,
    spreadPct: null,
    equity: 100_000,
    buyingPower: 100_000,
  };

  it("undefined override keeps the profile entry (parity: last price)", () => {
    expect(computePrefill(base).entry).toBe(104);
  });

  it("breakout override sets entry to the recent swing high", () => {
    const r = computePrefill({ ...base, entryMechanism: { id: "breakout", params: {} } });
    expect(r.entry).toBeCloseTo(104.5);
  });

  it("kijun_pullback override pulls entry to the Kijun structure", () => {
    // 26+ bars so lastLevels has a Kijun; a rising series → Kijun below last.
    const rising = series(Array.from({ length: 30 }, (_, i) => 90 + i));
    const last = 119;
    const r = computePrefill({ ...base, bars: rising, last, entryMechanism: { id: "kijun_pullback", params: {} } });
    expect(r.entry).not.toBeNull();
    expect(r.entry!).toBeLessThan(last); // pulled back below the last price
  });
});
