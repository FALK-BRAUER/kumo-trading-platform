/**
 * The trend strip may only use granularities the VENUE serves (#612).
 *
 * `bar_spec.py` splits them: `m1` and `d1` are EXTERNAL — the venue streams and backfills them.
 * `h1` and `w1` are INTERNAL, aggregated locally by Nautilus from the instrument's TRADE TICKS.
 *
 * IBKR does not deliver trade ticks. Measured on ibkr-paper-retired 2026-08-27:
 *
 *     10189: Failed to request tick-by-tick data. No market data permissions for NYSE STK
 *      322 : Max number of tick-by-tick requests has been reached      (73 occurrences)
 *
 * so every h1/w1 series is permanently empty there. The portfolio screen showed it precisely: of six
 * sparklines only 1M had data, and 1M was the only one backed by d1. 1W, 1Y and 5Y were not slow —
 * they were structurally impossible.
 *
 * This is a guard against reintroduction, not a test of arithmetic. Someone reaching for "1W would
 * look nicer on hourly bars" is making a change that works on one venue and silently blanks the
 * other, which is the #608 class.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/** Granularities the venue itself serves. Mirrors `bar_spec.py`'s EXTERNAL set. */
const VENUE_SERVED = new Set(["m1", "d1"]);
/** Locally aggregated from trade ticks — unavailable wherever tick-by-tick is refused. */
const TICK_DERIVED = new Set(["h1", "w1"]);

const SRC = readFileSync(join(import.meta.dirname, "trend.ts"), "utf8");

/** `series: "x"` entries from the SPANS table, comments stripped so prose cannot satisfy the rule. */
function declaredSeries(): string[] {
  const code = SRC.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
  return [...code.matchAll(/series:\s*"(\w+)"/g)].map((m) => m[1]);
}

describe("the guard can see the table", () => {
  it("finds one series per label", () => {
    // Fixture property first: if the regex matched nothing the rule below would hold vacuously.
    expect(declaredSeries().length).toBe(6);
  });

  it("the two sets are disjoint and non-empty, or the rule says nothing", () => {
    expect(VENUE_SERVED.size).toBeGreaterThan(0);
    expect(TICK_DERIVED.size).toBeGreaterThan(0);
    expect([...VENUE_SERVED].some((g) => TICK_DERIVED.has(g))).toBe(false);
  });
});

describe("every span is backed by a granularity the venue serves", () => {
  it("uses no tick-derived series", () => {
    const offenders = declaredSeries().filter((g) => TICK_DERIVED.has(g));
    expect(offenders).toEqual([]);
  });

  it("uses only m1 and d1", () => {
    for (const g of declaredSeries()) expect(VENUE_SERVED.has(g)).toBe(true);
  });

  it("still spans intraday AND multi-year, so this was not fixed by deleting windows", () => {
    // The cheap way to pass the rule above is to drop 1Y and 5Y. Pin that the labels survive.
    const code = SRC.replace(/\/\*[\s\S]*?\*\//g, " ");
    for (const label of ["1H", "1D", "1W", "1M", "1Y", "5Y"]) {
      expect(code).toContain(`label: "${label}"`);
    }
    expect(declaredSeries()).toContain("m1");
    expect(declaredSeries()).toContain("d1");
  });
});
