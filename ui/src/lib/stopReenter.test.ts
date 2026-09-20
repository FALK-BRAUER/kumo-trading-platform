import { describe, it, expect } from "vitest";
import { stopReenterDefaults } from "./stopReenter";
import type { Levels } from "@/lib/ichimoku";

function levels(overrides: Partial<Levels> = {}): Levels {
  return {
    price: 260, tenkan: 259, kijun: 255, spanA: 257, spanB: 253, cloudTop: 258, cloudBot: 250, ma200: 240,
    ...overrides,
  };
}

describe("stopReenterDefaults", () => {
  it("LONG: reclaim = live price, base = kijun, floor = min(cloudBot, ma200)", () => {
    const d = stopReenterDefaults("LONG", 260, levels());
    expect(d.reclaim_price).toBe(260);
    expect(d.base_price).toBe(255); // kijun
    expect(d.floor_price).toBe(240); // min(cloudBot=250, ma200=240)
  });

  it("SHORT: floor mirrors to max(cloudTop, ma200)", () => {
    const d = stopReenterDefaults("SHORT", 260, levels());
    expect(d.floor_price).toBe(258); // max(cloudTop=258, ma200=240)
  });

  it("falls back to a %-off-price floor when no Ichimoku levels are available yet", () => {
    const dLong = stopReenterDefaults("LONG", 100, null);
    expect(dLong.floor_price).toBeCloseTo(95, 5);
    expect(dLong.base_price).toBe(100); // no kijun -> falls back to price

    const dShort = stopReenterDefaults("SHORT", 100, null);
    expect(dShort.floor_price).toBeCloseTo(105, 5);
  });

  it("all null when there's no live price and no levels yet", () => {
    const d = stopReenterDefaults("LONG", null, null);
    expect(d.reclaim_price).toBeNull();
    expect(d.base_price).toBeNull();
    expect(d.floor_price).toBeNull();
  });
});
