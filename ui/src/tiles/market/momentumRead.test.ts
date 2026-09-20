/**
 * What the momentum map is allowed to claim (#351).
 *
 * The interesting tests here are not the four sign combinations — those are arithmetic. They are the
 * two that run against `__fixtures__/rotation.live.json`, a verbatim copy of the payload the api served
 * on 2026-08-19, kept whole rather than trimmed so the double cannot drift into representing something
 * production never emits.
 */
import { describe, expect, it } from "vitest";
import type { RotationAxis, RotationWindow } from "@/lib/api/client";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  labelSide,
  mapPoint,
  mapScale,
  placeLabels,
  plotX,
  plotY,
  quadrantOf,
  separable,
  separableCount,
} from "./momentumRead";
import live from "./__fixtures__/rotation.live.json";

const AXES = live.axes as unknown as RotationAxis[];
const WINDOWS = ["1D", "1W", "1M", "3M", "1Y"] as const;
const winsAt = (key: string): RotationWindow[] =>
  AXES.map((a) => a.win?.[key]).filter((w): w is RotationWindow => !!w);

const w = (o: Partial<RotationWindow>): RotationWindow => o as RotationWindow;

describe("quadrantOf", () => {
  it("names the corner from the sign pair", () => {
    expect(quadrantOf(w({ est: 2, move: 1 }))).toBe("Leading");
    expect(quadrantOf(w({ est: 2, move: -1 }))).toBe("Weakening");
    expect(quadrantOf(w({ est: -2, move: 1 }))).toBe("Improving");
    expect(quadrantOf(w({ est: -2, move: -1 }))).toBe("Lagging");
  });

  it("refuses to place an axis with no travel rather than defaulting it to a corner", () => {
    // A missing `move` means the previous window was never computed. Reading that as "not rising" puts
    // the axis in Lagging and invents a rotation out of an absence.
    expect(quadrantOf(w({ est: 2 }))).toBeNull();
    expect(quadrantOf(w({ est: 2, move: null }))).toBeNull();
    expect(quadrantOf(w({ move: 1 }))).toBeNull();
    expect(quadrantOf(null)).toBeNull();
  });

  it("resolves an exact zero to the negative side on both axes", () => {
    // Practically unreachable in float data; pinned so a change to `>=` has to be deliberate, and so
    // the strict comparisons match the diagram drawn in the module header.
    expect(quadrantOf(w({ est: 0, move: 0 }))).toBe("Lagging");
    expect(quadrantOf(w({ est: 0, move: 1 }))).toBe("Improving");
  });
});

describe("separable", () => {
  it("is true only when the interval keeps the estimate's sign", () => {
    expect(separable(w({ est: 3, lo: 1, hi: 5 }))).toBe(true);
    expect(separable(w({ est: -3, lo: -5, hi: -1 }))).toBe(true);
    expect(separable(w({ est: 3, lo: -1, hi: 7 }))).toBe(false);
  });

  it("is false, not true, when there is no interval at all", () => {
    // The failure direction matters: an axis with no interval must not render as the one certain thing
    // on the plot. Absence degrades to "cannot say", never to "yes".
    expect(separable(w({ est: 3 }))).toBe(false);
    expect(separable(w({ est: 3, lo: 1 }))).toBe(false);
  });
});

