import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import {
  driftLabel,
  gateOf,
  isConfirmed,
  leaderLabel,
  legTone,
  marketView,
  railDomain,
  railMarks,
  railPosition,
  readVerdict,
  sortAxes,
} from "./rotationRead";
import type { RotationAxis, RotationWindow } from "@/lib/api/client";
import live from "./__fixtures__/rotation.live.json";

const AXES = live.axes as unknown as RotationAxis[];
import { allSources } from "@/lib/framework/datasource/registry";
import { allTiles } from "@/lib/framework/registry";
import { marketDefinition } from "./definition";
// Side-effect imports: these are the composition roots that populate the two registries. Without them
// both are empty and every assertion below passes vacuously — which is why the counts are asserted first.
import "@/config/tiles";
import "@/config/datasources";

/**
 * #351 — the tile must read without Ichimoku knowledge and must never overstate the drift.
 *
 * Fixtures are the REAL payload emitted 2026-08-19T18:05Z (25 axes, 0 errors), not invented shapes.
 */
describe("reading a rotation verdict (#351)", () => {
  it("translates every gate state into plain words, with the winning leg kept separate", () => {
    // The fixture's own property first: the five states must map to DISTINCT reads/winners, or the
    // assertions below would pass against a function that returned one constant.
    const all = ["🟢 ON", "ON-wk", "TURN", "OFF-wk", "🔴 OFF"].map(readVerdict);
    expect(new Set(all.map((r) => `${r.read}|${r.winner}`)).size).toBe(5);

    expect(readVerdict("🟢 ON")).toEqual({ read: "Trending", winner: "num" });
    expect(readVerdict("ON-wk")).toEqual({ read: "Pulling back", winner: "num" });
    expect(readVerdict("TURN")).toEqual({ read: "No trend", winner: null });
    expect(readVerdict("OFF-wk")).toEqual({ read: "Bouncing", winner: "den" });
    expect(readVerdict("🔴 OFF")).toEqual({ read: "Trending", winner: "den" });
  });

  it("OFF is TRENDING with the denominator winning — not weakness", () => {
    // The half that a single-axis read loses. `OFF` is the same trending path as `ON` with the other
    // leg in front; rendering it as "no trend" would invert the read on every axis where the second
    // name is the one to own.
    const off = readVerdict("OFF");
    expect(off.read).toBe("Trending");
    expect(off.winner).toBe("den");
  });

  it("no user-facing word is gate vocabulary", () => {
    const words = ["🟢 ON", "ON-wk", "TURN", "OFF-wk", "OFF", "nonsense"].map((v) => readVerdict(v).read);
    for (const w of words) {
      expect(w).not.toMatch(/\b(ON|OFF|TURN|wk|cloud|tenkan|ichimoku|gate|adx)\b/i);
    }
  });

  it("an unrecognised verdict does not silently become a market claim", () => {
    // Matched on the trailing token, so an upstream emoji change cannot turn every axis into one read.
    expect(readVerdict("🟢 ON").read).toBe("Trending");
    expect(readVerdict(undefined).winner).toBeNull();
    expect(readVerdict("").winner).toBeNull();
  });
});

describe("drift is never a bare point estimate (#351)", () => {
  // IWM/SPY, 1M window, exactly as emitted.
  const REAL = { est: -1.091491956898516, lo: -7.089795426124546, hi: 5.294063318239961, sig: false };

  it("shows the interval alongside the estimate", () => {
    expect(driftLabel(REAL)).toBe("-1.09% (-7.09% … +5.29%)");
  });

  it("returns NOTHING rather than the estimate alone when the interval is missing", () => {
    // The failure mode this rule exists for: degrading to the most confident number exactly when the
    // data is weakest. A tile with no interval must show no percentage.
    expect(driftLabel({ est: -1.09, lo: null, hi: 5.29 })).toBeNull();
    expect(driftLabel({ est: -1.09 })).toBeNull();
    expect(driftLabel({ est: NaN, lo: -7, hi: 5 })).toBeNull();
    expect(driftLabel(null)).toBeNull();
  });

  it("a straddling interval is still shown — that IS the signal", () => {
    // -7.09 … +5.29 straddles zero and `sig` is false. The tile shows it rather than hiding it or
    // ranking on the midpoint, which is the whole argument of the issue.
    expect(REAL.lo).toBeLessThan(0);
    expect(REAL.hi).toBeGreaterThan(0);
    expect(driftLabel(REAL)).toContain("…");
  });
});

