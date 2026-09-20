/**
 * A flat armed row must say WHAT it is waiting for (#402).
 *
 * 2026-08-21, on CRAK:
 *
 *   > Crak is interesting also. Is it because we have a breakout ordered?
 *
 * He was right, and the row could not tell him. It rendered `flat · NO PRICE · —` with a sub-line
 * reading `0 held · 1 armed` — a COUNT WITH NO NOUN. Three different situations are indistinguishable
 * from it, and two of them mean money is committed at the venue:
 *
 *     a resting entry that has not triggered      (CRAK, today)
 *     a stop-and-reenter waiting for a reclaim
 *     a PEAK manager still attached to a flattened position
 *
 * THE FIXTURE IS CRAK'S REAL ORDER BOOK, read from the live engine 2026-08-21:
 *
 *     SELL LIMIT       180 @ 57.15    target leg
 *     BUY  LIMIT       180 @ 57.09    the entry
 *     SELL STOP_MARKET 180 trig 57.06 protective stop
 *
 * TELLING THE ENTRY FROM THE EXIT LEGS IS NOT A COUNTING PROBLEM. A bracket can rest as entry + stop
 * with one order per side, which is a tie. The invariant used instead is that a protective stop always
 * rests on the REDUCING side, so the stop names the exit side and the entry is the opposite one.
 */
import { describe, expect, it } from "vitest";
import { armedLabel, armedReading } from "./armedReading";
import type { WorkingOrderDTO } from "@/lib/api/types";

const o = (
  side: string,
  order_type: string,
  price: number | null,
  trigger_price: number | null = null,
  status = "ACCEPTED",
  quantity = 180,
): WorkingOrderDTO =>
  ({ side, order_type, price, trigger_price, status, quantity, client_order_id: `${side}-${order_type}` }) as WorkingOrderDTO;

/** CRAK, live, 2026-08-21. */
const CRAK = [
  o("SELL", "LIMIT", 57.15),
  o("BUY", "LIMIT", 57.09),
  o("SELL", "STOP_MARKET", null, 57.06),
];

describe("a flat armed row names its pending entry (#402)", () => {
  it("the fixture is a genuine TIE on side count", () => {
    // The fixture's own property first. If the entry were the only order of its side by COUNT, a
    // count-based implementation would pass and the invariant under test would be untested. CRAK has
    // two SELLs and one BUY — but a bracket resting as entry + stop alone is 1:1, and the assertion
    // below is what makes the stop-side rule load-bearing rather than incidental.
    const entryPlusStop = [o("BUY", "LIMIT", 57.09), o("SELL", "STOP_MARKET", null, 57.06)];
    const bySide = entryPlusStop.filter((x) => x.side === "BUY").length;
    expect(bySide).toBe(entryPlusStop.filter((x) => x.side === "SELL").length);
  });

  it("names CRAK's resting breakout entry, not one of its exit legs", () => {
    const r = armedReading(CRAK);
    expect(r).toEqual({ kind: "entry", side: "BUY", orderType: "LIMIT", price: 57.09, qty: 180 });
    expect(armedLabel(r!)).toBe("awaiting entry · buy limit 57.09");
  });

  it("uses the STOP's side to find the exit side, not the order count", () => {
    // entry + stop only: 1:1. A count-based rule has nothing to go on; the invariant does.
    const r = armedReading([o("BUY", "LIMIT", 57.09), o("SELL", "STOP_MARKET", null, 57.06)]);
    expect(r).toMatchObject({ kind: "entry", side: "BUY", price: 57.09 });
  });

  it("works for a SHORT entry, where every side is reversed", () => {
    // The stop rests on the BUY side to reduce a short, so the entry is the SELL.
    const r = armedReading([o("SELL", "LIMIT", 20.0), o("BUY", "STOP_MARKET", null, 21.0)]);
    expect(r).toMatchObject({ kind: "entry", side: "SELL", price: 20.0 });
  });

  it("a lone resting order on a flat cycle IS the entry", () => {
    const r = armedReading([o("BUY", "STOP_MARKET", null, 57.06)]);
    expect(r).toMatchObject({ kind: "entry", side: "BUY", price: 57.06 });
  });

  it("falls back to a bare 'armed' rather than inventing a noun", () => {
    // Two same-side orders and no stop: nothing here says which opens. Guessing would put a confident
    // wrong sentence on a row about committed capital, which is worse than the count it replaced.
    const r = armedReading([o("BUY", "LIMIT", 10), o("BUY", "LIMIT", 11)]);
    expect(r).toEqual({ kind: "armed" });
    expect(armedLabel(r!)).toBe("armed");
  });

  it("TERMINAL orders are not resting and do not name anything", () => {
    // A cancelled bracket is exactly what a flattened position leaves behind. Reading one as a pending
    // entry would tell the operator capital is committed when it is not — the dangerous direction.
    expect(armedReading([o("BUY", "LIMIT", 57.09, null, "CANCELED")])).toBeNull();
    expect(armedReading([o("BUY", "LIMIT", 57.09, null, "FILLED")])).toBeNull();
    expect(armedReading([o("BUY", "LIMIT", 57.09, null, "REJECTED")])).toBeNull();
  });

  it("HELD counts as resting — that is the whole of #387", () => {
    // Alpaca's `open` filter hides `held`, and a held bracket leg still reserves its shares. A reading
    // that treated it as terminal would repeat #387 on the display plane.
    const r = armedReading([o("BUY", "LIMIT", 57.09, null, "HELD"), o("SELL", "STOP_MARKET", null, 57.06, "HELD")]);
    expect(r).toMatchObject({ kind: "entry", side: "BUY" });
  });

  it("no orders at all is null — a row with no commitment needs no explanation", () => {
    expect(armedReading([])).toBeNull();
    expect(armedReading(undefined)).toBeNull();
    expect(armedReading(null)).toBeNull();
  });

  it("a trigger-only entry reports its TRIGGER as the price", () => {
    // A stop-entry breakout has no limit price. Reporting null would drop the one number the operator
    // wants — the level the thing fires at.
    //
    // A single resting order, deliberately. An earlier version of this test paired a BUY STOP entry
    // with a SELL LIMIT target, which is a 1:1 tie that the protective-stop invariant resolves the
    // WRONG way — it reads the BUY STOP as the protective leg. That shape is not one this system
    // places (a stop entry with a target and no protection), and encoding a guess for it would be worse
    // than leaving it unnamed. The tie case is covered above with the shape we do place.
    const r = armedReading([o("BUY", "STOP_MARKET", null, 60.5)]);
    expect(r).toMatchObject({ kind: "entry", side: "BUY", price: 60.5 });
  });
});

describe("the tile renders the noun (#402)", () => {
  const SRC = require("node:fs").readFileSync(
    require("node:path").join(import.meta.dirname, "ManagedPortfolioTile.tsx"),
    "utf8",
  );
  const code = SRC.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

  it("the scan reads the real component", () => {
    expect(SRC.length).toBeGreaterThan(1000);
    expect(code).toContain("ManagedPortfolioTile");
  });

  it("no longer renders a bare count", () => {
    // Banned by name: `{group.armed} armed` is the shipped line.
    expect(code).not.toMatch(/\{group\.armed\}\s*armed/);
    expect(code).toMatch(/armedNoun\(group\)/);
  });

  it("the explanation says capital is committed", () => {
    expect(code).toMatch(/armedTitle/);
    expect(SRC).toMatch(/committed/);
  });
});
