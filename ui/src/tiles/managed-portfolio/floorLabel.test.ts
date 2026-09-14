/**
 * A floor stop reads as a FLOOR, not as an absence (#872).
 *
 * THE UI PROBLEM THIS SOLVES. `securedValue` accumulates only `gain > 0` — a stop above entry for a
 * long — and an entry floor sits BELOW entry by construction. So QC345-003, fully protected by three
 * floors, renders permanently as:
 *
 *     SECURED · NOW   $0.00
 *     3 of 3 protected · none above entry
 *
 * Both lines are true and the pair reads as "nothing is protecting this". The tally is deliberately
 * left alone — it answers "what would survive if everything went wrong", and a floor genuinely secures
 * no GAIN — and the caption is what has to say which kind of stop is resting.
 *
 * THE KIND COMES FROM THE ORDER'S OWN TAG, not from a second derivation over the order type. A
 * STOP_MARKET is also a bracket leg, a PEAK stop, and an operator's own sell; deriving "is this a
 * floor" from the type would drift from the mechanism that placed it the first time either changed.
 */

import { describe, expect, it } from "vitest";
import { floorStopsOn, floorLabel, securedValue } from "./books";
import { securedSub } from "@/tiles/book/securedSub";
import type { TradeDTO } from "@/lib/api/types";

const FLOOR_TAG = "mode:entry_floor";

function order(over: Record<string, unknown> = {}) {
  return {
    client_order_id: "PROT-SELL-AMAT-XNAS-abc",
    side: "SELL",
    order_type: "STOP_MARKET",
    quantity: 6,
    leaves_qty: 6,
    price: null,
    trigger_price: 451.69,
    time_in_force: "GTC",
    status: "ACCEPTED",
    ts_last: 0,
    tags: [FLOOR_TAG],
    ...over,
  };
}

function cycle(over: Record<string, unknown> = {}): TradeDTO {
  return {
    instrument_id: "AMAT.XNAS",
    strategy_id: "QC345-003",
    side: "LONG",
    quantity: 6,
    avg_px_open: 475.73,
    is_capital_deployed: true,
    broker_protected: true,
    working_orders: [order()],
    ...over,
  } as unknown as TradeDTO;
}

describe("floorStopsOn", () => {
  it("finds a stop the engine tagged as an entry floor", () => {
    expect(floorStopsOn(cycle())).toHaveLength(1);
  });

  it("does NOT treat an untagged STOP_MARKET as a floor", () => {
    // FIXTURE PROPERTY: this order is a stop of the same TYPE and the same side, so a
    // type-derived mutant would call it a floor. It is a bracket leg.
    const bracket = order({ tags: ["bracket:g1"], client_order_id: "ALPACA-GENERATED" });
    expect(bracket.order_type).toBe("STOP_MARKET");
    expect(floorStopsOn(cycle({ working_orders: [bracket] }))).toEqual([]);
  });

  it("does NOT treat a trailing stop as a floor", () => {
    const trail = order({ order_type: "TRAILING_STOP_MARKET", trigger_price: null, tags: [] });
    expect(floorStopsOn(cycle({ working_orders: [trail] }))).toEqual([]);
  });

  it("ignores an order that is no longer live", () => {
    expect(floorStopsOn(cycle({ working_orders: [order({ status: "FILLED" })] }))).toEqual([]);
  });

  it("survives a frame with no tags at all", () => {
    // An older engine, or a DTO that has not been redeployed. `undefined` is not "it is a floor".
    expect(floorStopsOn(cycle({ working_orders: [order({ tags: undefined })] }))).toEqual([]);
  });
});

describe("floorLabel", () => {
  it("names the kind AND its trigger", () => {
    // The trigger is the fact an operator needs: "floor" alone does not say where it sits.
    expect(floorLabel(cycle())).toBe("floor 451.69");
  });

  it("is undefined when nothing is a floor, so the row stays quiet", () => {
    expect(floorLabel(cycle({ working_orders: [] }))).toBeUndefined();
  });

  it("counts the rungs of a ladder rather than showing only one", () => {
    // A scale-in adds a SECOND floor at the new entry, by design. Rendering one of two would be a
    // number that is true about half the position.
    const ladder = [order(), order({ client_order_id: "PROT-SELL-AMAT-XNAS-def", trigger_price: 462.5 })];
    expect(floorLabel(cycle({ working_orders: ladder }))).toBe("floor 451.69 · 462.50");
  });
});

describe("the SECURED tally and its caption", () => {
  it("still counts a floor as COVERED and as securing no gain", () => {
    // Left exactly as it was, deliberately: a stop below entry is loss limitation, not a locked-in
    // gain, and adding it as a negative would net against real secured value elsewhere.
    const tally = securedValue([cycle()]);
    expect(tally.covered).toBe(1);
    expect(tally.naked).toBe(0);
    expect(tally.value).toBe(0);
  });

  it("reports how many of the covered holdings are floors", () => {
    expect(securedValue([cycle()]).floors).toBe(1);
  });

  it("says FLOOR in the caption instead of the bare none-above-entry line", () => {
    // THE READING THAT WAS WRONG: "1 of 1 protected · none above entry" on a lane that is protected
    // exactly as intended. The number does not change; what it MEANS is now on the screen.
    expect(securedSub(securedValue([cycle()]), 1)).toBe("1 of 1 protected · 1 floor below entry");
  });

  it("keeps the old caption when the covered stops are not floors", () => {
    const trailed = cycle({
      working_orders: [order({ order_type: "TRAILING_STOP_MARKET", trigger_price: 400, tags: [] })],
    });
    expect(securedSub(securedValue([trailed]), 1)).toBe("1 of 1 protected · none above entry");
  });

  it("still outranks the floor note with a naked holding", () => {
    // ORDERING IS DELIBERATE and unchanged: a position with nothing under it is a worse fact than a
    // protected one whose stop sits below entry.
    const naked = cycle({ working_orders: [], broker_protected: false });
    expect(securedSub(securedValue([cycle(), naked]), 2)).toBe("1 of 2 unprotected");
  });
});