describe("rail position (#351)", () => {
  it("scales by the window's own interval so small and large movers are both legible", () => {
    const tight = railPosition({ est: 0.5, lo: 0.4, hi: 0.6 });
    const wide = railPosition({ est: 0.5, lo: -10, hi: 11 });
    expect(tight).toBeGreaterThan(wide!);
  });

  it("is clamped to the rail — an off-scale dot is a broken layout, not more information", () => {
    expect(railPosition({ est: 500, lo: -1, hi: 1 })).toBe(1);
    expect(railPosition({ est: -500, lo: -1, hi: 1 })).toBe(-1);
  });

  it("is null when the window cannot place it", () => {
    expect(railPosition({ est: 1, lo: null, hi: 2 })).toBeNull();
    expect(railPosition(undefined)).toBeNull();
  });
});

/**
 * The tile's own absence rules, pinned on its source (#351).
 *
 * Both of these shipped broken and were caught in the BROWSER, not by a test — which is the point of
 * writing them down. A tile that renders correctly in isolation can still be wrong in place.
 */
describe("absence is not an empty market (#351)", () => {
  // These tests replaced a set that scanned MarketTile.tsx for `useQuery`, `isError` and
  // `fetchStatus === "idle"`. Every one of them broke when the tile moved onto the framework's bound
  // source, while the behaviour they existed to defend was preserved exactly — they were pinned to
  // react-query's vocabulary rather than to the cockpit's rule. The rule is what is asserted now.

  it("reports a payload-level error as absence, whatever the transport thinks", () => {
    // The api answered and what it said was "nobody generated a read". That outranks a healthy source.
    expect(marketView({ error: "rotation.json not found", axes: [] }, "live")).toEqual({
      kind: "absent",
      reason: "rotation.json not found",
    });
  });

  it("reports a dead source as absence rather than as an empty market", () => {
    // The original bug, exactly: the api did not serve the route, the payload was undefined, and the
    // render fell through to "No rotations to show" — a dead api rendering as a calm tape.
    expect(marketView(undefined, "error").kind).toBe("absent");
  });

  it("reports a dead source as absent even while it still holds a payload", () => {
    // The name of this test used to claim it defended the ORDER of the loading and error checks. It does
    // not — swapping them leaves it green, because a present payload never reaches the loading branch.
    // What it actually pins is that a stale payload does not paper over a dead source, which is worth
    // pinning on its own: yesterday's rotation rendering as today's is the quiet version of the bug.
    expect(marketView({ axes: [{}] }, "error").kind).toBe("absent");
  });

  it("never says 'empty' while there is no payload at all", () => {
    // The second bug, one branch over, which survived the first fix: `isLoading` dropped between
    // retries, so a request still retrying against a 404 announced an empty market about an api it had
    // never reached. Empty must be reachable only once something has answered.
    expect(marketView(undefined, "loading").kind).toBe("loading");
    expect(marketView(undefined, undefined).kind).toBe("loading");
    expect(marketView(null, "live").kind).toBe("loading");
  });

  it("keeps showing a stale payload rather than blanking a once-a-day read", () => {
    expect(marketView({ axes: [{}] }, "stale").kind).toBe("rotations");
  });

  it("says empty only when the api answered with no axes", () => {
    expect(marketView({ axes: [] }, "live").kind).toBe("empty");
  });
});

