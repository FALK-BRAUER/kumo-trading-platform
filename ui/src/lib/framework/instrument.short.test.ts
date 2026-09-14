/**
 * #855 — computePnl is the ONE place mark-to-market is computed, and it double-negates a short.
 *
 * The contract (backend `engine_node.py:7433`, `signed = d.quantity if d.side == "LONG" else -d.quantity`)
 * is that the wire carries an UNSIGNED `quantity` and the direction in `side`. computePnl applies the
 * sign itself:
 *
 *     amt = (price - basis) * position.quantity * (long ? 1 : -1)
 *
 * That is correct only while `quantity` is guaranteed positive. Nothing in the type system says so —
 * `PositionDTO.quantity` is a bare `number` — and the moment shorts are permitted a signed quantity is
 * a live possibility on the same field (the engine already keeps `pos.signed_qty` beside it, and #807's
 * marking paths shipped the unsigned/signed confusion TWICE in one file). When a signed quantity
 * arrives, `amt` flips positive while `pct` stays negative: the headline reads "+$100  -10.00%".
 *
 * A percentage and an amount with OPPOSITE SIGNS on the same position is not a rounding disagreement,
 * it is one of the two numbers being wrong, and the operator cannot tell which. The fix is a single
 * `signedQty(side, quantity)` predicate that takes `Math.abs()` first — the form already written by hand
 * at `books.ts:423` and `PositionDetail.tsx:246`, and the reason those two exist is that this one does
 * not.
 */
import { describe, expect, it } from "vitest";

import { computePnl } from "./instrument";
import type { PositionDTO } from "@/lib/api/types";

/** Built from what `/positions` actually emits: unsigned quantity, direction in `side`, Money string. */
const position = (over: Partial<PositionDTO>): PositionDTO =>
  ({
    instrument_id: "AAPL.XNAS",
    side: "LONG",
    quantity: 10,
    avg_px_open: 100,
    realized_pnl: "0.00 USD",
    strategy_id: "MOMENTUM-002",
    ts_last: 0,
    ...over,
  }) as PositionDTO;

describe("the fixture can express the bug", () => {
  it("the short fixture IS a short, and its mark has MOVED AGAINST it", () => {
    // Vacuity guard. A fixture at its entry price, or one whose side is LONG, cannot violate any
    // sign invariance below — every assertion would pass by having nothing to disagree with.
    const p = position({ side: "SHORT", quantity: 10, avg_px_open: 100 });
    expect(p.side).toBe("SHORT");
    expect(p.quantity).toBeGreaterThan(0); // the wire contract: UNSIGNED
    expect(110).toBeGreaterThan(p.avg_px_open); // price above entry = a LOSING short
  });
});

describe("computePnl on the wire contract (unsigned quantity)", () => {
  it("short 10 @ 100, mark 110 → -$100 and -10%", () => {
    // 10 shares sold at 100 and marked at 110 owes 10 x $10 = $100, which is 10% of the $1,000 basis.
    // This is the case that works today; it is here so the signed case below cannot be mistaken for a
    // general breakage of shorts.
    const { amt, pct } = computePnl(position({ side: "SHORT", quantity: 10, avg_px_open: 100 }), 110);
    expect(amt).toBe(-100);
    expect(pct).toBeCloseTo(-10, 10);
  });
});

describe("computePnl when a SIGNED quantity arrives on the same field", () => {
  const signedShort = position({ side: "SHORT", quantity: -10, avg_px_open: 100 });

  it("the signed fixture is genuinely signed, and the same size as the unsigned one", () => {
    // The guard that belongs immediately before the invariants, not at the top of the file. If this
    // fixture's quantity were positive it would be the wire-contract case again and every assertion
    // below would pass while testing nothing about the signed spelling; if it were a different SIZE
    // the amounts could differ for an honest reason and the equality tests would be meaningless.
    expect(signedShort.quantity).toBeLessThan(0);
    expect(Math.abs(signedShort.quantity)).toBe(10);
    expect(Math.abs(signedShort.quantity)).toBe(
      Math.abs(position({ side: "SHORT", quantity: 10 }).quantity),
    );
    expect(signedShort.side).toBe("SHORT");
  });

  it("the amount and the percent must not disagree about which way the position went", () => {
    // THE INVARIANT, stated without naming the right answer: whatever the magnitude, a position cannot
    // be making money and losing money at the same time. `Math.sign` of the two must match.
    const { amt, pct } = computePnl(signedShort, 110);
    expect(amt).not.toBeNull();
    expect(Math.sign(amt as number)).toBe(Math.sign(pct));
    // AND THE VALUES, beside the agreement. Sign-agreement alone is satisfiable by two wrong numbers
    // that happen to point the same way — a fix returning +$100 and +10% would clear it while
    // reporting a losing short as a winner. Sold 10 at 100 and marked at 110: down $100, down 10%.
    expect(amt).toBe(-100);
    expect(pct).toBeCloseTo(-10, 10);
  });

  it("short 10 @ 100, mark 110 is -$100 however the quantity is spelled", () => {
    // The sign belongs to `side`. A quantity that already carries it must be normalised, not applied
    // twice: `(110-100) * -10 * -1 = +100` is the double negation.
    const { amt } = computePnl(signedShort, 110);
    expect(amt).toBe(-100);
  });

  it("a signed LONG quantity is not a short", () => {
    // The sibling case. `side` is the authority; a stray minus on a LONG must not invert the P&L
    // either, or the same predicate is wrong in the other direction.
    const { amt } = computePnl(position({ side: "LONG", quantity: -10, avg_px_open: 100 }), 110);
    expect(amt).toBe(100);
  });
});