describe("against the payload the api actually served", () => {
  it("agrees with the upstream sig flag on every axis and window", () => {
    // TWO DERIVATIONS OF ONE FACT. `sig` is computed upstream in ledger-tool; `separable` is computed here
    // from lo/hi. They answer the same question by different routes, so a disagreement means one side
    // is broken — and this catches it against real numbers rather than a double built to agree.
    const disagreements: string[] = [];
    for (const axis of AXES) {
      for (const key of WINDOWS) {
        const win = axis.win?.[key];
        if (!win || win.lo == null || win.hi == null) continue;
        if (separable(win) !== !!win.sig) {
          disagreements.push(`${axis.pair} ${key}: sig=${win.sig} lo=${win.lo} hi=${win.hi}`);
        }
      }
    }
    expect(disagreements).toEqual([]);
  });

  it("finds almost nothing separable — which is why the map plots intervals", () => {
    // THE FIXTURE MUST BE ABLE TO VIOLATE THIS, or the test above is asserting into a vacuum. 1D
    // carries separable axes and 1M carries none, so the two branches of `separable` are both exercised
    // by real data and a change that hardwired either answer would fail here.
    const oneDay = separableCount(winsAt("1D"));
    const oneMonth = separableCount(winsAt("1M"));
    expect(oneDay.plotted).toBe(25);
    expect(oneDay.separable).toBe(5);
    expect(oneMonth.plotted).toBe(25);
    expect(oneMonth.separable).toBe(0);

    // The headline claim of the whole panel, measured: across 125 axis-windows, five are distinguishable
    // from no drift at all. A conventional rotation graph would have labelled all 125 with a quadrant.
    const all = WINDOWS.flatMap((k) => winsAt(k));
    const total = separableCount(all);
    expect(total.plotted).toBe(125);
    expect(total.separable).toBe(5);
  });

  it("keeps every plotted interval inside the frame", () => {
    // A bar that runs off the plot is a broken layout, not extra information. Clamping is checked on the
    // real spread because the widest interval in this payload is ~13x the widest estimate.
    const wins = winsAt("1M");
    const scale = mapScale(wins);
    for (const win of wins) {
      const p = mapPoint(win, scale);
      if (!p) continue;
      for (const v of [p.x, p.y, p.lo, p.hi]) {
        if (v === null) continue;
        expect(v).toBeGreaterThanOrEqual(-1);
        expect(v).toBeLessThanOrEqual(1);
      }
    }
  });
});

describe("mapScale — the 85th percentile of the ESTIMATES (#388 follow-up)", () => {
  // THIS REVERSES AN EARLIER DECISION, DELIBERATELY, AND THE OLD TEST SAID SO:
  //
  //   "scales to the intervals, not just the markers — derived from `est` alone, every bar would
  //    extend past the edge of the plot. The bars ARE the panel's argument, so they are what has to
  //    fit."
  //
  // That was true when the map drew each axis as its 95% interval, a horizontal bar. It stopped being
  // true when #351 moved the interval onto the RAIL and left the map as one dot per axis. The scale
  // kept fitting bars that are no longer drawn.
  //
  // The cost, measured on a live 1D payload: `Math.max` over est/lo/hi meant the single widest
  // confidence interval across 25 axes set the x-scale, and mean |x| was 0.079 — the average marker
  // sat 4% of the way from the centre to the edge. The map rendered as a vertical line through the
  // origin. Operator: "Cannot see much more". There was no room to label because there was no SEPARATION.

  it("uses the estimates and IGNORES the interval ends", () => {
    // The old behaviour returned 11 here (the `hi`). One axis's uncertainty must not move every other
    // axis's dot — the interval belongs on the rail, which draws it per axis at full precision.
    const scale = mapScale([w({ est: 1, lo: -9, hi: 11, move: 2 })]);
    expect(scale.x).toBe(1);
    expect(scale.y).toBe(2);
  });

  it("an OUTLIER does not set the scale for everyone else", () => {
    // The actual defect. Twenty ordinary axes plus one extreme: under `max` the twenty collapse to
    // 1/50th of the plot. The 85th percentile leaves them spread and clamps the outlier.
    const many = [
      ...Array.from({ length: 20 }, () => w({ est: 1, move: 1 })),
      w({ est: 50, move: 50 }),
    ];
    const scale = mapScale(many);
    expect(scale.x).toBeLessThan(5);
    expect(scale.x).toBeGreaterThan(0.5);
  });

  it("the 85th percentile is INTERPOLATED, not a nearest-rank pick", () => {
    // Ten values 1..10: the 85th percentile sits between the 8th and 9th. Nearest-rank would jump to
    // a member value and move in steps as the set changes size, which makes the scale twitch between
    // refreshes for no reason the operator can see.
    const scale = mapScale(Array.from({ length: 10 }, (_, i) => w({ est: i + 1, move: 1 })));
    expect(scale.x).toBeCloseTo(8.65, 2);
  });

  it("survives a degenerate set instead of dividing by zero", () => {
    expect(mapScale([]).x).toBeGreaterThan(0);
    expect(mapScale([w({ est: 0, move: 0 })]).y).toBeGreaterThan(0);
  });

  it("a MISSING est or move is skipped, not read as zero", () => {
    // A zero would drag the percentile down and squash the axes that do have a reading — the same
    // shape as counting an unpriced position as flat (#356).
    //
    // PINNED TO THE VALUE, not to `> 4`. The loose assertion passed with `|| 0` substituted for the
    // finite check: [4, 0, 6] gives 5.4 and [4, 6] gives 5.7, and both clear 4. The mutation harness
    // is what said so.
    const scale = mapScale([w({ est: 4, move: 4 }), w({ est: null, move: null }), w({ est: 6, move: 6 })]);
    expect(scale.x).toBeCloseTo(5.7, 4);
  });
});

