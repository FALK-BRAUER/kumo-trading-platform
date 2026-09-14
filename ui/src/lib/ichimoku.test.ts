import { describe, it, expect } from "vitest";
import { cloudPosition, chikouAbovePrice, shiftedCloud, type Levels } from "./ichimoku";
import type { BarDTO } from "@/lib/api/types";

function levels(cloudTop: number, cloudBot: number): Levels {
  return { price: 0, tenkan: null, kijun: null, spanA: null, spanB: null, cloudTop, cloudBot, ma200: null };
}

function bars(closes: number[]): BarDTO[] {
  return closes.map((c, i) => ({
    instrument_id: "TEST.XNAS",
    ts_event: i,
    open: c,
    high: c + 1,
    low: c - 1,
    close: c,
    volume: 1000,
  }));
}

describe("cloudPosition", () => {
  it("above / in / below the cloud", () => {
    expect(cloudPosition(110, levels(105, 100))).toBe("above");
    expect(cloudPosition(102, levels(105, 100))).toBe("in");
    expect(cloudPosition(95, levels(105, 100))).toBe("below");
  });
  it("boundary — exactly at cloudTop/cloudBot reads 'in', never split between callers (#179)", () => {
    expect(cloudPosition(105, levels(105, 100))).toBe("in");
    expect(cloudPosition(100, levels(105, 100))).toBe("in");
  });
  it("null price or incomplete levels → null, never guesses", () => {
    expect(cloudPosition(null, levels(105, 100))).toBeNull();
    expect(cloudPosition(102, null)).toBeNull();
    expect(cloudPosition(102, { ...levels(105, 100), cloudTop: null })).toBeNull();
  });
});

describe("chikouAbovePrice", () => {
  it("current close vs close N bars ago", () => {
    const closes = Array.from({ length: 30 }, (_, i) => 100 + i); // rising series
    expect(chikouAbovePrice(bars(closes), 26)).toBe(true); // close[29]=129 > close[3]=103
    expect(chikouAbovePrice(bars(closes.slice().reverse()), 26)).toBe(false);
  });
  it("insufficient bars → null, never guesses", () => {
    expect(chikouAbovePrice(bars(Array.from({ length: 10 }, (_, i) => 100 + i)), 26)).toBeNull();
  });
});

describe("shiftedCloud", () => {
  it("insufficient bars (needs displacement + 52) → all null", () => {
    expect(shiftedCloud(bars(Array.from({ length: 50 }, () => 100)))).toEqual({
      cloudTop: null,
      cloudBot: null,
      green: null,
    });
  });
  it("uses the AS-OF-displacement window, not the latest bars — differs from an unshifted read", () => {
    // A flat series up to bar 80, then a sharp move in the last 26 bars. The shifted cloud (computed
    // from data ending 26 bars back) should reflect the FLAT period, unaffected by the recent move.
    const flat = Array.from({ length: 80 }, () => 100);
    const recentSpike = Array.from({ length: 26 }, (_, i) => 100 + i * 5);
    const result = shiftedCloud(bars([...flat, ...recentSpike]), 26);
    expect(result.cloudTop).not.toBeNull();
    expect(result.cloudTop).toBeCloseTo(100, 0); // flat series → cloud hugs ~100, not the spike
  });
});