/**
 * The same four positions, each written both ways, driven as a table.
 *
 * The single-case tests above name the number and say where it comes from; this says the SPELLING
 * cannot matter — which is the property a `signedQty` predicate actually delivers and the one that
 * survives someone changing the fixture prices. Both directions and both sides are here because a fix
 * that keys off the quantity's own sign instead of off `side` passes half of them.
 */
describe("one position, two spellings, one answer (#855)", () => {
  const CASES: { name: string; side: string; magnitude: number; price: number; amt: number; pct: number }[] = [
    // Sold 10 at 100, marked 110: owes $100, which is 10% of the $1,000 committed.
    { name: "a losing short", side: "SHORT", magnitude: 10, price: 110, amt: -100, pct: -10 },
    // Sold 10 at 100, marked 90: made $100.
    { name: "a winning short", side: "SHORT", magnitude: 10, price: 90, amt: 100, pct: 10 },
    { name: "a winning long", side: "LONG", magnitude: 10, price: 110, amt: 100, pct: 10 },
    { name: "a losing long", side: "LONG", magnitude: 10, price: 90, amt: -100, pct: -10 },
  ];

  for (const c of CASES) {
    it(`${c.name} reads the same signed or unsigned`, () => {
      const unsigned = computePnl(position({ side: c.side, quantity: c.magnitude }), c.price);
      const signed = computePnl(position({ side: c.side, quantity: -c.magnitude }), c.price);
      // The oracle first, so "they agree" cannot be satisfied by both being wrong.
      expect(unsigned.amt).toBe(c.amt);
      expect(unsigned.pct).toBeCloseTo(c.pct, 10);
      // Then the invariance.
      expect(signed.amt).toBe(c.amt);
      expect(signed.pct).toBeCloseTo(c.pct, 10);
    });
  }
});


/**
 * A quantity that cannot be read is UNKNOWN, not zero (#855).
 *
 * `magnitude` maps a non-finite quantity to 0 so the SIGN RULE stays total — it runs on render paths
 * and inside the trade-cycle publisher, where a raise removes the whole book from the screen rather
 * than surfacing one bad row. But 0 is the right answer for a sign and the wrong one for a display:
 * before the fix `(price - basis) * quantity * ±1` yielded NaN and the cell rendered an em-dash;
 * afterwards it yielded 0 and the cell rendered `$0.00`. A three-state answer collapsed into a
 * confident flat, on the number the operator reads to decide whether to act.
 *
 * Not reachable from the wire — JSON carries no NaN and the producer contract pins non-negative
 * finite quantities — so this is about what the function may be HANDED, by a store, a mock, or a
 * future bridge, not about a live frame.
 */
describe("an unreadable quantity renders unknown, not flat", () => {
  it("the fixture is genuinely unreadable, and its side is not", () => {
    // Vacuity guard: a finite quantity would take the ordinary path and say nothing about this.
    const p = position({ side: "LONG", quantity: Number.NaN });
    expect(Number.isFinite(p.quantity)).toBe(false);
    expect(p.side).toBe("LONG");
  });

  it("NaN yields amt null — the cell shows an em-dash, not $0", () => {
    expect(computePnl(position({ side: "LONG", quantity: Number.NaN }), 110).amt).toBeNull();
    expect(computePnl(position({ side: "SHORT", quantity: Number.NaN }), 110).amt).toBeNull();
  });

  it("Infinity yields amt null too", () => {
    expect(computePnl(position({ side: "LONG", quantity: Number.POSITIVE_INFINITY }), 110).amt).toBeNull();
  });

  it("a real zero quantity is a real zero — that one IS known", () => {
    // The sibling that must NOT become unknown: a flat position genuinely has no unrealized amount,
    // and rendering an em-dash there would be the mirror-image defect.
    expect(computePnl(position({ side: "LONG", quantity: 0 }), 110).amt).toBe(0);
  });
});