describe("plot geometry — the two defects the browser found, made assertable", () => {
  it("keeps a clamped marker off the frame, not on it", () => {
    // BOTH EXTREMES, because clamping produces exactly ±1 and `overflow-hidden` cuts whatever sits on
    // the border. Measured in the browser at 1D: a marker rendered at top -2px, a shaved crescent. The
    // margin is asserted as a real gap rather than "inside", because a dot has a radius — landing at
    // 0.4% is landing on the edge for anything wider than a hairline.
    for (const v of [-1, 1]) {
      expect(plotX(v)).toBeGreaterThanOrEqual(4);
      expect(plotX(v)).toBeLessThanOrEqual(96);
      expect(plotY(v)).toBeGreaterThanOrEqual(4);
      expect(plotY(v)).toBeLessThanOrEqual(96);
    }
  });

  it("still spends most of the frame on data", () => {
    // The inset must not become padding. Guards the opposite failure: fixing the clip by shrinking the
    // plot until the picture is a postage stamp in a border.
    expect(plotX(1) - plotX(-1)).toBeGreaterThanOrEqual(80);
    expect(plotY(-1) - plotY(1)).toBeGreaterThanOrEqual(80);
  });

  it("puts zero at the centre lines the frame draws", () => {
    // The crosshair is drawn at 50% by CSS while the markers are placed by these functions — two
    // derivations of one fact. If the inset were ever made asymmetric, every quadrant on screen would be
    // offset from the axis it is measured against, and nothing else would notice.
    expect(plotX(0)).toBe(50);
    expect(plotY(0)).toBe(50);
  });

  it("turns a label toward the centre so it cannot leave the frame", () => {
    // Fixed to the right, GDX/GLD and XLRE/KRE both ended past the clip boundary and lost characters.
    expect(labelSide(0.9)).toBe("left");
    expect(labelSide(-0.9)).toBe("right");
    expect(labelSide(0)).toBe("right");
  });
});

describe("the component does not compute its own coordinates", () => {
  it("has no local percentage mapping in MomentumMap.tsx", () => {
    // AIMED AT THE CLASS, NOT THE INSTANCE. Nothing in this repo can render a component in a test, so a
    // percentage written inline in the .tsx is a number no test can reach — which is exactly how both
    // edge defects shipped. This fails the moment someone reintroduces a local mapping rather than
    // extending the geometry module, whatever they choose to call it.
    const src = readFileSync(new URL("./MomentumMap.tsx", import.meta.url), "utf8");
    const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    const offenders = code.match(/\)\s*\*\s*100\b/g) ?? [];
    expect(offenders).toEqual([]);
  });
});

