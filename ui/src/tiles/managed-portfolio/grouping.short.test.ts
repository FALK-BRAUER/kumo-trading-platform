/**
 * #855 — `groupByPosition` applies the side to a quantity it has not normalised.
 *
 *     g.netQty += t.side === "SHORT" ? -t.quantity : t.quantity;      // grouping.ts:77
 *
 * `netQty` is the row's direction as well as its size: `ManagedPortfolioTile.tsx:311` colours the cell
 * bull when it is positive and bear when it is negative. Every other quantity read in this same loop
 * normalises first — `Math.abs(t.quantity)` twice, three lines below, for the weighted average — so
 * this line is the only one in the function that trusts the sign it was handed.
 *
 * While `quantity` is unsigned that trust is repaid. When a signed quantity reaches it, `-(-10)` is
 * `+10` and the row renders a SHORT as a LONG of the same size: the sign is applied twice and cancels.
 * There is no partial failure here and nothing to notice — the row is coherent, coloured, clickable,
 * and describes the opposite position to the one held.
 *
 * The sibling case matters as much: a signed quantity on a LONG must not invert either, or a fix that
 * simply drops the ternary trades one wrong row for another.
 */
import { describe, expect, it } from "vitest";

import { groupByPosition } from "./grouping";
import type { TradeDTO } from "@/lib/api/types";

const cycle = (over: Partial<TradeDTO>): TradeDTO =>
  ({
    account_id: "DU1",
    client_id: "IB",
    instrument_id: "AAPL.XNAS",
    strategy_id: "MOMENTUM-002",
    cycle_id: "c1",
    state: "HELD",
    side: "LONG",
    quantity: 10,
    is_capital_deployed: true,
    is_engaged: true,
    avg_px_open: 100,
    realized_pnl: "0.00 USD",
    last_px: 110,
    market_value: 1100,
    leg_count: 1,
    opened_ts: 0,
    last_event_ts: 0,
    working_orders: [],
    ...over,
  }) as TradeDTO;

describe("the fixture can express the bug", () => {
  it("netQty is reached at all — the cycle is HELD, so the branch that sets it runs", () => {
    // Vacuity guard. `netQty` is only touched inside `if (t.is_capital_deployed)`. A fixture that is
    // not deployed leaves it at its initial 0, and 0 is neither long nor short — every sign assertion
    // below would pass against a row the loop never entered.
    const [g] = groupByPosition([cycle({ side: "SHORT", quantity: 10 })]);
    expect(g.held).toBe(1);
    expect(g.netQty).toBe(-10); // the wire contract, and it works today
  });
});

describe("netQty carries the side exactly once (#855)", () => {
  it("a SHORT with a signed quantity is still short", () => {
    // −10 shares, side SHORT. Applying the side to an already-signed quantity yields +10 and the row
    // renders bull-coloured `10` for a position that is short 10.
    const [g] = groupByPosition([cycle({ side: "SHORT", quantity: -10 })]);
    expect(g.netQty).toBe(-10);
  });

  it("a LONG with a signed quantity is still long", () => {
    const [g] = groupByPosition([cycle({ side: "LONG", quantity: -10 })]);
    expect(g.netQty).toBe(10);
  });

  it("two shorts in one row net to twice the size, not to zero", () => {
    // The failure mode that hides itself: one signed leg and one unsigned leg in the same row cancel
    // to netQty 0, which the tile renders as a FLAT position in neutral grey — a row holding 20 short
    // shares reading as holding none.
    const [g] = groupByPosition([
      cycle({ cycle_id: "c1", side: "SHORT", quantity: 10 }),
      cycle({ cycle_id: "c2", side: "SHORT", quantity: -10 }),
    ]);
    expect(g.netQty).toBe(-20);
  });
});

/**
 * The same row written both ways, as a table.
 *
 * `netQty` is the only value in this function that trusts the sign it is handed, so the property
 * worth pinning is that the spelling cannot change the row: same side, same magnitude, same answer.
 * The oracle is asserted on the unsigned spelling first — "the two agree" is satisfiable by breaking
 * both, and breaking both here would flip every row in the tile at once, which is the version of this
 * bug that nobody would ship but every agreement-only test would allow.
 */
describe("one row, two spellings, one direction (#855)", () => {
  const CASES: { name: string; side: string; magnitude: number; netQty: number }[] = [
    { name: "a short", side: "SHORT", magnitude: 10, netQty: -10 },
    { name: "a long", side: "LONG", magnitude: 10, netQty: 10 },
  ];

  for (const c of CASES) {
    it(`${c.name} nets the same signed or unsigned`, () => {
      const [unsigned] = groupByPosition([cycle({ side: c.side, quantity: c.magnitude })]);
      const [signed] = groupByPosition([cycle({ side: c.side, quantity: -c.magnitude })]);
      expect(unsigned.netQty).toBe(c.netQty); // the oracle
      expect(signed.netQty).toBe(c.netQty); // the invariance
    });

    it(`${c.name} keeps its weighted average whichever way it is spelled`, () => {
      // The sibling read three lines below the defect, which already normalises with `Math.abs`. It
      // passes today, and it is here because a fix that reaches for the raw quantity again — rather
      // than for one predicate — would take this correct line down with it.
      const [unsigned] = groupByPosition([cycle({ side: c.side, quantity: c.magnitude })]);
      const [signed] = groupByPosition([cycle({ side: c.side, quantity: -c.magnitude })]);
      expect(unsigned.avg).toBe(100);
      expect(signed.avg).toBe(100);
    });
  }
});
