/**
 * #855 — DEPLOYED is computed two ways in one function, and the two disagree on a short.
 *
 * `deployedValue` walks two lists into a single total:
 *
 *     for (const t of trades)   ... if (mv != null) value += Math.abs(mv);   // managed cycles
 *     for (const x of external) ... if (mv != null) value += mv;             // unclaimed rows
 *
 * Both lists carry the same field, filled by the same engine, signed the same way: `market_value` is
 * `last x signed_qty` (`engine_node.py:7441` for cycles, `protection.py:808` for unclaimed rows), so a
 * short's is NEGATIVE. One branch takes the magnitude and the other takes the raw value, which means a
 * short contributes +|mv| when it is claimed by a strategy and −|mv| when it is not.
 *
 * This is the shape this repo keeps paying for: two derivations of one number, identical on the data
 * that has existed so far (every long agrees) and divergent on the data about to exist. Today the
 * disagreement is unreachable because nothing shorts; the moment CRSI does, moving one row from
 * UNCLAIMED to a strategy — an ownership change that moves no stock — swings DEPLOYED by 2 x |mv|.
 *
 * TWO KINDS OF ASSERTION HERE, AND THE AGREEMENT ONE IS NOT ENOUGH ON ITS OWN. "The branches must
 * match" is satisfiable by changing BOTH to the same wrong answer — signing the managed branch would
 * make a short SUBTRACT from deployed capital, and the pair would agree all the way down. So the
 * convention is pinned by an ORACLE first, on the value: |market_value|, because DEPLOYED asks how
 * much capital is committed and a short commits capital rather than releasing it. That is already
 * pinned for the managed branch at `books.test.ts:264` ("takes |market_value| so a short adds to
 * deployed rather than netting against it"); these say the unclaimed branch answers the same question
 * the same way. The agreement test stays, because an oracle on each branch and an equality between
 * them fail differently when a fix is half-applied.
 */
import { describe, expect, it } from "vitest";

import { deployedValue, type ExternalLike } from "./books";
import type { TradeDTO } from "@/lib/api/types";

/** A managed cycle as `/trades` emits it: UNSIGNED `quantity`, SIGNED `market_value`. */
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

/** An unclaimed broker row. `venue_qty` non-zero — a phantom is skipped entirely and would be vacuous. */
const unclaimed = (over: Partial<ExternalLike>): ExternalLike => ({
  realized_pnl: "0.00 USD",
  unrealized_pl: 100,
  market_value: 1100,
  venue_qty: 10,
  ...over,
});

const SHORT_CYCLE = cycle({ side: "SHORT", quantity: 10, market_value: -1100, unrealized_pl: -100 });
const SHORT_UNCLAIMED = unclaimed({ market_value: -1100, unrealized_pl: -100, venue_qty: -10 });

describe("the fixtures can express the bug", () => {
  it("both rows are the SAME short position, differing only in who claims it", () => {
    // Vacuity guard. If either fixture carried a positive `market_value` the two branches would agree
    // by accident and every assertion below would pass while the defect stood. And if the unclaimed
    // row were a phantom (`venue_qty === 0`) `deployedValue` would skip it, so it would contribute 0
    // rather than the wrong sign.
    expect(SHORT_CYCLE.side).toBe("SHORT");
    expect(SHORT_CYCLE.market_value).toBeLessThan(0);
    expect(SHORT_UNCLAIMED.market_value).toBeLessThan(0);
    expect(SHORT_UNCLAIMED.market_value).toBe(SHORT_CYCLE.market_value);
    expect(SHORT_UNCLAIMED.venue_qty).not.toBe(0);
  });
});