describe("MarketTile conforms to the tile framework (#351)", () => {
  const SRC = readFileSync(join(import.meta.dirname, "MarketTile.tsx"), "utf8");
  const code = SRC.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

  it("the scan reads the real component", () => {
    expect(SRC.length).toBeGreaterThan(1000);
    expect(code).toContain("MarketTile");
  });

  it("does not fetch — it binds a source by name", () => {
    // `ui-framework-spec` §1d: "Tiles never fetch directly — the container owns all data wiring." The
    // first cut broke this and justified it by citing the Pool tile, whose exemption is argued from a
    // mutation path a read-only snapshot does not have.
    expect(code).not.toMatch(/\buseQuery\b/);
    expect(code).not.toMatch(/\bgetRotation\b/);
    expect(code).not.toMatch(/\bfetch\(/);
    expect(marketDefinition.dataSources).toContain("rotation");
  });

  it("every source any tile declares is actually registered", () => {
    // AIMED AT THE CLASS, NOT THIS TILE. A definition naming a source nobody registered resolves to
    // undefined data and renders as a permanently loading tile — silent, and indistinguishable from a
    // slow api. Checked across every registered tile, because the typo is the thing being defended
    // against.
    //
    // This was a regex over source text until `oxc.jsx` was set in the vitest config: tsconfig says
    // `jsx: "preserve"`, so vitest could not parse a .tsx and importing `@/config/tiles` failed before
    // any test ran. Reading the real registries catches a name in any form a definition can express it,
    // which a regex over `dataSources: [...]` cannot.
    const registered = new Set(allSources().map((src) => src.name));
    const tiles = allTiles();

    // THE COMPOSITION ROOTS ACTUALLY RAN. Both registries are populated by side-effect imports, and an
    // empty registry would make the real assertion below pass while checking nothing.
    expect(registered.size).toBeGreaterThan(3);
    expect(tiles.length).toBeGreaterThan(5);

    const missing = tiles.flatMap((t) =>
      (t.dataSources ?? []).filter((n) => !registered.has(n)).map((n) => `${t.type} → ${n}`),
    );
    expect(missing).toEqual([]);
  });

  it("rotation is a WS PLANE, like every other fact the board reads (#384)", () => {
    // Operator, 2026-08-21: "It should work similar to trend lines, watch, portfolio etc."
    //
    // It was `kind: "rest"` polling `/market/rotation` every 60s — because the payload used to be a
    // FILE written by a sidecar, so there was no publisher to subscribe to and the tile polled the api
    // which polled the disk. Both are gone: the engine grades the axes off our own bars and publishes
    // `rotation` on the bus exactly as it publishes trades, session, account and equity_curve.
    //
    // The difference is not tidiness. A poll asks "anything new?" on a fixed clock and cannot tell a
    // fresh answer from a stale one — which is precisely how a dead sidecar served a four-hour-old
    // payload that looked perfectly healthy on 2026-08-21.
    const rotation = allSources().find((src) => src.name === "rotation");
    expect(rotation).toBeDefined();
    expect(rotation!.kind).toBe("ws");
    expect(rotation!.channel).toBe("rotation");
    expect(rotation!.endpoint).toBeUndefined();
  });

  it("the board's display facts are planes, not polls — as a CLASS", () => {
    // Aimed at the class rather than at rotation. `positions` is the one deliberate REST source left
    // (it predates the trades plane and is superseded by it), and `managers` is REST because the engine
    // publishes no such channel — those two are named so the exception stays deliberate rather than
    // becoming the pattern. Anything else arriving as a poll is a regression toward the file era.
    const REST_BY_DESIGN = new Set(["positions", "managers"]);
    const polls = allSources()
      .filter((src) => src.kind === "rest" && !REST_BY_DESIGN.has(src.name))
      .map((src) => src.name);
    expect(polls).toEqual([]);
  });

  it("does not mount a second period selector", () => {
    // The Board renders the global one and this tile reads the same store. Mounting its own put two
    // identical controls on screen — #336 established that reads as two independent scopes when it is
    // one, and the Equity tile lost its private selector for exactly this reason.
    expect(code).not.toMatch(/<PeriodSelector\b/);
  });
});

describe("the rail draws five different facts, in the axis's colour (#351)", () => {
  // Operator, seeing the first cut on a phone: "hard to read … no colors … I want the bar and bullets to
  // have color and the size in the mockup had meaning. small not filled bullet previous, big filled
  // bullet now." The reference build draws band, core, ghost, move+arrow and dot; the first cut drew
  // two grey dots on a hairline, which is one undifferentiated line per row.

  it("colours an axis by which leg is winning", () => {
    expect(gateOf("🟢 ON")).toBe("bull");
    expect(gateOf("ON-wk")).toBe("bull");
    expect(gateOf("OFF")).toBe("bear");
    expect(gateOf("OFF-wk")).toBe("bear");
    expect(gateOf("TURN")).toBe("watch");
    // An unrecognised verdict must not borrow a directional colour it has not earned.
    expect(gateOf("🟣 SOMETHING-NEW")).toBe("watch");
  });

  it("fills the dot only for a CONFIRMED gate", () => {
    // The fill is the gate, which is why a weakening trend is outlined and a confirmed one solid.
    expect(isConfirmed("🟢 ON")).toBe(true);
    expect(isConfirmed("OFF")).toBe(true);
    expect(isConfirmed("ON-wk")).toBe(false);
    expect(isConfirmed("OFF-wk")).toBe(false);
    expect(isConfirmed("TURN")).toBe(false);
  });
});

describe("one rail domain for every row (#351)", () => {
  it("snaps to a round bound that covers the bulk of the set", () => {
    const wins = [
      { est: 1, lo: -2, hi: 4 },
      { est: -1, lo: -3, hi: 1 },
      { est: 0.5, lo: -1, hi: 2 },
    ];
    const d = railDomain(wins);
    expect([1, 2, 3, 5, 8, 12, 20, 30, 50, 80, 120]).toContain(d);
    expect(d).toBeGreaterThanOrEqual(3);
  });

  it("is ONE number for the whole set, so two rows of different magnitude look different", () => {
    // THE REGRESSION THIS EXISTS FOR. The first cut scaled every rail by its OWN interval, so an axis
    // drifting in tenths and one drifting in whole percents rendered identically and the column could
    // not be read as a column — half of "hard to read". A shared domain means the position of a dot
    // means the same thing on every row.
    const wins = [
      { est: 0.2, lo: -0.3, hi: 0.7 },
      { est: 9, lo: 4, hi: 14 },
    ];
    const d = railDomain(wins);
    const small = railMarks(wins[0], d)!;
    const big = railMarks(wins[1], d)!;
    expect(Math.abs(big.est - 50)).toBeGreaterThan(Math.abs(small.est - 50) + 20);
  });

  it("survives a set with nothing in it", () => {
    expect(railDomain([])).toBeGreaterThan(0);
    expect(railDomain([null, undefined])).toBeGreaterThan(0);
  });

  it("SPREADS the live payload's rows across the rail instead of smearing them at the centre", () => {
    // MEASURED, AND PINNED AGAINST THE OBVIOUS WRONG ANSWER. Scaling to the widest value in the set
    // rather than to the 85th percentile is the tempting simplification, and on the payload the api
    // served it collapses all 25 rows into 15.7% of the rail — a smear either side of zero, which is
    // "hard to read" again with a different cause. The percentile rule spreads the same rows across
    // 62.7%, letting the few genuine extremes clamp instead of flattening everyone to accommodate them.
    //
    // An earlier version of this test asserted only that rows were not pinned at the ends. Both rules
    // pass that, so it could not fail — the number below is the one that separates them.
    const wins = AXES.map((a) => a.win?.["1M"]).filter((w): w is RotationWindow => !!w);
    const d = railDomain(wins);
    const est = wins
      .map((w) => railMarks(w, d))
      .filter((m): m is NonNullable<typeof m> => m !== null)
      .map((m) => m.est);
    expect(est.length).toBe(25); // the fixture still carries the rows this was measured on
    expect(Math.max(...est) - Math.min(...est)).toBeGreaterThan(40);
    // And nothing pinned to an end, which the percentile rule also has to keep true.
    expect(est.filter((e) => e <= 2 || e >= 98)).toEqual([]);
  });
});

describe("railMarks", () => {
  const D = 10;

  it("nests the standard-error core inside the 95% band", () => {
    const m = railMarks({ est: 0, lo: -4, hi: 4 }, D)!;
    expect(m.lo).toBeLessThan(m.core1);
    expect(m.core2).toBeLessThan(m.hi);
    expect(m.est).toBe(50);
  });

  it("carries the previous position and the direction travelled", () => {
    const up = railMarks({ est: 4, lo: 0, hi: 8, prev: -4 }, D)!;
    expect(up.prev).toBeLessThan(up.est);
    expect(up.moveRight).toBe(true);
    expect(up.moveWidth).toBeGreaterThan(0);

    const down = railMarks({ est: -4, lo: -8, hi: 0, prev: 4 }, D)!;
    expect(down.moveRight).toBe(false);
  });

  it("suppresses a move too small to draw rather than rendering a smudge", () => {
    // Below ~1.6% of the rail the bar, its arrowhead and the two dots overlap into one blob that reads
    // as a defect. No bar is the honest rendering of "it barely moved".
    const m = railMarks({ est: 0.05, lo: -3, hi: 3, prev: 0.0 }, D)!;
    expect(m.prev).not.toBeNull();
    expect(m.moveWidth).toBeNull();
  });

  it("returns null rather than a confident-looking mark when the interval is missing", () => {
    expect(railMarks({ est: 1 }, D)).toBeNull();
    expect(railMarks(undefined, D)).toBeNull();
  });
});

describe("ordering (#351)", () => {
  const axis = (pair: string, verdict: string, adx: number, est: number) => ({
    pair,
    verdict,
    adx,
    win: { "1M": { est, lo: est - 5, hi: est + 5, move: est / 2 } },
  });
  const winOf = (a: ReturnType<typeof axis>) => a.win["1M"];

  it("puts confirmed trends on top by default, not the biggest percentage", () => {
    // The operator asked for "most trending on top". #351 argues the same thing from the data: ranking by
    // percentage manufactures conviction the numbers do not support, and "the discriminating signal is
    // PERSISTENCE (the gate, ADX) — a property of the path, not of the mean". The two agree, so the
    // default is the gate — and the axis with by far the largest drift must NOT lead if its gate is
    // dead.
    const axes = [
      axis("BIG/DRIFT", "TURN", 9, 40),
      axis("WEAK/GATE", "ON-wk", 30, 1),
      axis("REAL/TREND", "ON", 22, 2),
    ];
    expect(sortAxes(axes, "trend", winOf).map((a) => a.pair)).toEqual([
      "REAL/TREND",
      "WEAK/GATE",
      "BIG/DRIFT",
    ]);
  });

  it("breaks ties inside a band by ADX", () => {
    const axes = [axis("A/B", "ON", 12, 5), axis("C/D", "ON", 31, 1)];
    expect(sortAxes(axes, "trend", winOf)[0].pair).toBe("C/D");
  });

  it("ranks by drift only where the operator has explicitly asked for it", () => {
    const axes = [axis("A/B", "ON", 40, 1), axis("C/D", "TURN", 2, 40)];
    expect(sortAxes(axes, "drift", winOf)[0].pair).toBe("C/D");
    expect(sortAxes(axes, "travel", winOf)[0].pair).toBe("C/D");
    expect(sortAxes(axes, "pair", winOf)[0].pair).toBe("A/B");
  });

  it("does not mutate the array it was given", () => {
    const axes = [axis("Z/Z", "TURN", 1, 1), axis("A/A", "ON", 1, 1)];
    const before = axes.map((a) => a.pair);
    sortAxes(axes, "trend", winOf);
    expect(axes.map((a) => a.pair)).toEqual(before);
  });
});

// ==================================================================================================
// #389 — ON and OFF both rendered the single word "Trending", so COLOUR carried the whole direction.
//
//     XLE/SPY   Trending  (green)   +1.71%     <- energy beating the market
//     IWF/IWD   Trending  (red)     -0.33%     <- value beating growth
//     MDY/SPY   Trending  (red)     +0.00%     <- the market beating mid caps
//
// Operator: "I'm not sure energy is trending or market (which wouldn't be a trend)." Exactly the ambiguity.
// A ratio has no direction of its own — XLE/SPY rising means energy beating the market and the same
// path falling means the market beating energy. "Trending" describes the PATH and says nothing about
// the SIDE.
//
// This breaks #351's own acceptance criterion, quoted from `globals.css`: "Colour never alone — the
// Read word ships with it at every breakpoint." The word shipped; it was the same word for both
// directions, so it carried no information and colour did all the work. In greyscale, or for a
// colourblind reader, the tile was unreadable.
// ==================================================================================================

describe("the leader is named, not just coloured (#389)", () => {
  it("the fixture actually contains the collision", () => {
    // The fixture's own property first. If ON and OFF produced different Read words already, every
    // assertion below would pass with the fix reverted.
    expect(readVerdict("🟢 ON").read).toBe(readVerdict("🔴 OFF").read);
    expect(readVerdict("🟢 ON").winner).not.toBe(readVerdict("🔴 OFF").winner);
  });

  it("ON and OFF produce DIFFERENT text for the same pair", () => {
    const on = leaderLabel("🟢 ON", "XLE", "SPY");
    const off = leaderLabel("🔴 OFF", "XLE", "SPY");
    expect(on).not.toBe(off);
    expect(on).toBe("XLE leads");
    expect(off).toBe("SPY leads");
  });

  it("keeps the PATH state as a modifier rather than dropping it", () => {
    // The path information was the only thing the old word carried, and it is still worth having —
    // "leading but pausing" is a different read from "leading". It just cannot be the whole message.
    expect(leaderLabel("🟡 ON-wk", "XLE", "SPY")).toBe("XLE leads · pausing");
    expect(leaderLabel("🟡 OFF-wk", "XLE", "SPY")).toBe("SPY leads · fading");
  });

  it("says NO GATE rather than naming a leader that does not exist", () => {
    // TURN and any unrecognised verdict have no winning leg. Naming one would be a claim about the
    // market that the data does not support — the same rule the drift line follows.
    expect(leaderLabel("TURN", "XLE", "SPY")).toBe("no gate");
    expect(leaderLabel(null, "XLE", "SPY")).toBe("no gate");
    expect(leaderLabel("nonsense", "XLE", "SPY")).toBe("no gate");
  });

  it("the label survives greyscale — it is readable with no colour at all", () => {
    // The actual acceptance test for #389. Strip every tone and the row must still say who is winning.
    const rows = ["🟢 ON", "🟡 ON-wk", "🔴 OFF", "🟡 OFF-wk", "TURN"].map((v) =>
      leaderLabel(v, "IWF", "IWD"),
    );
    expect(new Set(rows).size).toBe(rows.length); // every state reads differently
    expect(rows.filter((r) => r.startsWith("IWF")).length).toBe(2);
    expect(rows.filter((r) => r.startsWith("IWD")).length).toBe(2);
  });
});

describe("the pair's own legs carry the direction (#389)", () => {
  it("winner and loser get OPPOSITE tones", () => {
    expect(legTone("🟢 ON", "num")).toBe("bull");
    expect(legTone("🟢 ON", "den")).toBe("bear");
    expect(legTone("🔴 OFF", "num")).toBe("bear");
    expect(legTone("🔴 OFF", "den")).toBe("bull");
  });

  it("no gate means NEITHER leg is coloured as a winner", () => {
    // Operator, on the colour scheme: the losing side should be red and the winning green. With no gate
    // there is no winning side, and painting one green would assert a read that does not exist.
    expect(legTone("TURN", "num")).toBe("watch");
    expect(legTone("TURN", "den")).toBe("watch");
  });

  it("agrees with the verdict word — two derivations of one direction", () => {
    // CLAUDE.md: when a fact is derived in two places, pin that they use the same predicate. The leg
    // tone and the leader label both answer "which side is winning", and a row whose green leg
    // disagreed with its own label would be worse than the bug being fixed.
    for (const v of ["🟢 ON", "🟡 ON-wk", "🔴 OFF", "🟡 OFF-wk", "TURN"]) {
      const label = leaderLabel(v, "XLE", "SPY");
      if (label === "no gate") {
        expect(legTone(v, "num")).toBe("watch");
        continue;
      }
      const leader = label.startsWith("XLE") ? "num" : "den";
      expect(legTone(v, leader as "num" | "den")).toBe("bull");
      expect(legTone(v, leader === "num" ? "den" : "num")).toBe("bear");
    }
  });
});


// ==================================================================================================
// THE SEAM. `leaderLabel` and `legTone` were WRITTEN, EXPORTED AND TESTED before this change and the
// tile called neither of them — the row went on rendering `read` and a neutral pair for days while a
// green suite reported the helpers correct.
//
// That is the failure CLAUDE.md names: "a passing test on a helper says nothing about whether anything
// calls it correctly", and it is why deleting PEAK's ATR-scaling call at attach passed the entire repo.
// A source scan because this project has no rendered-component tests and jsdom computes no Tailwind, so
// a DOM assertion would pass while the phone stayed wrong — same reasoning as `trendStripWidth.test.ts`
// and `mobileWidth.test.ts`.
// ==================================================================================================

describe("the tile actually CALLS the direction helpers (#389)", () => {
  const TILE = readFileSync(join(import.meta.dirname, "MarketTile.tsx"), "utf8");
  const code = TILE.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

  it("the scan reads the real component", () => {
    expect(TILE.length).toBeGreaterThan(1000);
    expect(code).toContain("AxisRow");
  });

  it("renders the LEADER LABEL, not the bare Read word", () => {
    expect(code).toMatch(/leaderLabel\(\s*axis\.verdict/);
    // The bare `{read}` render is what produced "Trending" on both sides. Banned by name so a revert
    // is loud rather than quiet.
    expect(code).not.toMatch(/>\s*\{read\}\s*</);
  });

  it("colours the pair's own legs", () => {
    expect(code).toMatch(/legTone\(axis\.verdict,\s*"num"\)/);
    expect(code).toMatch(/legTone\(axis\.verdict,\s*"den"\)/);
    // and it must render the legs separately rather than the pre-joined string, or there is nothing
    // to colour independently
    expect(code).toMatch(/\{axis\.num\}/);
    expect(code).toMatch(/\{axis\.den\}/);
    expect(code).not.toMatch(/\{axis\.pair\}/);
  });

  it("keeps ONE tone->class map rather than a second inline copy", () => {
    // The rail already mapped Gate -> class inline. Two copies of one mapping drift, and a row whose
    // leg colour disagreed with its own rail would be worse than the bug being fixed.
    expect(code).toMatch(/function toneClass\(/);
  });
});
