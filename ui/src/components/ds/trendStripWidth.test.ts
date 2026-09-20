/**
 * The trend strip must not be the thing that makes a row wider than a phone (#294).
 *
 * WHY THIS IS A SOURCE SCAN. There are no rendered-component tests in this project, and the property
 * is a layout class — jsdom computes no Tailwind, so a DOM assertion would pass while the phone stayed
 * broken. Same reasoning, and the same technique, as `lib/design/mobileWidth.test.ts`.
 *
 * WHAT WAS MEASURED, in a real browser, on the live UI with 20 watchlist rows:
 *
 *     single row, toFixed(2) labels, 30px charts   265px   (overflowed)
 *     3-wide grid                                 137px   (fit, but broke the row in two - REJECTED)
 *     single row, pctLabel, 24px charts           ~194px  (this)
 *
 * The strip's column is 204px wide, set by the `TK - KJ - Cld` line above it. That is the allowance;
 * 194px fits inside it with all six timeframes on one line.
 *
 * The six cells are ~24px of sparkline each, but the % label under them could read "+271.45%", which is
 * what pushed the strip past 250px and `Last`, `Cld`, `Vol` and the 5Y column off the viewport.
 *
 * The overflow was not merely a legibility problem. Reaching the clipped columns meant scrolling the
 * row leftward, and a leftward drag is ALSO what arms slide-to-delete (`WatchlistTile.tsx:264`) — so
 * the only gesture that revealed the hidden content was the destructive one. Removing the width is
 * what removes the scroll, and removing the scroll is what disambiguates the gesture.
 *
 * NOT VERIFIED HERE: that a row fits 390px end to end. That needs a real 390px viewport, and the
 * browser tooling available could not emulate one — `matchMedia('(min-width: 640px)')` still matched,
 * so every in-browser measurement was of the desktop layout. The strip halving is measured; the
 * viewport acceptance in #294 is not, and should be checked on a phone before that issue is closed.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = readFileSync(join(import.meta.dirname, "TrendStrip.tsx"), "utf8");
const code = SRC.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

describe("TrendStrip stays narrow (#294)", () => {
  it("the scan reads the real component", () => {
    // The fixture's own property first — a path typo would make everything below vacuous.
    expect(SRC.length).toBeGreaterThan(500);
    expect(code).toContain("Sparkline");
  });

  it("keeps the six windows on ONE row", () => {
    // THE FIRST FIX FOR #294 WAS THE WRONG FIX. It re-flowed the six into a 3-wide grid: 265px became
    // 137px and the row fit, but it fit by breaking the strip onto two lines. Operator: "You were to fit
    // the width to my iPhone, but not by introducing a line break." Six timeframes are a sequence; two
    // rows of three read as two groups.
    //
    // Banned by name so a revert to either of the two known-wrong layouts is loud rather than quiet.
    expect(code).not.toMatch(/grid-cols-/);
    expect(code).not.toMatch(/flex-wrap/);
    // The PROPERTY, not the literal class string. The first version of this assertion pinned
    // `className="flex gap-1 pt-0.5"` exactly and broke the moment `w-full` was added — failing for a
    // change that preserved everything it was defending. Same mistake as the tests that pinned
    // react-query's vocabulary in the Market tile.
    expect(code).toMatch(/<div className="flex[^"]*"/);
  });

  it("fills the width it is given rather than picking a pixel size", () => {
    // Three fixed sizes were tried and all three were wrong somewhere: 30px overflowed a phone, the
    // grid broke the row in two, 24px was too small to read. Measured in the browser: the left half of
    // the row is 24px of cloud chips against a 374px split, and the right column had been self-limiting
    // to 204px because the levels line above happened to measure that. The width was always there.
    expect(code).toMatch(/w-full/);
    expect(code).toMatch(/flex-1/);
    // `min-w-0` is load-bearing: without it a flex child will not shrink below its content, so the
    // widest % label sets the floor again — which is the bug this layout replaced.
    expect(code).toMatch(/min-w-0/);
    expect(code).toMatch(/fluid/);
  });

  it("caps the chart height on desktop instead of growing in both directions", () => {
    // Operator: "have a max height for desktop". Width is free, height is not — a chart that grows both
    // ways turns a dense list row into a dashboard on a wide screen.
    expect(code).toMatch(/sm:h-\[\d+px\]/);
  });

  it("buys the width back from the LABELS, which is where it was being spent", () => {
    // The charts were never the problem: 24-30px each. `toFixed(2)` was, rendering "+254.65%" — eight
    // characters — under a 30px chart, six times over. `pctLabel` is where that is decided, and it is a
    // pure function so the widths below can actually be asserted.
    expect(code).toMatch(/pctLabel\(/);
    expect(code).not.toMatch(/toFixed\(2\)[^;]*%`\}/); // no inline 2dp label rendering
  });

  it("still renders all six windows — narrowing must not drop a timeframe", () => {
    // Truncation is not a fix here. #294 is explicit: "Fit the screen width. Do not remove content."
    // The 5Y column was one of the things being lost off the right edge.
    expect(code).toMatch(/windows\.map/);
    expect(code).not.toMatch(/slice\(0,\s*[0-5]\)/);
  });
});

describe("the strip is shared, not copied (#251)", () => {
  it("Portfolio uses the same TrendStrip primitive as Watch", () => {
    // #251 asked for trendlines on "the screen you actually hold positions on", and blocked itself on the
    // width: "do not add it to a second tile until that is resolved, or the same problem doubles". #294
    // resolved it by making the strip fluid, so the second tile wires the SAME component rather than a
    // portfolio-flavoured copy — two strips that look identical and diverge is the trap the issue named.
    const portfolio = readFileSync(
      join(import.meta.dirname, "..", "..", "tiles", "managed-portfolio", "ManagedPortfolioTile.tsx"),
      "utf8",
    );
    expect(portfolio).toMatch(/import \{ TrendStrip \} from "@\/components\/ds\/TrendStrip"/);
    expect(portfolio).toMatch(/buildTrendWindows/);
    // and it must NOT hand-roll its own sparkline row
    expect(portfolio).not.toMatch(/grid-cols-6|<Sparkline/);
  });
})