describe("DEPLOYED counts a short as capital COMMITTED (#855)", () => {
  it("the managed branch already takes |market_value| — the convention, restated", () => {
    // Not a new claim: `books.test.ts:264` pins this and passes today. It is repeated here as the
    // ORACLE the unclaimed branch is measured against, so the two tests below cannot both be
    // satisfied by moving the managed branch to the wrong answer.
    expect(deployedValue([SHORT_CYCLE], []).value).toBe(1100);
  });

  it("an unclaimed SHORT row adds 1100, it does not subtract it", () => {
    // The oracle on the value. A short sold at 100 and marked at 110 has $1,100 of the account's
    // capacity tied up in it; DEPLOYED is what `pctDeployed` and the liquidation-gap reading are
    // built on, and a negative contribution there says the account has MORE room free because it
    // took on more risk.
    expect(deployedValue([], [SHORT_UNCLAIMED]).value).toBe(1100);
  });

  it("a SHORT contributes the same whether it is a managed cycle or an unclaimed row", () => {
    const managed = deployedValue([SHORT_CYCLE], []);
    const external = deployedValue([], [SHORT_UNCLAIMED]);
    expect(managed.unmarked).toBe(0);
    expect(external.unmarked).toBe(0);
    // Today: managed = +1100 (Math.abs), external = −1100 (raw). A $2,200 swing on a $1,100 position,
    // produced by claiming it.
    expect(external.value).toBe(managed.value);
  });

  it("a LONG already contributes the same either way — the disagreement is side-specific", () => {
    // The sibling that passes. It is here so the failure above cannot be read as "deployedValue is
    // broken", which would send the fix at the wrong line.
    const managed = deployedValue([cycle({})], []);
    const external = deployedValue([], [unclaimed({})]);
    expect(external.value).toBe(managed.value);
    expect(managed.value).toBe(1100);
  });

  /**
   * The flat book is TWO STRATEGIES, because one strategy cannot hold both legs.
   *
   * Under NETTING a strategy holds exactly one net position per instrument — the position id is
   * `{instrument}-{strategy_id}` — so a long 10 and a short 10 under one `strategy_id` is not a book
   * that can exist, and two cycles cannot share a `cycle_id` in one frame either. The live case this
   * is drawn from had WHD long 28 under BCTROT-004 and short 28 under MOMENTUM-002.
   *
   * A double that cannot represent production is the bug, and here it would have been a load-bearing
   * one: the whole point of the case is that ownership is what differs between the two readings.
   */
  const FLAT_BOOK = [
    cycle({ strategy_id: "BCTROT-004", cycle_id: "long-leg" }),
    cycle({
      strategy_id: "MOMENTUM-002",
      cycle_id: "short-leg",
      side: "SHORT",
      quantity: 10,
      market_value: -1100,
      unrealized_pl: -100,
    }),
  ];

  it("the flat book is one instrument held by TWO strategies", () => {
    // Vacuity guard on the fixture's own property, before the invariance it is used for.
    expect(FLAT_BOOK[0].instrument_id).toBe(FLAT_BOOK[1].instrument_id);
    expect(FLAT_BOOK[0].strategy_id).not.toBe(FLAT_BOOK[1].strategy_id);
    expect(FLAT_BOOK[0].cycle_id).not.toBe(FLAT_BOOK[1].cycle_id);
    expect(FLAT_BOOK[0].side).toBe("LONG");
    expect(FLAT_BOOK[1].side).toBe("SHORT");
    expect(FLAT_BOOK[0].quantity).toBe(Math.abs(FLAT_BOOK[1].quantity)); // genuinely flat
  });

  it("a genuinely flat book values the same however its two legs are claimed", () => {
    // The live shape this comes from: WHD was long 28 under BCTROT-004 and short 28 under
    // MOMENTUM-002 at the same mark, genuinely flat, and an unsigned market value summed the legs to
    // exposure that did not exist (#807, engine_node.py:7509-7515). Whatever DEPLOYED should say
    // about that book, it must say it once — not one number when both legs are claimed and another
    // when neither is.
    const bothClaimed = deployedValue(FLAT_BOOK, []);
    const neitherClaimed = deployedValue([], [unclaimed({}), SHORT_UNCLAIMED]);
    // The oracle: $2,200 of capital is committed across the two legs. Netting them to 0 says a
    // hedged book is a book holding nothing — the reading that let WHD's flat +28/-28 pair be
    // mis-sized in the first place (`engine_node.py:7509-7515`).
    expect(bothClaimed.value).toBe(2200);
    expect(neitherClaimed.value).toBe(2200);
    expect(neitherClaimed.value).toBe(bothClaimed.value);
  });
});
