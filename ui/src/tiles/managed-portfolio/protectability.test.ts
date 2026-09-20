/**
 * "Unprotected" and "unprotectable" are not the same condition (#757).
 *
 * MEASURED 2026-08-31, on two live stacks:
 *
 *   paper     "9 of 38 unprotected"    — positions protection has not covered YET
 *   staging   "Unprotected 22"         — positions it CAN NEVER cover, because 11 of 22 have no
 *                                        price and `plan_protection` refuses to size a stop it
 *                                        cannot price
 *
 * The screen renders them identically, so an operator reading staging sees an ordinary backlog. It
 * is not a backlog: while the venue refuses market data those positions will never be protected, and
 * no amount of waiting changes it.
 *
 * Three states, not two: protected / not yet / cannot. Collapsing the third into the second is what
 * let 22 unprotectable positions read as a queue.
 */
import { describe, expect, it } from "vitest";

import { protectability } from "./books";

const pos = (over: Record<string, unknown> = {}) =>
  ({
    instrument_id: "AEM.XNYS",
    is_capital_deployed: true,
    side: "LONG",
    broker_protected: false,
    working_orders: [],
    ...over,
  }) as never;

describe("protectability is three states", () => {
  it("a covered position is PROTECTED", () => {
    expect(protectability(pos({ broker_protected: true }), [])).toBe("protected");
  });

  it("an uncovered position the engine CAN price is NOT YET", () => {
    expect(protectability(pos(), [])).toBe("unprotected");
  });

  it("an uncovered position the engine CANNOT price is UNPROTECTABLE", () => {
    // The staging case: no price, so `plan_protection` refuses on `no_price` every tick forever.
    expect(protectability(pos(), ["AEM.XNYS"])).toBe("unprotectable");
  });

  it("being unpriceable does not override a stop that IS resting", () => {
    // A stop placed while a price existed keeps protecting after the feed goes. Reporting that as
    // unprotectable would be a false alarm on a genuinely covered position — the direction that
    // gets a safety badge ignored.
    expect(protectability(pos({ broker_protected: true }), ["AEM.XNYS"])).toBe("protected");
  });

  it("an EMPTY unpriceable list leaves every verdict unchanged", () => {
    // The common case, and the one that must stay cheap: paper prices everything.
    expect(protectability(pos(), [])).toBe("unprotected");
    expect(protectability(pos({ broker_protected: true }), [])).toBe("protected");
  });

  it("an ABSENT unpriceable list is not the same as an empty one", () => {
    // `undefined` means the engine did not tell us — an older frame, a degraded read. It must not
    // silently assert that everything is priceable, which would be the healthy-looking answer.
    expect(protectability(pos(), undefined)).toBe("unprotected");
  });
});
