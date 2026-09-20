/**
 * NET must be REALIZED + MEASURED Δunrealized (#596).
 *
 * 2026-08-27: "Net at the top is simply realised + unrealised for the period. cash and
 * liquidation and balance are completely different things."
 *
 * TODAY IT IS INVERTED. `BookTile.tsx:230` takes NET from the equity curve (`equity - base_value`)
 * and `:242` derives Δunrealized as `net - realized`. The panel then presents the three numbers as
 * if their sum were a measurement. It is an identity BY CONSTRUCTION — it cannot disagree, so it
 * cannot detect anything. Measured on an Alpaca paper instance 2026-08-27 18:49, every period at once:
 *
 *     REALIZED ALL  $3,530.66   Δ UNREALIZED ALL  -$3,530.66   NET  $0.00
 *     REALIZED 1M   $4,211.73   Δ UNREALIZED 1M   -$4,211.73   NET  $0.00
 *     REALIZED 1W    -$269.58   Δ UNREALIZED 1W    +$269.58    NET  $0.00
 *
 * Δ is exactly minus REALIZED in every row, because NET was 0 and `0 - realized` is what prints.
 *
 * WHY THIS REVERSES A DELIBERATE DECISION. `panelIdentity.ts` argues "DERIVED, NOT RECOMPUTED",
 * because "summing per-position marks against window-start prices would build a second ledger that
 * disagrees with the broker's NET". Right about RECOMPUTING marks; wrong about the source used
 * here. `unrealized_intraday_pl` is the BROKER'S OWN FIELD, from the same endpoint as the positions
 * and the same provenance as REALIZED — not a parallel ledger:
 *
 *     GET /v2/positions -> AEM  unrealized_pl -31.05   unrealized_intraday_pl -1.26
 *
 * ARITHMETIC IS NOT PROVENANCE (codex, coverage review). A test that `f(a,b) === a+b` cannot fail
 * for any interesting reason — it is the same vacuity as the identity it replaces, one level out.
 * So the assertions that carry weight here are about WHICH FIELD each period reads and what happens
 * when it is absent, plus a call-site guard that the panel actually stopped back-solving.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { measuredUnrealizedDelta, netFromComponents } from "./panelIdentity";

const BOOK_TILE = join(import.meta.dirname, "BookTile.tsx");

/** What the account plane delivers on Alpaca. Numbers measured on an Alpaca paper instance 2026-08-27. */
const ALPACA = { unrealized_standing_total: 2251.06, unrealized_intraday_total: -33.0 };

/** IBKR publishes no intraday field. The ABSENCE is the point, so it is modelled as null. */
const IBKR = { unrealized_standing_total: 381.06, unrealized_intraday_total: null };

describe("the fixture can express the defect", () => {
  it("standing and intraday are DIFFERENT quantities", () => {
    // A fixture where the day's move equals the lifetime move cannot tell 1D from ALL apart, and
    // every routing assertion below would pass against a function that ignored `period`.
    expect(measuredUnrealizedDelta("1D", ALPACA)).not.toBeCloseTo(
      measuredUnrealizedDelta("all", ALPACA) as number, 2,
    );
  });

  it("the IBKR fixture really lacks the intraday figure", () => {
    expect(IBKR.unrealized_intraday_total).toBeNull();
    // ...and still HAS a standing one, or the venue-neutral assertion below proves nothing.
    expect(typeof IBKR.unrealized_standing_total).toBe("number");
  });
});

describe("each period reads the field that actually means its delta", () => {
  it("1D is the sum of the broker's INTRADAY field", () => {
    // -1.26 + 18.40 - 50.14 = -33.00. The live panel read NET - 1D = -$33.24 on this same book,
    // which is the tie to reality: this is the day's mark movement and nothing else.
    expect(measuredUnrealizedDelta("1D", ALPACA)).toBeCloseTo(-33.0, 2);
  });

  it("ALL is the sum of STANDING unrealized — nothing was held at inception", () => {
    // -31.05 + 412.11 + 1870.00 = 2,251.06, exactly the "standing" figure the panel printed.
    // Standing IS the lifetime delta because the account opened flat. Not an approximation.
    expect(measuredUnrealizedDelta("all", ALPACA)).toBeCloseTo(2251.06, 2);
  });

  it.each(["1W", "1M", "3M"])("%s is UNAVAILABLE — no broker field, no recorded series", (p) => {
    // No broker reports "unrealized as of seven days ago", and nothing records it yet. Returning a
    // number here would be a guess wearing a measurement's clothes.
    expect(measuredUnrealizedDelta(p, ALPACA)).toBeNull();
  });

  it("1D on a venue WITHOUT the intraday field is unavailable, not zero and not standing", () => {
    // The #573 mistake in a different costume: an IBKR tenant must not silently borrow Alpaca
    // semantics. Falling back to `unrealized_pl` would report the position's WHOLE life as today.
    expect(measuredUnrealizedDelta("1D", IBKR)).toBeNull();
  });

  it("ALL still works on that venue — standing is venue-neutral", () => {
    expect(measuredUnrealizedDelta("all", IBKR)).toBeCloseTo(381.06, 2);
  });

  it("an EMPTY book is zero for ALL, because nothing held is a fact, not an absence", () => {
    expect(measuredUnrealizedDelta("all", { unrealized_standing_total: 0 })).toBe(0);
  });

  it("a NON-FINITE total makes the period unavailable, not NaN on the panel", () => {
    // JSON encoders carry NaN/Infinity through as real numbers; "$NaN" in the hero slot reads as a
    // broken tile rather than as missing data.
    expect(measuredUnrealizedDelta("all", { unrealized_standing_total: Number.NaN })).toBeNull();
  });

  it("a MISSING account frame is unavailable, not zero", () => {
    expect(measuredUnrealizedDelta("1D", undefined)).toBeNull();
    expect(measuredUnrealizedDelta("all", null)).toBeNull();
  });
});

describe("NET is the sum, and unknown never becomes zero", () => {
  it("unknown Δ makes NET unknown", () => {
    expect(netFromComponents(3530.66, null)).toBeNull();
  });

  it("unknown REALIZED makes NET unknown", () => {
    expect(netFromComponents(null, -33.0)).toBeNull();
  });
});

describe("the PANEL stopped back-solving (provenance, not arithmetic)", () => {
  const src = readFileSync(BOOK_TILE, "utf8").replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

  it("the guard reaches the file that carried the bug", () => {
    // Fixture property first: if the read failed or comments ate everything, the assertions below
    // would pass against nothing at all.
    expect(src.length).toBeGreaterThan(5000);
    // Something the panel demonstrably contains. "BOOK" is NOT in this file — the heading comes from
    // elsewhere — and asserting it made this guard fail against a correct file, which is the guard
    // reporting a defect in itself rather than in the code.
    expect(src).toMatch(/periodNet/);
    expect(src).toMatch(/realized/);
  });

  it("NET no longer comes from deltaUnrealized(net, realized)", () => {
    // THE DEFECT, as a call site. `deltaUnrealized` derives Δ FROM net; using it as the panel's Δ is
    // what makes the sum definitional. A unit test on the new function cannot see this.
    expect(src).not.toMatch(/deltaUnrealized\s*\(/);
  });

  it("NET is assembled from the two measured components", () => {
    expect(src).toMatch(/netFromComponents\s*\(/);
    expect(src).toMatch(/measuredUnrealizedDelta\s*\(/);
  });
});