// ==================================================================================================
// #388 — labels collided and dots were not tappable.
//
// Observed on a phone 2026-08-20: `GLD/SPY` printed over `XLV/SPY` over `GDX/GLD`, and `XLI/XLV` over
// `XLI/XLY`. Operator: "labels are missing. can't even click" — they were not missing, they were stacked.
//
// CAUSE: every label sat at a fixed offset from its dot with NO collision handling. Axes cluster near
// the origin by construction, so even the furthest-out ones (the labelled ones) are often close
// together.
//
// The component's own docstring said "NO LAYOUT JS", and that rule IS the bug: whether two labels
// collide is a question about pixels, so a map that refuses to measure its box cannot answer it. An
// 8-character label is ~44px wide whatever percentage its dot sits at — the same two dots collide on a
// phone and clear each other on a desktop.
// ==================================================================================================

describe("labels are placed, not just offset (#388)", () => {
  const PHONE = { width: 328, height: 224 };

  it("the fixture actually produces a collision", () => {
    // The fixture's own property FIRST. Two dots far enough apart would place cleanly with the fix
    // reverted, and every assertion below would be vacuous — the exact failure that let
    // kumo-trading-strategies' truncation test pass twice with the bug reintroduced.
    const a = { pair: "GLD/SPY", x: 0.42, y: 0.30 };
    const b = { pair: "XLV/SPY", x: 0.43, y: 0.305 };
    const dx = Math.abs(plotX(a.x) - plotX(b.x)) / 100 * PHONE.width;
    const dy = Math.abs(plotY(a.y) - plotY(b.y)) / 100 * PHONE.height;
    expect(dx).toBeLessThan(10);
    expect(dy).toBeLessThan(11); // inside one label's height — they WOULD overlap
  });

  it("two near-coincident dots are split onto OPPOSITE sides, never stacked", () => {
    // Not "one of them is dropped" — that would throw away a name it did not have to. The second
    // label's preferred side is taken, so it turns outward and both stay readable. What must never
    // happen is the shipped behaviour: both drawn at the same offset, on top of each other.
    const out = placeLabels(
      [
        { pair: "GLD/SPY", x: 0.42, y: 0.30 },
        { pair: "XLV/SPY", x: 0.43, y: 0.305 },
      ],
      PHONE,
    );
    expect(out.length).toBe(2);
    expect(new Set(out.map((p) => p.side)).size).toBe(2);
  });

  it("a dot with NO free side left is dropped", () => {
    // There are FOUR candidate positions now, not two, so it takes five coincident dots to exhaust
    // them — which is the point of adding above/below: the map's live shape is a vertical cluster near
    // the origin where both horizontal slots are occupied by neighbouring markers.
    //
    // The loser still gets no name. #388: "a label that cannot be placed legibly should not be drawn —
    // the dot's position is still information."
    const cluster = Array.from({ length: 5 }, (_, i) => ({
      pair: `A${i}/SPY`,
      x: 0.42 + i * 0.001,
      y: 0.30 + i * 0.001,
    }));
    const out = placeLabels(cluster, PHONE);
    expect(out.length).toBeLessThan(cluster.length);
    expect(new Set(out.map((p) => p.side)).size).toBeGreaterThan(1);
  });

  it("PRIORITY ORDER decides who wins a contested spot", () => {
    // The map sorts by distance from the origin before calling this, so the axis that has actually
    // gone somewhere keeps its name. Reversing the input must reverse the winner — if it did not, the
    // function would be picking by position or by chance rather than honouring the caller's ranking.
    const near = { pair: "AAA/BBB", x: 0.42, y: 0.30 };
    const far = { pair: "CCC/DDD", x: 0.43, y: 0.305 };
    expect(placeLabels([far, near], PHONE)[0].pair).toBe("CCC/DDD");
    expect(placeLabels([near, far], PHONE)[0].pair).toBe("AAA/BBB");
  });

  it("tries the OTHER side before giving up", () => {
    // Turning a label inward is free, and a fix that dropped every contested label would lose names
    // it did not have to. Two dots at the same height but far apart horizontally must both fit.
    const out = placeLabels(
      [
        { pair: "AAA/BBB", x: -0.6, y: 0.0 },
        { pair: "CCC/DDD", x: 0.6, y: 0.0 },
      ],
      PHONE,
    );
    expect(out.length).toBe(2);
  });

  it("never places a label off the edge of the plot", () => {
    // Characters clipped by the frame are as unreadable as a label stacked on another, so both are
    // refused. Now checked in BOTH axes, because a label can go above or below.
    //
    // GEOMETRY at 328x224, 8px: a 17-char label at x=0.92 has cx 296.8, so its right rect runs to
    // 384.7 — well past the frame. `above` and `below` are half-width either side of cx, spanning
    // 255.8..337.7, also past it. Only `left` (208.8..290.8) fits, so that is the only answer.
    const out = placeLabels([{ pair: "AAAAAAAA/BBBBBBBB", x: 0.92, y: 0.0 }], PHONE);
    expect(out).toEqual([{ pair: "AAAAAAAA/BBBBBBBB", side: "left" }]);
  });

  it("does not place a label on top of a CORNER caption", () => {
    // "Improving" / "Leading" / "Lagging" / "Weakening" are drawn in the corners, and a label landing
    // on one is as unreadable as a label landing on another label.
    //
    // GEOMETRY at 328x224, 8px: a dot at the extreme top-right has cx 301.1, cy 17.9. Its inward
    // (left) rect is x 271.0..295.1, y 12.9..22.9; the "Leading" caption occupies x 280.6..324,
    // y 2..12 — those do NOT overlap in y at this font size, so `left` is legitimately free and is the
    // right answer. What must never happen is the label being placed ABOVE, into the caption: that
    // rect is y 1.9..11.9, squarely on it.
    const out = placeLabels([{ pair: "AA/BB", x: 0.95, y: 1.0 }], PHONE);
    expect(out.map((p) => p.side)).not.toContain("above");
    expect(out).toEqual([{ pair: "AA/BB", side: "left" }]);
  });

  it("returns nothing when the box has not been measured yet", () => {
    // First render, before the ResizeObserver fires. Guessing a width would place labels against a box
    // that does not exist; drawing none for one frame is the honest answer.
    //
    // The `width > 0` clause is BELT AND BRACES and this test does not isolate it — with width 0 every
    // candidate is refused by the frame check anyway, so deleting the clause changes no behaviour.
    // Recorded rather than dressed up as coverage: a test that cannot fail carries no information, and
    // pretending otherwise is worse than saying so. The `height > 0` clause is NOT redundant — with
    // height 0 a dot near the horizontal centre places cleanly against the corner rects.
    expect(placeLabels([{ pair: "AA/BB", x: 0, y: 0 }], { width: 0, height: 224 })).toEqual([]);
    expect(placeLabels([{ pair: "AA/BB", x: 0, y: 0 }], { width: 328, height: 0 })).toEqual([]);
  });

  it("a crowded window shows FEWER labels rather than stacking them", () => {
    // #388: "A label that cannot be placed legibly should not be drawn — the dot's position is still
    // information." The cap is a ceiling, not a quota.
    const cluster = Array.from({ length: 8 }, (_, i) => ({
      pair: `A${i}/SPY`,
      x: 0.4 + i * 0.004,
      y: 0.3 + i * 0.004,
    }));
    const out = placeLabels(cluster, PHONE);
    expect(out.length).toBeLessThan(cluster.length);
    expect(new Set(out.map((p) => p.pair)).size).toBe(out.length);
  });
});

