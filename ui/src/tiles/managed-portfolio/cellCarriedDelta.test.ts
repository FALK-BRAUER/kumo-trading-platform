/**
 * The per-lane cell can finally show a WINDOW DELTA, not just window realized (#699).
 *
 * WHAT THE OPERATOR ASKED FOR, before any of the rest of it: the strategy tile showing the delta for the
 * SELECTED period. Until now the cell led with `realized(W)` — honest, and only half the answer,
 * because `NET(W) = realized(W) + Δunrealized(W)` and the second term needed a per-lane mark at the
 * window's START. `cellHeadline`'s own comment said so: "which the broker does not publish (#699)".
 *
 * It is published now — `GET /pnl/unrealized-base`, from the EOD observation table (#734).
 *
 * THE THREE STATES SURVIVE INTO THE CELL, which is the whole reason this took a table rather than a
 * subtraction. A delta that is UNKNOWN must not become zero: `realized(W) + 0` renders as a complete
 * net and is a smaller, more confident lie than the em dash it replaced.
 */

import { describe, expect, it } from "vitest";
import { cellHeadline } from "./books";

const NO_DAY = undefined;

describe("the delta is CARRIED beside the headline, never summed into it", () => {
  // I SHIPPED A COMMIT THAT SUMMED THEM AND IT WAS WRONG. `realized(W)` is FIFO by lot; both ends of
  // Δunrealized use `avg_px_open`, which under NETTING does not move on a partial close. Buy 50@10
  // and 50@20 (avg 15), window-start mark 20, sell 50 @20 in-window with the mark unchanged:
  // FIFO realized 500, unrealized 500 -> 250, so the sum reads 250 where the truth is 0. The error
  // is `closed_qty x (avg - fifo_lot_basis)`, and momentum scales out routinely.
  it("keeps the headline at window REALIZED and does not add the delta to it", () => {
    const h = cellHeadline("1M", NO_DAY, 3085.56, -257.41);
    expect(h.kind).toBe("window");
    expect(h.value).toBeCloseTo(3085.56, 2);
    expect(h.unrealizedDelta).toBeCloseTo(-257.41, 2);
  });

  it("carries a POSITIVE delta too, so the sign is not assumed", () => {
    expect(cellHeadline("1M", NO_DAY, 100, 40).unrealizedDelta).toBeCloseTo(40, 6);
  });

  it("carries a delta even when realized is exactly zero", () => {
    const h = cellHeadline("1M", NO_DAY, 0, -50);
    expect(h.value).toBeCloseTo(0, 6);
    expect(h.unrealizedDelta).toBeCloseTo(-50, 6);
  });

  it("refuses a NON-FINITE delta rather than propagating NaN into a cell", () => {
    expect(cellHeadline("1M", NO_DAY, 100, NaN).unrealizedDelta).toBeNull();
    expect(cellHeadline("1M", NO_DAY, 100, Infinity).unrealizedDelta).toBeNull();
  });
});

describe("what an unknown delta must NOT become", () => {
  it("reports an unknown delta as NULL, never as zero", () => {
    // A zero delta and an unknown one are different facts. Rendering "no change in mark" where the
    // truth is "we never captured that day" is the confident-wrong-number this table exists to end.
    const h = cellHeadline("1M", NO_DAY, 3085.56, null);
    expect(h.kind).toBe("window");
    expect(h.value).toBeCloseTo(3085.56, 2);
    expect(h.unrealizedDelta).toBeNull();
  });

  it("treats an ABSENT delta the same as an explicitly unknown one", () => {
    expect(cellHeadline("1M", NO_DAY, 100, undefined).unrealizedDelta).toBeNull();
  });

  it("stays UNSWEPT when realized itself is unknown, whatever the delta says", () => {
    // A delta without a realized half is not a net. An unswept window plus a known mark change is
    // still an unswept window — reporting the mark change alone under a NET label would answer a
    // question nobody asked.
    const h = cellHeadline("1M", NO_DAY, null, -257.41);
    expect(h.kind).toBe("unswept");
    expect(h.value).toBeNull();
  });
});

describe("1D is unchanged", () => {
  // THE DOUBLE HAD TO BE FIXED, NOT THE ASSERTION. My first fixture passed
  // `{ move, unpriced, held }` — field names that do not exist. `readDayMove` reads `covered`,
  // `value` and `missing`, so it returned `{kind:"nothing"}`, the 1D branch never fired, and the
  // test failed against perfectly correct code. A double that cannot represent production is the
  // bug; loosening the assertion would have hidden a real regression later.
  const REAL_DAY = { covered: 1, value: 12.5, missing: 0 };

  it("still leads with the DAY MOVE, which is the one true per-lane delta we already had", () => {
    const h = cellHeadline("1D", REAL_DAY as never, 99, -40);
    expect(h.kind).toBe("day");
    expect(h.value).toBeCloseTo(12.5, 6);
  });

  it("does NOT add the window delta on top of the day move", () => {
    // 1D's day move is already a delta — qty x (mark − prior close). Adding a window delta to it
    // would double-count the same change, and the number would look plausible.
    const h = cellHeadline("1D", REAL_DAY as never, 99, -40);
    expect(h.value).toBeCloseTo(12.5, 6);
  });

  it("falls through to the window when 1D has no usable day reading", () => {
    // A 1D cell whose prior closes never arrived has no day move. It is then an ordinary window —
    // the same fall-through the function already had, still carrying the delta beside it.
    const h = cellHeadline("1D", { covered: 0, value: 0, missing: 2 } as never, 99, -40);
    expect(h.kind).toBe("window");
    expect(h.value).toBeCloseTo(99, 6);
    expect(h.unrealizedDelta).toBeCloseTo(-40, 6);
  });
});
