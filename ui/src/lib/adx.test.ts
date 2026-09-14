import { describe, it, expect } from "vitest";
import { computeAdx, isRising } from "./adx";

function uptrend(n: number): { high: number; low: number; close: number }[] {
  return Array.from({ length: n }, (_, i) => {
    const base = 100 + i * 1.5;
    return { high: base + 1, low: base - 1, close: base + 0.5 };
  });
}

function flat(n: number): { high: number; low: number; close: number }[] {
  return Array.from({ length: n }, () => ({ high: 100.2, low: 99.8, close: 100 }));
}

describe("computeAdx", () => {
  it("no bars → empty series, never throws", () => {
    expect(computeAdx([])).toEqual({ adx: [], plusDi: [], minusDi: [] });
  });

  it("arrays are 1:1 with bars (bar 0 included, matching pandas indexing, #181 code review)", () => {
    const { adx, plusDi, minusDi } = computeAdx(uptrend(5));
    expect(adx).toHaveLength(5);
    expect(plusDi).toHaveLength(5);
    expect(minusDi).toHaveLength(5);
    // At bar 0 there's no prior bar — +DM/-DM are 0 by definition, so DX (and thus adx[0]) is null,
    // matching pandas' NaN there — never a fabricated number.
    expect(adx[0]).toBeNull();
  });

  it("adx/plusDi/minusDi stay within [0, 100] wherever defined", () => {
    const { adx, plusDi, minusDi } = computeAdx(uptrend(60));
    for (const series of [adx, plusDi, minusDi]) {
      for (const v of series) {
        if (v == null) continue;
        expect(v).toBeGreaterThanOrEqual(0);
        expect(v).toBeLessThanOrEqual(100);
      }
    }
  });

  it("a sustained uptrend produces +DI > -DI and a non-trivial ADX", () => {
    const { adx, plusDi, minusDi } = computeAdx(uptrend(60));
    expect(plusDi.at(-1)!).toBeGreaterThan(minusDi.at(-1)!);
    expect(adx.at(-1)!).toBeGreaterThan(15); // clearly trending, not "no trend"
  });

  it("a flat/sideways series (non-zero range) produces near-zero directional movement", () => {
    const { plusDi, minusDi } = computeAdx(flat(30));
    expect(plusDi.at(-1)!).toBeCloseTo(0, 1);
    expect(minusDi.at(-1)!).toBeCloseTo(0, 1);
  });

  it("a ZERO-range series (high===low always, ATR===0) → null DI/ADX, never a fabricated 0", () => {
    const deadFlat = Array.from({ length: 20 }, () => ({ high: 100, low: 100, close: 100 }));
    const { adx, plusDi, minusDi } = computeAdx(deadFlat);
    expect(plusDi.every((v) => v === null)).toBe(true);
    expect(minusDi.every((v) => v === null)).toBe(true);
    expect(adx.every((v) => v === null)).toBe(true);
  });
});

describe("isRising", () => {
  it("true when the latest value exceeds the value `lookback` slots back", () => {
    expect(isRising([10, 12, 14, 16], 3)).toBe(true);
    expect(isRising([16, 14, 12, 10], 3)).toBe(false);
  });
  it("false (never throws) when the series is too short to compare", () => {
    expect(isRising([], 3)).toBe(false);
    expect(isRising([1, 2, 3], 3)).toBe(false); // length == lookback, no index -1-3
  });
  it("false (never throws) when either compared value is null", () => {
    expect(isRising([null, 12, 14, 16], 3)).toBe(false);
    expect(isRising([10, 12, 14, null], 3)).toBe(false);
  });
});
