import { describe, it, expect, beforeAll } from "vitest";
import { getChecklist } from "@/lib/framework/checklist/registry";
import { evaluateChecklist } from "@/lib/framework/checklist/types";
import type { BarDTO } from "@/lib/api/types";
import "./checklists"; // side-effect: registers "blue_flag"

function trendingBars(n: number, direction: 1 | -1, step = 0.8): BarDTO[] {
  return Array.from({ length: n }, (_, i) => {
    const close = 100 + direction * i * step;
    return {
      instrument_id: "TEST.XNAS",
      ts_event: i,
      open: close,
      high: close + 1,
      low: close - 1,
      close,
      volume: 10000,
    };
  });
}

// A CONSTANT-increment (or purely accelerating, zero-pullback) ramp saturates ADX at its 100 ceiling and
// FLAT — every bar is a new high with no down days at all, so ADX has nowhere left to rise TO (expected —
// not a bug in computeAdx, see adx.test.ts). Two phases — choppy/sideways, THEN a clean strengthening
// trend — gives "adx_rising" genuine room to test true without the series saturating first.
function acceleratingBars(n: number, direction: 1 | -1): BarDTO[] {
  const choppyPhase = Math.floor(n * 0.6);
  return Array.from({ length: n }, (_, i) => {
    const offset =
      i < choppyPhase
        ? Math.sin(i * 0.9) * 2
        : Math.sin(choppyPhase * 0.9) * 2 + (i - choppyPhase) * 0.6;
    const close = 100 + direction * offset;
    return {
      instrument_id: "TEST.XNAS",
      ts_event: i,
      open: close,
      high: close + 0.5,
      low: close - 0.5,
      close,
      volume: 10000,
    };
  });
}

describe("blue_flag checklist (config, #181)", () => {
  let def: ReturnType<typeof getChecklist>;
  beforeAll(() => {
    def = getChecklist("blue_flag");
  });

  it("is registered", () => {
    expect(def).toBeDefined();
    expect(def!.conditions).toHaveLength(8);
  });

  it("a long, strong, sustained, ACCELERATING uptrend clears every condition — tier +++", () => {
    const ctx = { weeklyBars: acceleratingBars(120, 1), dailyBars: acceleratingBars(260, 1) };
    const result = evaluateChecklist(def!, ctx);
    expect(result.veto).toBeNull();
    expect(result.score).toBe(8);
    expect(result.tier).toBe("+++");
    expect(result.results.every((r) => r === true)).toBe(true);
  });

  it("a sustained downtrend vetoes on weekly-below-cloud, tier ---", () => {
    const ctx = { weeklyBars: trendingBars(120, -1), dailyBars: trendingBars(260, -1) };
    const result = evaluateChecklist(def!, ctx);
    expect(result.veto).toBe("weekly below cloud");
    expect(result.tier).toBe("---");
  });

  it("insufficient history on every series → every condition null, not false, no veto, tier '?' not bearish", () => {
    const ctx = { weeklyBars: trendingBars(5, 1), dailyBars: trendingBars(5, 1) };
    const result = evaluateChecklist(def!, ctx);
    expect(result.results.every((r) => r === null)).toBe(true);
    expect(result.score).toBe(0);
    expect(result.unknown).toBe(8);
    expect(result.veto).toBeNull(); // insufficient data is NOT a veto — never guesses
    expect(result.tier).toBe("?"); // NOT "--" — all-unknown must never read as a failing score
  });

  // Tier logic tested directly against hand-built results — a real bar series that's simultaneously
  // "weekly below cloud" AND "daily price above its 200d MA" is fragile to construct (needs a long prior
  // uptrend + a sharp-but-recent drop); the branch itself is what matters here, matching predecessor-repo's
  // exact tier scheme (`scanner/ichimoku.py:158-166`), not the market shape that would trigger it.
  it("vetoed (weekly below cloud) but condition 8 (price>MA200) still true → '--', not '---'", () => {
    const results = [false, false, false, false, false, false, false, true];
    expect(def!.tier(0, 8, "weekly below cloud", results)).toBe("--");
  });

  it("vetoed (weekly below cloud) and condition 8 also false → '---'", () => {
    const results = new Array(8).fill(false);
    expect(def!.tier(0, 8, "weekly below cloud", results)).toBe("---");
  });
});
