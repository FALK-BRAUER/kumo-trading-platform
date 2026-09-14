import { describe, it, expect } from "vitest";
import type { KpiContext, KpiDef } from "./types";
import { registerKpi, getKpi, allKpis } from "./registry";

const EMPTY_CTX: KpiContext = {
  instrumentId: "TEST.XNAS", price: null, quote: null, bars: [], vwap: null, todayRange: null,
  fundamentals: null,
};

function fakeKpi(overrides?: Partial<KpiDef>): KpiDef {
  return { id: "fake-kpi", label: "Fake", format: () => "42", ...overrides };
}

describe("kpi registry", () => {
  it("register/get/all round-trip — the mechanism is generic, not tied to any one KPI", () => {
    const def = fakeKpi();
    registerKpi(def);
    expect(getKpi("fake-kpi")).toBe(def);
    expect(allKpis()).toContain(def);
  });

  it("unknown id → undefined, not a throw", () => {
    expect(getKpi("does-not-exist")).toBeUndefined();
  });

  it("format() runs against the passed context and can return null for 'not available'", () => {
    const def = fakeKpi({ format: (ctx) => (ctx.price == null ? null : `$${ctx.price}`) });
    expect(def.format(EMPTY_CTX)).toBeNull();
    expect(def.format({ ...EMPTY_CTX, price: 100 })).toBe("$100");
  });
});