describe("labels avoid the MARKERS, not just each other (#388 follow-up)", () => {
  const PHONE2 = { width: 328, height: 224 };

  it("the fixture puts a dot exactly where the label would go", () => {
    // The fixture's own property first. If the obstacle were not in the label's path, the assertion
    // below would pass with dot-avoidance deleted.
    const labelled = { pair: "GLD/SPY", x: 0.0, y: 0.5 };
    // Inward side for x=0 is "right", so a marker just to the right is directly in the way.
    const blocker = { x: 0.12, y: 0.5 };
    const without = placeLabels([labelled], PHONE2, []);
    const withDot = placeLabels([labelled], PHONE2, [labelled, blocker]);
    expect(without[0].side).toBe("right");
    expect(withDot[0].side).not.toBe("right");
  });

  it("a label is never written THROUGH a dot", () => {
    // Operator, looking at the live map: `GLD/SPY` printed straight through a marker, and two more sat on
    // markers at the bottom of the cluster. Collision was label-vs-label and label-vs-corner only, so
    // a label could not land on another LABEL and could land anywhere on the DATA.
    const labelled = { pair: "GLD/SPY", x: 0.0, y: 0.5 };
    const dots = [labelled, { x: 0.12, y: 0.5 }, { x: -0.12, y: 0.5 }];
    const out = placeLabels([labelled], PHONE2, dots);
    // Both horizontal slots are blocked by markers, so it must go vertical or not at all.
    expect(["above", "below"]).toContain(out[0].side);
  });

  it("UNLABELLED markers are obstacles too", () => {
    // The dots passed in are every plotted point, not just the ranked ones. A cluster of unlabelled
    // markers is exactly where a label must not be written — and on the live map most markers are
    // unlabelled.
    const labelled = { pair: "AA/BB", x: 0.0, y: 0.0 };
    const crowd = [labelled, { x: 0.1, y: 0.0 }, { x: -0.1, y: 0.0 }, { x: 0.0, y: 0.12 }, { x: 0.0, y: -0.12 }];
    expect(placeLabels([labelled], PHONE2, crowd)).toEqual([]);
  });

  it("ABOVE and BELOW are real candidates, not decoration", () => {
    // The live map is a vertical cluster near the origin: horizontal slots are taken by neighbours and
    // the only free space is above and below. Two candidate positions is why so few labels were drawn.
    const labelled = { pair: "AA/BB", x: 0.0, y: 0.0 };
    const sideBlocked = [labelled, { x: 0.1, y: 0.0 }, { x: -0.1, y: 0.0 }];
    const out = placeLabels([labelled], PHONE2, sideBlocked);
    expect(out.length).toBe(1);
    expect(["above", "below"]).toContain(out[0].side);
  });

  it("more labels are placed than the old two-sided, dot-blind rule managed", () => {
    // The measured point of the change, on the real map's shape: a vertical cluster. Not a claim about
    // aesthetics — a count.
    const cluster = Array.from({ length: 12 }, (_, i) => ({
      pair: `A${i}/SPY`,
      x: 0.0 + (i % 2 ? 0.02 : -0.02),
      y: -0.5 + i * 0.09,
    }));
    const placed = placeLabels(cluster, PHONE2, cluster);
    expect(placed.length).toBeGreaterThan(4);
    // and none of them may sit on a marker
    expect(placed.length).toBeLessThanOrEqual(cluster.length);
  });

  it("a VERTICAL label off the top of a short plot is refused", () => {
    // The frame check had to grow a y-axis the moment labels could go above/below. On a short plot the
    // top inset puts a marker at cy 8, and an `above` rect runs to y -8 — off the frame. A first
    // version checked x only and placed it there.
    const SHORT = { width: 328, height: 100 };
    const top = { pair: "AA/BB", x: 0.0, y: 1.0 };
    const sidesBlocked = [top, { x: 0.12, y: 1.0 }, { x: -0.12, y: 1.0 }];
    const out = placeLabels([top], SHORT, sidesBlocked);
    expect(out.map((p) => p.side)).not.toContain("above");
  });

  it("an ABOVE label is CENTRED on its dot, not hung off one side", () => {
    // The rect the collision test measures must be the rect the component draws — `above` renders with
    // `translate(-50%, ...)`, i.e. centred. Measuring it left-aligned instead would let a label be
    // accepted in a slot it does not actually occupy, and the two would disagree about where the label
    // is. Detectable at the LEFT EDGE: centred needs w/2 of clearance and is refused; left-aligned
    // would fit and be wrongly accepted.
    const NARROW = { width: 120, height: 224 };
    const edge = { pair: "AAAA/BBBB", x: -0.98, y: 0.0 };
    const sidesBlocked = [edge, { x: -0.86, y: 0.0 }, { x: -1.0, y: 0.0 }];
    const out = placeLabels([edge], NARROW, sidesBlocked);
    expect(out.map((p) => p.side)).not.toContain("above");
    expect(out.map((p) => p.side)).not.toContain("below");
  });

  it("passing NO dots keeps the old behaviour — the parameter is additive", () => {
    // Every existing caller and test that omits `dots` must be unaffected, or this change is a silent
    // behaviour break dressed as an improvement.
    // Chosen so a phantom default obstacle at the ORIGIN would change the answer: this label's inward
    // side sweeps across the middle of the plot. A default of `[]` must mean no obstacles at all.
    const one = { pair: "AAAAAAAAAAAA/BBBB", x: 0.55, y: 0.0 };
    expect(placeLabels([one], PHONE2)).toEqual(placeLabels([one], PHONE2, []));
    expect(placeLabels([one], PHONE2).length).toBe(1);
  });
});

