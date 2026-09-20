/**
 * Mechanism catalog (#65) unit tests. Each mechanism computes ONE raw candidate; rounding + protective-side
 * validation + fallback are the caller's job (covered by prefill.test.ts), so these assert raw candidates and
 * null cases only. The (leg, id) keying is exercised via the stop-vs-target `percent` collision.
 */
import { describe, expect, it } from "vitest";
import { TICK_SIZE } from "@/config/orderTicket";
import type { BarDTO } from "@/lib/api/types";
import type { Levels } from "@/lib/ichimoku";
import { getMechanism, hasMechanism, mechanismIds, type MechanismCtx } from "./mechanisms";

const bar = (o: number, h: number, l: number, c: number): BarDTO => ({
  instrument_id: "T.X",
  ts_event: 0,
  open: o,
  high: h,
  low: l,
  close: c,
  volume: 100,
});

// Flat-ish series, then a fixture with a known swing high/low for breakout.
const flat: BarDTO[] = Array.from({ length: 20 }, () => bar(100, 101, 99, 100));
const swing: BarDTO[] = [...Array.from({ length: 15 }, () => bar(100, 101, 99, 100)), bar(100, 108, 92, 100)];

const levels: Levels = { price: 100, tenkan: 102, kijun: 98, spanA: 96, spanB: 94, cloudTop: 96, cloudBot: 94, ma200: 90 };

const ctx = (over: Partial<MechanismCtx>): MechanismCtx => ({
  bars: flat,
  action: "BUY",
  last: 100,
  mid: 100,
  spreadPct: 0.001,
  levels,
  entry: null,
  stop: null,
  ...over,
});

describe("registry", () => {
  it("keys by (leg, id) — percent exists for both stop and target without collision", () => {
    expect(hasMechanism("stop", "percent")).toBe(true);
    expect(hasMechanism("target", "percent")).toBe(true);
    expect(getMechanism("stop", "percent")).not.toBe(getMechanism("target", "percent"));
  });
  it("lists ids per leg", () => {
    expect(mechanismIds("entry")).toEqual(
      expect.arrayContaining(["marketable", "last", "kijun_pullback", "tenkan_pullback", "atr_pullback", "breakout"]),
    );
    expect(mechanismIds("stop")).toEqual(expect.arrayContaining(["ichimoku", "atr", "percent"]));
    expect(mechanismIds("target")).toEqual(expect.arrayContaining(["r_multiple", "percent", "structure"]));
  });
  it("throws on unknown mechanism", () => {
    expect(() => getMechanism("entry", "nope")).toThrow(/unknown mechanism/);
  });
});

describe("entry mechanisms", () => {
  const m = (id: string) => getMechanism("entry", id);

  it("marketable takes mid when spread is sane", () => {
    expect(m("marketable").compute(ctx({}), { offsetTicks: 1, maxSpreadPct: 0.003 })).toEqual({ price: 100 });
  });
  it("marketable falls to last with a note when spread too wide", () => {
    const r = m("marketable").compute(ctx({ spreadPct: 0.02, last: 99 }), { offsetTicks: 1, maxSpreadPct: 0.003 });
    expect(r).toEqual({ price: 99, note: "spread too wide → last price" });
  });
  it("marketable rejects a crossed (negative) spread → last", () => {
    const r = m("marketable").compute(ctx({ spreadPct: -0.001, last: 99 }), { offsetTicks: 1, maxSpreadPct: 0.003 });
    expect(r.price).toBe(99);
  });
  it("marketable with no mid returns last, no note", () => {
    expect(m("marketable").compute(ctx({ mid: null, last: 99 }), { offsetTicks: 1, maxSpreadPct: 0.003 })).toEqual({ price: 99 });
  });
  it("kijun_pullback uses kijun, falls to last when no levels", () => {
    expect(m("kijun_pullback").compute(ctx({}), { offsetTicks: 0 }).price).toBe(98);
    expect(m("kijun_pullback").compute(ctx({ levels: null, last: 99 }), { offsetTicks: 0 }).price).toBe(99);
  });
  it("tenkan_pullback uses tenkan", () => {
    expect(m("tenkan_pullback").compute(ctx({}), { offsetTicks: 0 }).price).toBe(102);
  });
  it("atr_pullback subtracts k·ATR below last on a BUY", () => {
    // ATR of the flat series = 2 (high-low each bar). last 100, mult 1 → 98.
    expect(m("atr_pullback").compute(ctx({}), { offsetTicks: 0, atrLookback: 14, atrMult: 1 }).price).toBeCloseTo(98);
    // SELL adds.
    expect(m("atr_pullback").compute(ctx({ action: "SELL" }), { offsetTicks: 0, atrLookback: 14, atrMult: 1 }).price).toBeCloseTo(102);
  });
  it("atr_pullback null when not enough bars", () => {
    expect(m("atr_pullback").compute(ctx({ bars: flat.slice(0, 3) }), { offsetTicks: 0, atrLookback: 14, atrMult: 1 }).price).toBeNull();
  });
  it("breakout returns the swing high (BUY) / low (SELL)", () => {
    expect(m("breakout").compute(ctx({ bars: swing }), { offsetTicks: 1, lookback: 10 }).price).toBe(108);
    expect(m("breakout").compute(ctx({ bars: swing, action: "SELL" }), { offsetTicks: 1, lookback: 10 }).price).toBe(92);
  });
});

