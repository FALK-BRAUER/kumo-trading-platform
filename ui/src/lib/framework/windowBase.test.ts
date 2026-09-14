/**
 * The per-lane window delta, computed in the tile (#699).
 *
 * `Δunrealized(W) = standing_now(lane) − base(lane)`. The tile already knows the first term; the
 * second comes from `GET /pnl/unrealized-base`, which reads #734's observation table.
 *
 * THE None-PROPAGATION RULES LIVE HERE NOW. They were pinned on a Python helper
 * (`window_delta_unrealized`) that nothing called, and the subtraction genuinely belongs on this
 * side because the "now" term lives on the frame. Two implementations of one rule is the drift these
 * tickets are about, so the Python one is DELETED IN THE SAME COMMIT — one commit of overlap, then
 * one owner.
 */

import { describe, expect, it, vi } from "vitest";
import { laneCoverageGap, windowDelta } from "./windowBase";

/** A captured base map as the backend actually emits it: keyed by `strategy_id`. */
const EXT_BASE: Record<string, number | null> = { "MOMENTUM-001": 100, EXTERNAL: -1093.3 };

describe("the delta, and the three states it must keep apart", () => {
  it("subtracts the base from the standing figure", () => {
    expect(windowDelta(175, { "MOMENTUM-002": 100 }, "MOMENTUM-002")).toBeCloseTo(75, 6);
  });

  it("is UNKNOWN when the period has no base map at all", () => {
    // The window has no base (`all`), the day was never captured, or the read failed. All three
    // arrive as null and all three are unknown — the endpoint's `unreadable` and `error` fields are
    // what tell an operator which.
    expect(windowDelta(175, null, "MOMENTUM-002")).toBeNull();
  });

  it("is ZERO — not unknown — for a lane absent from a CAPTURED day", () => {
    // An empty map means the manifest says that day WAS captured and every lane was flat. A lane
    // absent from it genuinely held nothing, so its base is a KNOWN zero. Treating this as unknown
    // suppressed a value we have; treating a never-captured day as zero fabricates one.
    expect(windowDelta(175, {}, "MOMENTUM-002")).toBeCloseTo(175, 6);
  });

  it("is UNKNOWN when the lane's own base is explicitly null", () => {
    // One unpriced leg makes that lane's total unknown at the base date — a partial sum is not a
    // total. The rest of the map is unaffected.
    expect(windowDelta(175, { "MOMENTUM-002": null, "BCTROT-004": 10 }, "MOMENTUM-002")).toBeNull();
  });

  it("is UNKNOWN when the standing figure itself is unknown", () => {
    // A delta needs two numbers. A missing one is not a zero — both directions, because I have
    // hunted the first much harder than the second.
    expect(windowDelta(null, { "MOMENTUM-002": 100 }, "MOMENTUM-002")).toBeNull();
  });

  it("refuses a NON-FINITE standing figure rather than propagating NaN into a cell", () => {
    expect(windowDelta(NaN, { A: 1 }, "A")).toBeNull();
    expect(windowDelta(Infinity, { A: 1 }, "A")).toBeNull();
  });
});

describe("laneCoverageGap — the silent-darkness detector", () => {
  it("names lanes that hold something and appear in NO period map", () => {
    // A strategy_id NAMESPACE MISMATCH between the table and the frame would dark the whole feature
    // while looking exactly like "no bases yet". Agreement is not connection, and this is the shape
    // that has bitten this codebase repeatedly today.
    const gap = laneCoverageGap(
      { "MOMENTUM-002": 100, "BCTROT-004": -50 },
      { "1W": { "MOMENTUM-002": 10 }, "1M": { "MOMENTUM-002": 20 } },
    );
    expect(gap).toEqual(["BCTROT-004"]);
  });

  it("says NOTHING when every period map is null — no bases yet is not a mismatch", () => {
    // THE VACUITY GUARD. Before any capture has run every map is null, and a detector that fired
    // then would cry on every fresh instance and be muted before it ever meant anything.
    expect(laneCoverageGap({ A: 100 }, { "1W": null, "1M": null })).toEqual([]);
  });

  it("ignores lanes holding NOTHING — they are legitimately absent from a base", () => {
    expect(laneCoverageGap({ A: 0, B: 100 }, { "1W": { B: 5 } })).toEqual([]);
  });

  it("ignores a lane with an UNKNOWN standing figure", () => {
    // Unknown standing is not evidence of a mismatch; it is evidence of an unpriced leg.
    expect(laneCoverageGap({ A: null, B: 100 }, { "1W": { B: 5 } })).toEqual([]);
  });

  it("does not fire on a CAPTURED-FLAT day, where every map is legitimately empty", () => {
    // An empty map is a real answer — the day was captured and everything was flat. A lane absent
    // from it is not missing, it is zero.
    expect(laneCoverageGap({ A: 100 }, { "1W": {} })).toEqual([]);
  });
});