describe("the map wires the placement and makes dots tappable (#388)", () => {
  const SRC = readFileSync(join(import.meta.dirname, "MomentumMap.tsx"), "utf8");
  const code = SRC.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

  it("the scan reads the real component", () => {
    expect(SRC.length).toBeGreaterThan(1000);
    expect(code).toContain("MomentumMap");
  });

  it("calls placeLabels instead of offsetting every label blindly", () => {
    expect(code).toMatch(/placeLabels\(/);
    // The old unconditional render is banned by name so a revert is loud.
    expect(code).not.toMatch(/named\.has\(/);
  });

  it("MEASURES the box, because collision needs pixels", () => {
    expect(code).toMatch(/ResizeObserver/);
  });

  it("a dot is a BUTTON, and `title` is gone", () => {
    // `title` renders as a hover tooltip on a desktop and does nothing at all on a touch device — it
    // is not an interaction. Banned by name: it is the thing that looked like a fix and was not.
    expect(code).toMatch(/<button/);
    expect(code).toMatch(/onClick=/);
    expect(code).not.toMatch(/\stitle=\{/);
  });

  it("keeps the dot reachable by keyboard and announced to a screen reader", () => {
    expect(code).toMatch(/aria-label=/);
    expect(code).toMatch(/focus-visible:/);
  });

  it("selecting a dot names it somewhere the user can read", () => {
    // The whole point of replacing `title`: a tap has to produce an answer on screen.
    expect(code).toMatch(/chosen/);
    expect(code).toMatch(/quadrant/);
  });

  it("passes EVERY plotted marker as an obstacle, not just the ranked ones", () => {
    // An unlabelled marker is still something a label must not be written through, and on the live map
    // most markers are unlabelled. Passing only `ranked` would leave the majority unprotected.
    expect(code).toMatch(/points\.map\(\(p\) => \(\{ x: p\.point\.x, y: p\.point\.y \}\)\)/);
  });

  it("the cap is a CEILING over the whole set, not a quota of eight", () => {
    // It was 8 (5 when short) because labels were drawn unconditionally. `placeLabels` now refuses what
    // it cannot draw, so the geometry decides and the cap is the whole set. Reverting to 8 silently
    // caps the map at eight names however much room there is.
    expect(code).toMatch(/const NAMED = 25;/);
    expect(code).not.toMatch(/const NAMED = 8;/);
  });

  it("the hit area is bigger than the 8px dot", () => {
    // An 8px target is below every touch guideline, and "can't even click" is the reported symptom.
    expect(code).toMatch(/h-6 w-6/);
  });
});
