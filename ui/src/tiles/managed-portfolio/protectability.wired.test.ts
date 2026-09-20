/**
 * The SEAM for "unprotectable collapses into unprotected".
 *
 * `protectability` shipped correct and CALLED BY NOTHING — the shape this repo keeps re-finding: a
 * change that anchors on something absent does nothing, quietly. These tests drive the tally the
 * headline actually renders from, and the hop that carries the engine's answer to it.
 *
 * MEASURED 2026-08-31: paper "9 of 38 unprotected", staging "Unprotected 22", rendered identically.
 * Paper's drain. Staging's cannot: 11 of its 22 have no price and `plan_protection` refuses to size a
 * stop it cannot price, every tick, for as long as the venue refuses market data.
 */
import { describe, expect, it } from "vitest";

import { securedValue, protectability } from "./books";
import type { TradeDTO } from "@/lib/api/types";

const held = (over: Partial<TradeDTO> & { instrument_id?: string }): TradeDTO =>
  ({
    is_capital_deployed: true,
    quantity: 10,
    avg_px_open: 100,
    side: "LONG",
    working_orders: [],
    ...over,
  }) as unknown as TradeDTO;

// `side: "SELL"` is not decoration: `protectiveOrdersOn` requires the order to REDUCE the position,
// so a stop without it is filtered out and the double would silently describe an unprotected book
// while claiming to describe a protected one.
const STOP = {
  order_type: "STOP_MARKET",
  side: "SELL",
  trigger_price: 105,
  quantity: 10,
  leaves_qty: 10,
};

describe("the fixture can express the bug", () => {
  it("an unpriceable holding is genuinely unprotected first", () => {
    // Vacuity guard. If the fixture were already protected, every assertion below would pass by
    // having nothing that could violate it — kumo-trading-strategies' truncation test, one level out.
    expect(protectability(held({ instrument_id: "PENG.XNAS" }), [])).toBe("unprotected");
    expect(protectability(held({ instrument_id: "PENG.XNAS" }), ["PENG.XNAS"])).toBe("unprotectable");
  });
});

describe("securedValue separates CANNOT from NOT YET", () => {
  it("counts an unpriceable holding as unprotectable, not as naked", () => {
    const s = securedValue([held({ instrument_id: "PENG.XNAS" })], ["PENG.XNAS"]);
    expect(s.unprotectable).toBe(1);
    expect(s.naked).toBe(0);
  });

  it("still counts a priceable holding with no stop as naked", () => {
    const s = securedValue([held({ instrument_id: "AAPL.XNAS" })], ["PENG.XNAS"]);
    expect(s.naked).toBe(1);
    expect(s.unprotectable).toBe(0);
  });

  it("a RESTING STOP beats unpriceability", () => {
    // A stop placed while a price existed keeps protecting after the feed goes. Calling that
    // unprotectable is a false alarm on a covered position — the direction that gets a safety badge
    // ignored, which is worse than the under-report it would be fixing.
    const s = securedValue(
      [held({ instrument_id: "PENG.XNAS", working_orders: [STOP] as never })],
      ["PENG.XNAS"],
    );
    expect(s.covered).toBe(1);
    expect(s.unprotectable).toBe(0);
    expect(s.naked).toBe(0);
  });

  it("UNDEFINED is not an empty list — an unasked engine must not assert everything is priceable", () => {
    const s = securedValue([held({ instrument_id: "PENG.XNAS" })], undefined);
    expect(s.unprotectable).toBe(0);
    expect(s.naked).toBe(1);
  });

  it("the three buckets sum to the held count", () => {
    // A panel whose cells do not add up to its total is #596.
    const trades = [
      held({ instrument_id: "A", working_orders: [STOP] as never }),
      held({ instrument_id: "B" }),
      held({ instrument_id: "C" }),
    ];
    const s = securedValue(trades, ["C"]);
    expect(s.covered + s.naked + s.unprotectable).toBe(3);
    expect([s.covered, s.naked, s.unprotectable]).toEqual([1, 1, 1]);
  });

  it("the SECURED VALUE is unchanged by the split", () => {
    // The dollar figure answers a different question and must not move because a third counter was
    // added beside it. Two derivations of one fact drifting is this file's own warning.
    const trades = [held({ instrument_id: "A", working_orders: [STOP] as never }), held({ instrument_id: "C" })];
    expect(securedValue(trades, ["C"]).value).toBe(securedValue(trades, []).value);
  });
});

describe("the engine's answer reaches the tally", () => {
  it("useHealth exposes unpricedPositions", async () => {
    // The hop that was missing. `protectability` took the list as an argument and nothing in the app
    // could supply it, so the correct function was unreachable from the screen it was written for.
    const mod = await import("@/lib/framework/health");
    const classify = (mod as { classifyHealth?: unknown }).classifyHealth as
      | ((a: Record<string, unknown>) => { unpricedPositions?: string[] })
      | undefined;
    expect(classify, "classifyHealth is not exported, so this hop cannot be tested").toBeTypeOf(
      "function",
    );
    const out = classify!({
      isError: false,
      isFetched: true,
      data: {
        status: "ok",
        subsystems: [
          { name: "engine", ok: true },
          { name: "redis", ok: true },
          { name: "postgres", ok: true },
        ],
        feed_last_tick_ts: 0,
        unpriced_positions: ["PENG.XNAS"],
      },
      // `feed` is required by the signature — a double omitting it throws inside classifyHealth,
      // which is production rejecting what a looser double would have accepted.
      feed: { tone: "ok", healthy: true },
      wsConnected: true,
      wsActive: true,
    });
    expect(out.unpricedPositions).toEqual(["PENG.XNAS"]);
  });

  it("an ABSENT key stays undefined rather than becoming an empty list", async () => {
    // Measured as a survivor: changing `?.unpriced_positions` to `?? []` in health.ts left every
    // other test here green. An older or degraded frame would then assert that everything is
    // priceable, and 22 unprotectable holdings would silently read as an ordinary backlog again —
    // absence rendered as permission, one hop away from where it was fixed.
    const mod = await import("@/lib/framework/health");
    // Through `unknown`: the test deliberately feeds a loose record, and TS (correctly) refuses the
    // direct cast between `HealthInputs` and `Record<string, unknown>` (#810).
    const classify = (mod as unknown as { classifyHealth: (a: Record<string, unknown>) => { unpricedPositions?: string[] } })
      .classifyHealth;
    const out = classify({
      isError: false,
      isFetched: true,
      data: { status: "ok", subsystems: [{ name: "engine", ok: true }], feed_last_tick_ts: 0 },
      feed: { tone: "ok", healthy: true },
      wsConnected: true,
      wsActive: true,
    });
    expect(out.unpricedPositions).toBeUndefined();
  });

  it("the BookTile headline renders the third state", async () => {
    // A source assertion, deliberately: rendering the tile needs the whole query/websocket stack.
    // What must be pinned is that the count reaches the copy — a tally computed and never displayed
    // is the same defect one layer down.
    const fs = await import("node:fs/promises");
    const src = await fs.readFile("src/tiles/book/BookTile.tsx", "utf8");
    expect(src, "BookTile does not render unprotectable, so the split stops at the tally").toMatch(
      /unprotectable/,
    );
    expect(src, "BookTile does not pass the engine's unpriced list into securedValue").toMatch(
      /securedValue\(\s*trades\s*,/,
    );
  });
});
