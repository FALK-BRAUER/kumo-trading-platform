/**
 * The global period selector (#233 follow-up).
 *
 * The operator saw "+$2,841 this 1W" sitting directly under a NET of $1,061 with no way to reconcile them. The
 * cause was three time bases on one panel and nothing saying so: state figures (cash, deployed) have no
 * period, REALIZED was scoped to today, and the equity chart carried its own local selector.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { PERIODS } from "./periods";
import { useCockpitStore } from "@/lib/framework/store";

describe("the global period", () => {
  it("defaults to 1D — the window an operator acts on", () => {
    // Read from the store's INITIAL state, not from one this test just set. The first version had a
    // `beforeEach` that set the period to 1D and then asserted it was 1D, which passes whatever the
    // default is — changing it to 1M left the test green.
    //
    // 1D is also the window the old per-session REALIZED already answered, so the default preserves
    // what that number meant while making it selectable rather than implicit.
    expect(useCockpitStore.getInitialState().period).toBe("1D");
  });

  it("is SHARED, so one control moves every flow figure", () => {
    useCockpitStore.setState({ period: "1D" });
    // The whole point. Two surfaces reading their own period is the bug, not the feature: it is how a
    // chart says 1W while the number above it says today and neither is labelled.
    useCockpitStore.getState().setPeriod("1W");
    expect(useCockpitStore.getState().period).toBe("1W");
  });

  it("offers 1D through All, with 1D first", () => {
    expect(PERIODS.map(([key]) => key)).toEqual(["1D", "1W", "1M", "3M", "all"]);
  });

  it("the equity tile reads the SHARED period, not one of its own", () => {
    // The call site. Reverting the tile to a local period only failed the type-checker, and only by
    // accident — a local `useState("1M")` would have type-checked fine and silently restored the split
    // that caused the confusion in the first place.
    const source = readFileSync(
      join(__dirname, "..", "..", "tiles", "equity", "EquityTile.tsx"), "utf8");
    expect(source).toContain("useCockpitStore((s) => s.period)");
    expect(source).not.toMatch(/useState\([\"']1[DWM]/);
  });
});

describe("the global period control is actually MOUNTED (#233)", () => {
  /**
   * The seam, and the defect it is named for. `PeriodSelector` shipped correct and mounted ONLY
   * inside `EquityTile`, gated on that tile's own `available` curves — so on any view without the
   * equity tile there was no control at all, while `BookTile` went on reading the shared `period`
   * from the store. State global, control local: REALIZED sat on whatever the store last held and
   * nothing on screen could move it. the operator's report was simply "I cannot see [it] — no period
   * selector", which is exactly what a correct component nobody mounts looks like.
   */
  it("Board renders it, so every view has one", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const src = readFileSync(join(__dirname, "..", "board", "Board.tsx"), "utf8");
    expect(src, "the board never imports the selector").toContain("PeriodSelector");
    expect(src, "the selector is imported and never rendered").toMatch(/<PeriodSelector\s*\/>/);
  });

  it("the board's instance passes NO `available`, so no period is greyed out", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const src = readFileSync(join(__dirname, "..", "board", "Board.tsx"), "utf8");
    // `available` exists so the equity chart can disable a curve the broker did not return. On the
    // global bar every period is answerable — an unswept one renders a dash, which is a different
    // and honest statement from a disabled tab.
    expect(src).not.toMatch(/<PeriodSelector[^/>]*available/);
  });
});