describe("the Unclaimed row is bucketed under EXTERNAL, not under its own label", () => {
  // THIS DEFECT HAS SHIPPED ONCE ALREADY, for the realized figure, and books.ts:585-640 is the
  // post-mortem: the Unclaimed row looked itself up by its own LABEL and `by_strategy["Unclaimed"]`
  // does not exist, so EXTERNAL money rendered nowhere. The window delta repeated it.
  //
  // It is worse here than it was there. `windowDelta` treats a lane ABSENT from a non-empty captured
  // map as a base of ZERO — deliberately, because that is what an absent lane means once a day has
  // been captured. So the Unclaimed row does not render blank, which someone would notice. It
  // renders its ENTIRE standing unrealized as if the whole thing had moved this window, on the
  // flagship number of #699, with no outward sign of being wrong.

  it("FIXTURE PROPERTY: the base map keys by strategy_id, so it has EXTERNAL and no Unclaimed", () => {
    // Assert the fixture can express the bug before asserting the bug is gone. A base map that
    // happened to contain "Unclaimed" would make the real assertion below pass against a mapping
    // that does nothing — the vacuous-fixture shape that let a truncation-invariance test pass
    // twice with the look-ahead reintroduced.
    expect(Object.prototype.hasOwnProperty.call(EXT_BASE, "EXTERNAL")).toBe(true);
    expect(Object.prototype.hasOwnProperty.call(EXT_BASE, "Unclaimed")).toBe(false);
  });

  it("resolves the Unclaimed label to EXTERNAL's base rather than fabricating zero", () => {
    // Standing -1500, base -1093.30 => the window moved -406.70. Read by label it would be the
    // whole -1500, which is the number that would have reached the screen.
    expect(windowDelta(-1500, EXT_BASE, "Unclaimed")).toBeCloseTo(-406.7, 6);
  });

  it("a real lane is untouched by the mapping", () => {
    expect(windowDelta(250, EXT_BASE, "MOMENTUM-001")).toBeCloseTo(150, 6);
  });

  it("EXTERNAL passed directly still resolves to the same base — one rule, not two", () => {
    // Two derivations of one fact will disagree. Whichever spelling reaches this function, the
    // answer must be identical, or the label and the id become two rules that drift.
    expect(windowDelta(-1500, EXT_BASE, "EXTERNAL")).toEqual(
      windowDelta(-1500, EXT_BASE, "Unclaimed"),
    );
  });

  it("the three states survive the mapping — an uncaptured window is still UNKNOWN", () => {
    // The mapping must not turn "never told us" into a number. This is the state the whole
    // read path exists to keep separate.
    expect(windowDelta(-1500, null, "Unclaimed")).toBeNull();
  });

  it("RESOLUTION LIVES INSIDE windowDelta, so no call site can get it wrong", () => {
    // Aim at the class, not the instance. There are two call sites today (BookTile,
    // ManagedPortfolioTile) and the next one will be written by someone who has not read this file.
    // A resolver the CALLER must remember is a list that has to be kept correct; resolving here
    // makes the dangerous thing unreachable instead. If this ever moves back out to the callers,
    // this assertion is what says so.
    expect(windowDelta(0, { EXTERNAL: 10 }, "Unclaimed")).toBe(-10);
  });
});
