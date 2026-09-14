/**
 * A guard against the class of defect #343, not against its one instance.
 *
 * THE DEFECT. Alpaca publishes a per-period equity curve carrying `base_value` and `pnl`, and it
 * computes that `pnl` to the LAST POINT OF ITS PORTFOLIO HISTORY — the previous session's close. Two
 * separate tiles read the field straight off the frame and presented it as the window's P&L beside a
 * LIVE equity figure. Measured on the paper account 2026-08-18 12:40 UTC, premarket:
 *
 *   BOOK    "NET · 1W  +$2,922.86"    while NET · 1D read −$615.59 — a week not containing its own day
 *   EQUITY  "+$1,556 this 1M"         beside a headline of $100,232 over a chart starting at $99,291
 *
 * and lifetime P&L read +$847.61 on an account funded at exactly $100,000 now standing at $100,232.02.
 *
 * THE INVARIANT, stated so a machine can check it: **no tile may read `.pnl` off an equity curve.** The
 * window's end is always live equity, which is what `periodNet` computes — so every consumer goes
 * through that one function and they cannot disagree with each other or with LIQUIDATION. `periodNet`
 * itself is the sole exception: it owns the field, and uses it only as the fallback for a node that has
 * no broker account at all.
 *
 * WHY A SOURCE SCAN. There are no rendered-component tests in this project, and the bug is not in any
 * helper — every helper was correct. It is in WHICH FIELD the tile reached for, which is a property of
 * the call site. A test on `periodNet` passes whether or not anything calls it (CLAUDE.md: test the
 * seam, not the unit — this shape shipped green five times in one day). Two tiles had the bug and only
 * one was found by reading; the scan finds the third.
 */
import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const SRC = join(import.meta.dirname, "..", "..");  // src/ — this file sits at src/tiles/book/

/** The module that owns the field. Everything else must go through its export. */
const OWNER = join("tiles", "book", "periodNet.ts");

/**
 * A read of `pnl` off something curve-shaped: `curve.pnl`, `curve?.pnl`, `curves[period].pnl`,
 * `c?.pnl ?? null`. Deliberately NOT a bare `.pnl` anywhere — `point.pnl` inside the chart series is a
 * legitimate per-sample delta, and banning it would make this test noise that gets disabled.
 */
const CURVE_PNL = /\bcurves?\b[^;\n]{0,40}?\??\.\s*pnl\b/;

/**
 * COMMENTS ARE NOT CODE. Written after this guard's first run flagged `EquityTile.tsx` for a comment
 * that quotes the very line it had just removed. A rule that a fix's own explanation can violate is one
 * that gets deleted rather than obeyed — and worse, it teaches the next person not to write the comment.
 */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
}

/** The predicate under test, named so the assertions below can exercise it directly. */
function readsCurvePnl(source: string): boolean {
  return CURVE_PNL.test(stripComments(source));
}

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) out.push(...sourceFiles(p));
    else if (/\.tsx?$/.test(e.name) && !/\.test\.tsx?$/.test(e.name)) out.push(p);
  }
  return out;
}

describe("period P&L has ONE source (#343)", () => {
  const files = sourceFiles(SRC);

  it("the scan reaches the files that carried the bug", () => {
    // Assert the fixture's own property FIRST. A glob that silently matched nothing would make every
    // assertion below vacuously true, which is how a guard comes to pin nothing at all.
    expect(files.length).toBeGreaterThan(20);
    expect(files).toContain(join(SRC, "tiles", "equity", "EquityTile.tsx"));
    expect(files).toContain(join(SRC, "tiles", "book", "BookTile.tsx"));
  });

  it("periodNet still reads the field — the exception has to be real", () => {
    // If the owner stopped reading `pnl`, the rule below would hold trivially and this guard would be
    // pinning the absence of a mechanism rather than its containment.
    expect(readFileSync(join(SRC, OWNER), "utf8")).toMatch(/\bpnl\b/);
  });

  it("the predicate catches the REAL line and nothing that merely resembles it", () => {
    // The exact expression `EquityTile` shipped, and the exact expression `BookTile`'s NET was built to
    // replace. If the scan cannot fail on these, everything below it is decoration.
    expect(readsCurvePnl("const pnl = curve?.pnl ?? null;")).toBe(true);
    expect(readsCurvePnl("const p = curves[period].pnl;")).toBe(true);
    expect(readsCurvePnl("return frame?.curves?.[period]?.pnl;")).toBe(true);

    // A comment quoting the removed line — this fix's own explanation does exactly that.
    expect(readsCurvePnl("// it used to be `curve.pnl`, measured to the last close")).toBe(false);
    expect(readsCurvePnl("/* curve.pnl is the broker's to-last-close figure */")).toBe(false);

    // The chart's per-sample delta is a different and legitimate field.
    expect(readsCurvePnl("points.map((p) => p.pnl)")).toBe(false);
    expect(readsCurvePnl("const { pnl } = point;")).toBe(false);
  });

  it("no other module reads pnl off an equity curve", () => {
    const offenders = files
      .filter((f) => !f.endsWith(OWNER))
      .filter((f) => readsCurvePnl(readFileSync(f, "utf8")))
      .map((f) => f.slice(SRC.length + 1));
    expect(offenders).toEqual([]);
  });
});
