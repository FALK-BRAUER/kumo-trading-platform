/**
 * The Book panel must actually SWITCH — and only the figures that may (#586, rule #392).
 *
 * `unit.test.ts` proves the arithmetic. That says nothing about whether the panel calls it: a correct
 * helper nothing routes through is the defect with extra steps, and this repo has shipped that shape
 * repeatedly (`_lane_symbols` defined and never called; #606 wired into one of two providers). This is
 * the seam.
 *
 * IT IS ALSO THE CLASS GUARD. The risk is not that today's figures are wrong — they are asserted one
 * by one below. It is that a figure added later quietly renders `fmtUsd` and never switches, or worse,
 * switches a BALANCE and invents a denominator. So the last test asserts the property over every
 * `<Metric>` in the file rather than over a list someone has to remember to extend.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const code = readFileSync(join(__dirname, "BookTile.tsx"), "utf8");

describe("the unit toggle is wired into the panel", () => {
  it("renders the control ONCE, on the period row, opposite the period selector", () => {
    // #586 pairs the two axes of one question — unit and window. The first version put the toggle
    // beside the BOOK heading, which is a DIFFERENT ROW from the selector and left the period row's
    // left half empty (2026-09-06: "the placement is completely wrong"). It lives on the Board,
    // which also makes it global — matching the store it reads and #392's cross-screen rule.
    const board = readFileSync(join(__dirname, "../../components/board/Board.tsx"), "utf8");
    expect(board).toMatch(/<UnitToggle\s*\/>/);
    // Opposite, not stacked: same flex row as the selector, justified apart.
    expect(board).toMatch(/justify-between[\s\S]{0,120}<UnitToggle\s*\/>[\s\S]{0,80}<PeriodSelector\s*\/>/);
    // And NOT duplicated into the tile — two controls writing one store is how they drift apart.
    expect(code).not.toMatch(/<UnitToggle\s*\/>/);
  });

  it("routes every RELATIVE-CHANGE figure through the switch, with its decided denominator", () => {
    for (const [expr, figure] of [
      ["net", "net"],
      ["realized", "realized"],
      ["dUnrealized", "dUnrealized"],
      ["secured.value", "secured"],
    ] as const) {
      expect(code, `${figure} does not switch`).toMatch(
        new RegExp(`inUnit\\(${expr.replace(".", "\\.")}, "${figure}"\\)`),
      );
    }
  });

  it("takes the window denominator from the SAME derivation NET is built from", () => {
    // A second "equity at the window's start" computed here would be two derivations of one fact, and
    // `periodNet.ts` already documents what that costs. `baseValue` is imported, not reimplemented.
    expect(code).toMatch(/import \{ baseValue,/);
    expect(code).toMatch(/const windowBaseEquity = baseValue\(/);
    // And it must NOT be confused with `useWindowBase()`, which is the per-lane UNREALIZED base — a
    // different fact with a different shape. The first draft shadowed it and inherited its type.
    expect(code).toMatch(/const windowBase = useWindowBase\(\)/);
  });

  it("leaves BALANCES absolute — cash and liquidation never switch", () => {
    // "% of what" has no non-circular answer for a balance. If either ever routes through `inUnit`,
    // the panel has started inventing a denominator.
    const balanceLines = code
      .split("\n")
      .filter((l) => /label="(Cash|Liquidation)/.test(l) || /label={`(Cash|Liquidation)/.test(l));
    expect(balanceLines.length, "the balance cells moved — this guard can no longer see them").toBeGreaterThan(0);
    for (const line of balanceLines) expect(line).not.toMatch(/inUnit\(/);
  });

  it("REFUSES rather than falling back to dollars when no percentage can be formed (#1077)", () => {
    // Two renderers of one toggle lived in this file and disagreed: the lane cell's `laneFigure`
    // rendered `—` where `inUnit` rendered `fmtUsd(value)` — so the panel printed `-$57.47` under a
    // `%` toggle beside figures that had switched. A field that silently declines to switch is the
    // same failure as one that never could (#392). The render is asserted in
    // `heroRowRefusesDollarFallback.test.ts`; this pins the SOURCE so the fallback cannot come back
    // under a different call shape and pass that test by accident.
    expect(code).not.toMatch(/pct === null \? fmtUsd\(/);
    expect(code).toMatch(/pct === null \? "—" : fmtPct\(pct\)/);
    // And the dash says why: every refused figure carries `unitRefusal` on its title.
    for (const figure of ["net", "realized", "dUnrealized", "secured"]) {
      expect(code, `${figure} refuses without a reason on its title`).toMatch(new RegExp(`unitRefusal\\("${figure}"\\)`));
    }
  });

  it("gives EVERY figure a decided denominator — a new one cannot default", () => {
    // THE CLASS. Each `inUnit(x, "key")` call must name a key that exists in BOOK_FIGURES, so a
    // figure cannot be switched against a denominator nobody chose. Reads the table from source
    // rather than importing the tile, which would drag JSX and a live store into a unit test.
    const table = readFileSync(join(__dirname, "../../components/ds/unit.ts"), "utf8");
    const declared = new Set(Array.from(table.matchAll(/^\s{2}(\w+):\s*"/gm)).map((m) => m[1]));
    const used = Array.from(code.matchAll(/inUnit\([^,]+,\s*"(\w+)"\)/g)).map((m) => m[1]);
    expect(used.length, "no figure routes through inUnit — this guard matched nothing").toBeGreaterThan(0);
    for (const key of used) {
      expect(declared.has(key), `BookTile switches "${key}", which BOOK_FIGURES does not declare`).toBe(true);
    }
  });
});