describe("stop mechanisms", () => {
  const m = (id: string) => getMechanism("stop", id);

  it("ichimoku picks the ref (bufTicks:0 = raw): kijun / cloudBot (BUY) / cloudTop (SELL)", () => {
    expect(m("ichimoku").compute(ctx({}), { ichimokuRef: "kijun", bufTicks: 0 }).price).toBe(98);
    expect(m("ichimoku").compute(ctx({}), { ichimokuRef: "cloud", bufTicks: 0 }).price).toBe(94); // cloudBot for BUY
    expect(m("ichimoku").compute(ctx({ action: "SELL" }), { ichimokuRef: "cloud", bufTicks: 0 }).price).toBe(96); // cloudTop for SELL
  });
  it("ichimoku applies bufTicks on the protective side (BUY below, SELL above)", () => {
    expect(m("ichimoku").compute(ctx({}), { ichimokuRef: "kijun", bufTicks: 1 }).price).toBeCloseTo(98 - TICK_SIZE);
    expect(m("ichimoku").compute(ctx({ action: "SELL" }), { ichimokuRef: "kijun", bufTicks: 1 }).price).toBeCloseTo(98 + TICK_SIZE);
  });
  it("ichimoku null when no level", () => {
    expect(m("ichimoku").compute(ctx({ levels: null }), { ichimokuRef: "kijun", bufTicks: 1 }).price).toBeNull();
  });
  it("atr subtracts mult·ATR from entry (BUY), null without entry", () => {
    expect(m("atr").compute(ctx({ entry: 100 }), { atrLookback: 14, atrMult: 1.5 }).price).toBeCloseTo(97); // 100 - 1.5*2
    expect(m("atr").compute(ctx({ entry: null }), { atrLookback: 14, atrMult: 1.5 }).price).toBeNull();
  });
  it("percent stop is entry·(1∓p), null without entry", () => {
    expect(m("percent").compute(ctx({ entry: 100 }), { percentStop: 0.02 }).price).toBeCloseTo(98);
    expect(m("percent").compute(ctx({ action: "SELL", entry: 100 }), { percentStop: 0.02 }).price).toBeCloseTo(102);
    expect(m("percent").compute(ctx({ entry: null }), { percentStop: 0.02 }).price).toBeNull();
  });
});

describe("target mechanisms", () => {
  const m = (id: string) => getMechanism("target", id);

  it("r_multiple is entry ± R·|entry−stop|, null without stop", () => {
    expect(m("r_multiple").compute(ctx({ entry: 100, stop: 98 }), { targetR: 2 }).price).toBeCloseTo(104);
    expect(m("r_multiple").compute(ctx({ action: "SELL", entry: 100, stop: 102 }), { targetR: 2 }).price).toBeCloseTo(96);
    expect(m("r_multiple").compute(ctx({ entry: 100, stop: null }), { targetR: 2 }).price).toBeNull();
  });
  it("percent target is entry·(1±p)", () => {
    expect(m("percent").compute(ctx({ entry: 100 }), { percentTarget: 0.04 }).price).toBeCloseTo(104);
    expect(m("percent").compute(ctx({ action: "SELL", entry: 100 }), { percentTarget: 0.04 }).price).toBeCloseTo(96);
  });
  it("structure uses the cloud edge only when it's beyond entry in-direction", () => {
    // BUY: cloudTop 96 is BELOW entry 100 → not a valid target → null.
    expect(m("structure").compute(ctx({ entry: 100 }), {}).price).toBeNull();
    // BUY with entry below the cloud top → target = cloudTop.
    expect(m("structure").compute(ctx({ entry: 95 }), {}).price).toBe(96);
    // no levels → null.
    expect(m("structure").compute(ctx({ entry: 95, levels: null }), {}).price).toBeNull();
  });
});
